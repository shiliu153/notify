"""命令行入口：参数解析与退出码映射。"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, TextIO

from .channels.base import Channel
from .config import Config, ConfigError, load_config
from .core import (
    EXIT_ALL_FAILED,
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_SKIPPED,
    SendOutcome,
    build_channels,
    send_message,
)
from .log import format_ts
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
    except (ConfigError, MessageError) as exc:
        print("%s: %s" % (PROG, exc), file=deps.err)
        return EXIT_CONFIG

    print("%s: 未知子命令 %r" % (PROG, args.command), file=deps.err)
    return EXIT_CONFIG
