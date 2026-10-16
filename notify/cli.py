"""命令行入口：参数解析与退出码映射。"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, TextIO

from .channels.base import Channel, deliver_with_retry
from .config import DEFAULT_LOG_PATH, Config, ConfigError, load_config
from .core import (
    EXIT_ALL_FAILED,
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_SKIPPED,
    SendOutcome,
    build_channels,
    build_envelope,
    make_subject,
    select_channels,
    send_message,
)
from .log import format_ts, tail
from .message import Message, MessageError
from .store import Store

PROG = "notify"

TO_HELP = "投递目标：auto（默认，所有就绪通道）/ email / email:<通道名>"

SELFTEST_BODY = (
    "这是一条 notify 自检消息。收到它说明通道凭据与投递链路正常。\n"
    "\n"
    "- 若它落在垃圾箱，请标记为“非垃圾邮件”\n"
    "- 并把发件人加入联系人或白名单\n"
)


@dataclass
class Deps:
    config: Optional[Config] = None
    channels: Optional[List[Channel]] = None
    store: Optional[Store] = None
    sleep: Callable[[float], None] = time.sleep
    out: Optional[TextIO] = None
    err: Optional[TextIO] = None


def _add_config_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="配置文件路径（默认 ~/.config/notify/config.ini）",
    )


def _validate_to(value: str) -> str:
    if value in ("auto", "email") or value.startswith("email:"):
        return value
    raise argparse.ArgumentTypeError(
        "未知的 --to 取值：%r（可用 auto / email / email:<通道名>）" % value
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="日报邮件通知工具")
    subparsers = parser.add_subparsers(dest="command")

    send = subparsers.add_parser("send", help="发送一条通知")
    _add_config_option(send)
    send.add_argument("--title", required=True, help="邮件标题")
    body_group = send.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body", help="正文文本")
    body_group.add_argument("--body-file", help="正文文件路径（- 表示标准输入）")
    send.add_argument("--level", choices=["info", "warn", "error"], default="info")
    send.add_argument("--to", type=_validate_to, default="auto", help=TO_HELP)
    send.add_argument("--dedup-key", default=None, help="去重键（默认由标题派生）")
    send.add_argument(
        "--dedup-window",
        type=int,
        default=None,
        metavar="MINUTES",
        help="去重窗口分钟数；0 表示禁用去重",
    )
    send.add_argument("--dry-run", action="store_true", help="只渲染与检查，不发信")
    send.add_argument("--json", action="store_true", dest="as_json", help="输出机器可读结果")

    test = subparsers.add_parser("test", help="给每个就绪通道发一条自检消息")
    _add_config_option(test)
    test.add_argument("--to", type=_validate_to, default="auto", help=TO_HELP)
    test.add_argument("--json", action="store_true", dest="as_json")

    channels_cmd = subparsers.add_parser("channels", help="列出通道及就绪状态")
    _add_config_option(channels_cmd)

    doctor = subparsers.add_parser("doctor", help="体检：配置、权限、端口、DNS")
    _add_config_option(doctor)
    doctor.add_argument("--timeout", type=int, default=5, metavar="SECONDS")
    doctor.add_argument(
        "--dkim-selector",
        default=None,
        metavar="NAME",
        help="DKIM 选择器（控制台生成），用于 DNS 检查",
    )

    log_cmd = subparsers.add_parser("log", help="查看最近的投递记录")
    _add_config_option(log_cmd)
    log_cmd.add_argument("-n", "--number", type=int, default=20)
    log_cmd.add_argument("--json", action="store_true", dest="as_json")

    return parser


def read_body(args) -> str:
    if getattr(args, "body", None) is not None:
        return args.body
    path = getattr(args, "body_file", None)
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise MessageError("读取正文文件失败：%s" % exc)


def resolve_config(deps: Deps, args) -> Config:
    if deps.config is not None:
        return deps.config
    return load_config(getattr(args, "config", None))


def resolve_channels(deps: Deps, config: Config) -> List[Channel]:
    if deps.channels is not None:
        return deps.channels
    return build_channels(config)


def _seconds(result) -> str:
    return "%5.1fs" % (result.duration_ms / 1000.0)


def report(outcome: SendOutcome, config: Config, message: Message, deps: Deps, as_json=False) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "exit_code": outcome.exit_code,
                    "subject": outcome.subject,
                    "skipped": outcome.skipped,
                    "dry_run": outcome.dry_run,
                    "message_id": outcome.message_id,
                    "channels": [
                        {
                            "channel": result.channel,
                            "ok": result.ok,
                            "attempts": result.attempts,
                            "duration_ms": result.duration_ms,
                            "error": result.error,
                            "error_class": result.error_class,
                        }
                        for result in outcome.results
                    ],
                },
                ensure_ascii=False,
            ),
            file=deps.out,
        )
        return

    if outcome.dry_run:
        print("dry-run：未发送。", file=deps.out)
        print("  收件人：%s" % ", ".join(config.recipients), file=deps.out)
        print("  主题：%s" % outcome.subject, file=deps.out)
        print("  正文：%d 行" % len(message.body.splitlines()), file=deps.out)
        return

    if outcome.exit_code == EXIT_SKIPPED:
        print("跳过（%s）：%s" % (outcome.skipped, outcome.subject), file=deps.out)
        return

    for result in outcome.results:
        if result.ok:
            print(
                "✓ %-22s %s  %s" % (result.channel, _seconds(result), result.message_id),
                file=deps.out,
            )
        else:
            print("✗ %-22s %s  %s" % (result.channel, _seconds(result), result.error), file=deps.out)

    if outcome.exit_code == EXIT_OK and len(outcome.results) > 1:
        print(
            "已降级：%s 失败后由 %s 送达"
            % (outcome.results[0].channel, outcome.results[-1].channel),
            file=deps.out,
        )


def cmd_send(args, deps: Deps) -> int:
    config = resolve_config(deps, args)
    message = Message(
        title=args.title,
        body=read_body(args),
        level=args.level,
        dedup_key=args.dedup_key,
        source=socket.gethostname(),
        created_at=format_ts(),
    )
    store = deps.store if deps.store is not None else Store(config.state_path)
    channels = resolve_channels(deps, config)
    outcome = send_message(
        config,
        message,
        selector=args.to,
        dedup_window=args.dedup_window,
        dry_run=args.dry_run,
        store=store,
        channels=channels,
        log_path=config.log_path,
        sleep=deps.sleep,
    )
    report(outcome, config, message, deps, as_json=args.as_json)
    return outcome.exit_code


def cmd_test(args, deps: Deps) -> int:
    """给每个就绪通道各发一条自检消息（不写去重与限流状态）。"""
    config = resolve_config(deps, args)
    channels = resolve_channels(deps, config)
    try:
        targets = select_channels(channels, args.to)
    except ValueError as exc:
        print("%s: %s" % (PROG, exc), file=deps.err)
        return EXIT_CONFIG
    if not targets:
        print("%s: 没有就绪的通道（检查密钥环境变量是否已加载）" % PROG, file=deps.err)
        return EXIT_CONFIG

    message = Message(
        title="notify 自检",
        body=SELFTEST_BODY,
        level="info",
        created_at=format_ts(),
    )
    subject = make_subject(config, message)
    hostname = socket.gethostname()

    payload = []
    failed = 0
    for channel in targets:
        envelope = build_envelope(config, message, subject, hostname, channel.sender)
        result = deliver_with_retry(channel, envelope, sleep=deps.sleep)
        payload.append(
            {
                "channel": result.channel,
                "ok": result.ok,
                "attempts": result.attempts,
                "duration_ms": result.duration_ms,
                "error": result.error,
            }
        )
        if result.ok:
            print(
                "✓ %-22s %s  %s" % (result.channel, _seconds(result), result.message_id),
                file=deps.out,
            )
        else:
            failed += 1
            print(
                "✗ %-22s %s  %s" % (result.channel, _seconds(result), result.error),
                file=deps.err,
            )

    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False), file=deps.out)

    return EXIT_OK if failed == 0 else EXIT_ALL_FAILED


def cmd_channels(args, deps: Deps) -> int:
    config = resolve_config(deps, args)
    channels = resolve_channels(deps, config)
    print("%-24s %-8s %-8s %s" % ("通道", "启用", "状态", "服务端"), file=deps.out)
    for channel in channels:
        settings = getattr(channel, "config", None)
        endpoint = (
            "%s:%s (%s)" % (settings.host, settings.port, settings.security)
            if settings is not None
            else "-"
        )
        enabled = "是" if getattr(settings, "enabled", True) else "否"
        print(
            "%-24s %-8s %-8s %s"
            % (channel.name, enabled, "就绪" if channel.ready() else "未就绪", endpoint),
            file=deps.out,
        )
    return EXIT_OK


def check_tcp(host: str, port: int, timeout: int = 5) -> Optional[str]:
    """通返回 None，否则返回错误描述。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return "%s: %s" % (type(exc).__name__, exc)


