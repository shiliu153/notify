#!/usr/bin/env python3
"""为 shiliu153 这台 ECS 定制的日报生成器。

只做一件事：把当天的状态汇总成 markdown 打到 stdout，交给 notify 发送。

    python3.11 daily-report.py > /tmp/daily.md
    /opt/notify/bin/notify send --title "服务器日报 $(date +%F)" \
        --body-file /tmp/daily.md --dedup-key daily-digest

设计原则：**任何一段采集失败都不许让整封日报消失**——每节独立 try，
失败就写一行"采集失败：原因"，其余照常。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ---- 本机配置（可用环境变量覆盖） ----------------------------------------

WB_DIR = Path(os.environ.get("WB_DIR", "/www/dk_project/workbuddy2api"))
WB_BASE = os.environ.get("WB_BASE", "http://127.0.0.1:7863")

# 要巡检的站点：(显示名, URL)
SITES = [
    ("主站 shiliu153.cn", "https://shiliu153.cn/"),
    ("主站 shiliu153.me", "https://shiliu153.me/"),
    ("网关 wb.shiliu153.me", "https://wb.shiliu153.me/healthz"),
]

# 要巡检的 systemd 单元
UNITS = ["nginx", "mysqld", "redis", "docker"]

# 要巡检的本机端口 (端口, 说明)
PORTS = [
    (80, "nginx http"),
    (443, "nginx https"),
    (3306, "mysql"),
    (6379, "redis"),
    (7863, "workbuddy2api"),
    (8000, "chatapp-web"),
    (8001, "chatapp-2"),
]

HTTP_TIMEOUT = 10


# ---- 小工具 ---------------------------------------------------------------


def run(cmd, timeout=15):
    """跑一条命令，返回 (exit_code, stdout)。永不抛异常。"""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()
    except Exception as exc:  # noqa: BLE001 - 采集失败不应中断日报
        return -1, "%s: %s" % (type(exc).__name__, exc)


def human_bytes(num):
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step:
            return "%.1f%s" % (num, unit)
        num /= step
    return "%.1fPB" % num


def human_tokens(num):
    if num >= 1_000_000_000:
        return "%.2fB" % (num / 1_000_000_000)
    if num >= 1_000_000:
        return "%.2fM" % (num / 1_000_000)
    if num >= 1_000:
        return "%.1fK" % (num / 1_000)
    return str(num)


def section(title):
    print()
    print("## %s" % title)
    print()


def table(headers, rows):
    print("| " + " | ".join(headers) + " |")
    print("|" + "|".join([" --- "] * len(headers)) + "|")
    for row in rows:
        print("| " + " | ".join(str(c) for c in row) + " |")


# ---- 一、主机 -------------------------------------------------------------


def collect_host():
    section("一、主机")
    rows = []

    code, out = run(["df", "-P", "/"])
    if code == 0 and len(out.splitlines()) >= 2:
        parts = out.splitlines()[1].split()
        if len(parts) >= 5:
            rows.append(("磁盘 /", "%s / %s (%s)" % (parts[2], parts[1], parts[4])))
    else:
        rows.append(("磁盘 /", "采集失败"))

    code, out = run(["free", "-m"])
    if code == 0:
        for line in out.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                total, used, avail = int(parts[1]), int(parts[2]), int(parts[6])
                rows.append(
                    ("内存", "%dM / %dM (%.0f%%，可用 %dM)" % (used, total, 100.0 * used / total, avail))
                )
    else:
        rows.append(("内存", "采集失败"))

    try:
        load1, load5, load15 = os.getloadavg()
        rows.append(("负载 1/5/15 分钟", "%.2f / %.2f / %.2f" % (load1, load5, load15)))
    except OSError:
        rows.append(("负载", "采集失败"))

    try:
        with open("/proc/uptime", "r") as fh:
            seconds = float(fh.read().split()[0])
        days, rem = divmod(int(seconds), 86400)
        rows.append(("运行时长", "%d 天 %d 小时" % (days, rem // 3600)))
    except Exception:
        rows.append(("运行时长", "采集失败"))

    code, out = run(["systemctl", "--failed", "--no-legend", "--no-pager"])
    if code == 0:
        failed = [l.split()[0] for l in out.splitlines() if l.strip()]
        rows.append(("失败的 systemd 单元", "无" if not failed else "、".join(failed)))

    table(["项目", "值"], rows)


# ---- 二、服务存活 ---------------------------------------------------------


def collect_services():
    section("二、服务存活")
    rows = []

    for unit in UNITS:
        code, out = run(["systemctl", "is-active", unit])
        state = out if out else "unknown"
        rows.append((unit, "✅ %s" % state if code == 0 else "❌ %s" % state))

    code, out = run(
        ["docker", "inspect", "-f", "{{.State.Status}}|{{.State.StartedAt}}", "workbuddy2api"]
    )
    if code == 0 and "|" in out:
        status, started = out.split("|", 1)
        rows.append(("容器 workbuddy2api", "✅ %s（启动于 %s）" % (status, started[:19])))
    else:
        rows.append(("容器 workbuddy2api", "❌ 查询失败"))

    for port, label in PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3):
                rows.append(("端口 %d" % port, "✅ %s" % label))
        except OSError:
            rows.append(("端口 %d" % port, "❌ %s 未监听" % label))

    table(["对象", "状态"], rows)


# ---- 三、网站可用性 -------------------------------------------------------


def collect_sites():
    section("三、网站可用性")
    rows = []
    for label, url in SITES:
        started = time.time()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "notify-daily-report"})
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                code = response.status
                body = response.read(4096).decode("utf-8", "replace")
            elapsed = time.time() - started
            extra = ""
            if "healthz" in url:
                try:
                    payload = json.loads(body)
                    extra = "（健康账号 %s/%s）" % (payload.get("healthy"), payload.get("total"))
                except ValueError:
                    pass
            mark = "✅" if 200 <= code < 400 else "⚠️"
            rows.append((label, "%s %d" % (mark, code), "%.2fs%s" % (elapsed, extra)))
        except urllib.error.HTTPError as exc:
            rows.append((label, "⚠️ %d" % exc.code, "%.2fs" % (time.time() - started)))
        except Exception as exc:  # noqa: BLE001
            rows.append((label, "❌ 不可达", "%s" % type(exc).__name__))
    table(["站点", "状态", "耗时"], rows)


# ---- 四、workbuddy2api 用量 ----------------------------------------------


def _wb_api_key():
    try:
        with open(str(WB_DIR / "config.json"), "r", encoding="utf-8") as fh:
            return json.load(fh).get("api_key", "")
    except Exception:
        return ""


def _wb_get(path):
    request = urllib.request.Request(
        WB_BASE + path,
        headers={"Authorization": "Bearer " + _wb_api_key()},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _sum_buckets(buckets):
    totals = {"q": 0, "e": 0, "p": 0, "c": 0, "t": 0, "ch": 0, "cm": 0}
    for bucket in buckets:
        for key in totals:
            totals[key] += bucket.get(key, 0) or 0
    return totals


def collect_wb():
    section("四、workbuddy2api 用量")

    # 4.1 今日与窗口内合计（来自按小时分桶的 usage.json）
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(str(WB_DIR / "data" / "usage.json"), "r", encoding="utf-8") as fh:
            usage = json.load(fh)
        buckets = usage.get("buckets", [])
        today_buckets = [b for b in buckets if str(b.get("s", "")).startswith("h:" + today)]
        today_sum = _sum_buckets(today_buckets)
        window_sum = _sum_buckets(buckets)

        def hit_rate(s):
            denom = s["ch"] + s["cm"]
            return "%.2f%%" % (100.0 * s["ch"] / denom) if denom else "—"

        table(
            ["指标", "今日", "记录窗口内合计"],
            [
                ("请求数", today_sum["q"], window_sum["q"]),
                ("错误数", today_sum["e"], window_sum["e"]),
                ("prompt tokens", human_tokens(today_sum["p"]), human_tokens(window_sum["p"])),
                ("completion tokens", human_tokens(today_sum["c"]), human_tokens(window_sum["c"])),
                ("总 tokens", human_tokens(today_sum["t"]), human_tokens(window_sum["t"])),
                ("缓存命中率", hit_rate(today_sum), hit_rate(window_sum)),
            ],
        )
        print()
        print("（按小时分桶累加；窗口起点 %s）" % str(usage.get("saved", "?"))[:16])
    except Exception as exc:  # noqa: BLE001
        print("今日用量采集失败：%s: %s" % (type(exc).__name__, exc))

    # 4.2 按账号：余额与累计
    print()
    try:
        status = _wb_get("/status")
        accounts = status.get("accounts", [])
        rows = []
        for account in accounts:
            tokens = account.get("token_usage", {}) or {}
            rows.append(
                (
                    account.get("nickname") or account.get("uid", "?")[:8],
                    "%.0f / %.0f" % (account.get("credits", 0), account.get("credits_total", 0)),
                    tokens.get("request_count", 0),
                    human_tokens(tokens.get("total_tokens", 0)),
                    account.get("success_count", 0),
                    account.get("err_total", 0),
                    "是" if account.get("cooling") else "否",
                    "是" if account.get("disabled") else "否",
                )
            )
        table(
            ["账号", "余额/总额", "累计请求", "累计 tokens", "成功", "失败", "冷却中", "已禁用"],
            rows,
        )
    except Exception as exc:  # noqa: BLE001
        print("账号状态采集失败：%s: %s" % (type(exc).__name__, exc))

    # 4.3 面板口径的总计
    print()
    try:
        panel = _wb_get("/panel/api/usage")
        totals = panel.get("totals", {})
        print(
            "面板口径：请求 %s，错误 %s，总 tokens %s，缓存命中率 %.2f%%，平均延迟 %.0fms"
            % (
                totals.get("requests", "?"),
                totals.get("errors", "?"),
                human_tokens(totals.get("total_tokens", 0)),
                totals.get("cache_hit_rate", 0.0),
                totals.get("avg_latency_ms", 0.0),
            )
        )
        print("（面板统计窗口起点 %s）" % str(panel.get("since", "?"))[:16])
    except Exception as exc:  # noqa: BLE001
        print("面板用量采集失败：%s: %s" % (type(exc).__name__, exc))


# ---- 五、请求明细与磁盘占用 ----------------------------------------------


def collect_request_logs():
    section("五、请求归档")
    today = datetime.now().strftime("%Y-%m-%d")
    log_dir = WB_DIR / "data" / "request-logs"
    try:
        files = sorted(log_dir.glob("requests-*.jsonl"))
        rows = []
        for path in files[-5:]:
            try:
                with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
                    lines = sum(1 for _ in fh)
            except Exception:
                lines = -1
            rows.append((path.name, lines, human_bytes(path.stat().st_size)))
        table(["归档文件", "行数", "大小"], rows)
        today_file = log_dir / ("requests-%s.jsonl" % today)
        print()
        print("今日归档：%s" % ("存在" if today_file.exists() else "尚未生成"))
    except Exception as exc:  # noqa: BLE001
        print("请求归档采集失败：%s: %s" % (type(exc).__name__, exc))


# ---- main -----------------------------------------------------------------


def main():
    now = datetime.now()
    print("# 服务器日报 %s" % now.strftime("%Y-%m-%d"))
    print()
    print("生成时间：%s ｜ 主机：%s" % (now.strftime("%Y-%m-%d %H:%M:%S"), socket.gethostname()))

    for collector in (
        collect_host,
        collect_services,
        collect_sites,
        collect_wb,
        collect_request_logs,
    ):
        try:
            collector()
        except Exception as exc:  # noqa: BLE001 - 单节失败不影响整封
            section(collector.__name__)
            print("本节采集失败：%s: %s" % (type(exc).__name__, exc))

    print()
    print("---")
    print("由 notify + daily-report.py 自动生成")


if __name__ == "__main__":
    main()