def check_dns(domain: str, dkim_selector: Optional[str] = None) -> List[str]:
    """有 dig 才检查；返回人类可读的结论行。"""
    dig = shutil.which("dig")
    if not dig:
        return ["跳过 DNS 检查（未找到 dig 命令）"]

    def query(name: str, record_type: str) -> List[str]:
        try:
            completed = subprocess.run(
                [dig, "+short", record_type, name],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ["查询 %s 失败：%s" % (name, exc)]
        return [
            line
            for line in completed.stdout.decode("utf-8", "replace").splitlines()
            if line.strip()
        ]

    results: List[str] = []
    spf = query(domain, "TXT")
    if any("v=spf1" in line for line in spf):
        results.append("SPF 记录存在")
    else:
        results.append("SPF 记录缺失（应在 %s 上有一条 v=spf1 的 TXT）" % domain)

    dmarc = query("_dmarc." + domain, "TXT")
    if any("v=DMARC1" in line for line in dmarc):
        results.append("DMARC 记录存在")
    else:
        results.append("DMARC 记录缺失（建议加 _dmarc.%s 的 v=DMARC1 记录）" % domain)

    if dkim_selector:
        dkim = query("%s._domainkey.%s" % (dkim_selector, domain), "TXT")
        results.append("DKIM 记录存在" if dkim else "DKIM 记录缺失（选择器 %s）" % dkim_selector)
    else:
        results.append("未检查 DKIM（用 --dkim-selector 指定控制台生成的选择器）")

    return results


def cmd_doctor(args, deps: Deps) -> int:
    try:
        config = resolve_config(deps, args)
    except ConfigError as exc:
        print("✗ 配置：%s" % exc, file=deps.err)
        return EXIT_CONFIG

    channels = resolve_channels(deps, config)
    failures = 0

    print("配置：%s" % config.path, file=deps.out)
    print("收件人：%s" % ", ".join(config.recipients), file=deps.out)
    print("发件人名称：%s" % config.from_name, file=deps.out)
    print(
        "限流：每分钟 %d 封 / 每天 %d 封，去重窗口 %d 分钟"
        % (config.limits.per_minute, config.limits.per_day, config.limits.dedup_window_minutes),
        file=deps.out,
    )

    ready = [channel for channel in channels if channel.ready()]
    if not ready:
        failures += 1
        print("✗ 没有就绪的通道：检查密钥环境变量或 password_file", file=deps.out)

    domain = None
    for channel in channels:
        settings = getattr(channel, "config", None)
        print("通道 %s：%s" % (channel.name, "就绪" if channel.ready() else "未就绪"), file=deps.out)
        if settings is None:
            continue
        if settings.password and "@" in settings.username:
            domain = settings.username.split("@", 1)[1]
        if not channel.ready():
            continue
        problem = check_tcp(settings.host, settings.port, timeout=args.timeout)
        if problem:
            failures += 1
            print(
                "  ✗ TCP %s:%d 不通：%s" % (settings.host, settings.port, problem),
                file=deps.out,
            )
        else:
            print("  ✓ TCP %s:%d 可达" % (settings.host, settings.port), file=deps.out)

    if domain:
        for line in check_dns(domain, args.dkim_selector):
            print("  DNS：%s" % line, file=deps.out)

    print("结论：%s" % ("全部通过" if failures == 0 else "%d 项失败" % failures), file=deps.out)
    return EXIT_OK if failures == 0 else EXIT_CONFIG


def cmd_log(args, deps: Deps) -> int:
    try:
        log_path = resolve_config(deps, args).log_path
    except ConfigError:
        log_path = DEFAULT_LOG_PATH

    records = tail(log_path, args.number)
    if args.as_json:
        print(json.dumps(records, ensure_ascii=False, indent=2), file=deps.out)
        return EXIT_OK

    if not records:
        print("没有投递记录（%s）" % log_path, file=deps.out)
        return EXIT_OK

    for record in records:
        event = record.get("event", "?")
        if event == "delivery":
            mark = "✓" if record.get("ok") else "✗"
            detail = record.get("message_id") or record.get("error") or ""
            print(
                "%-9s %s  %-22s %s  %s"
                % (event, mark, record.get("channel", "-"), record.get("ts", "-"), detail),
                file=deps.out,
            )
        else:
            print(
                "%-9s    %s  %s"
                % (
                    event,
                    record.get("ts", "-"),
                    record.get("reason") or record.get("error") or record.get("title", ""),
                ),
                file=deps.out,
            )
    return EXIT_OK


def main(
    argv=None,
    *,
    config=None,
    channels=None,
    store=None,
    sleep=time.sleep,
    out=None,
    err=None,
) -> int:
    deps = Deps(
        config=config,
        channels=channels,
        store=store,
        sleep=sleep,
        out=out if out is not None else sys.stdout,
        err=err if err is not None else sys.stderr,
    )
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help(deps.err)
        return EXIT_CONFIG

    try:
        if args.command == "send":
            return cmd_send(args, deps)
        if args.command == "test":
            return cmd_test(args, deps)
        if args.command == "channels":
            return cmd_channels(args, deps)
        if args.command == "doctor":
            return cmd_doctor(args, deps)
        if args.command == "log":
            return cmd_log(args, deps)
    except (ConfigError, MessageError) as exc:
        print("%s: %s" % (PROG, exc), file=deps.err)
        return EXIT_CONFIG

    print("%s: 未知子命令 %r" % (PROG, args.command), file=deps.err)
    return EXIT_CONFIG
