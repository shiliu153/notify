#!/usr/bin/env python3
"""为 shiliu153 这台 ECS 定制的日报生成器。

只做一件事：把当天的状态汇总成 markdown 打到 stdout，交给 notify 发送。

    python3.11 daily-report.py > /tmp/daily.md
    /opt/notify/bin/notify send --title "服务器日报 $(date +%F)" \
        --body-file /tmp/daily.md --dedup-key daily-digest

设计原则：
1. **任何一段采集失败都不许让整封日报消失**——每节独立 try，失败写一行原因。
2. **不误报**。这台机器上 nginx / mysqld 由宝塔直接拉起，systemd 的
   `nginx.service` / `mysqld.service` 是 LSB 脚本、`is-active` 恒为 inactive，
   所以存活判定走进程名与端口，不走 systemd（docker 是真 systemd 单元，除外）。
"""
from __future__ import annotations

import collections
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- 本机配置（可用环境变量覆盖） ----------------------------------------

WB_DIR = Path(os.environ.get("WB_DIR", "/www/dk_project/workbuddy2api"))
WB_BASE = os.environ.get("WB_BASE", "http://127.0.0.1:7863")

# 要巡检的站点：(显示名, URL)
SITES = [
    ("shiliu153.cn (http)", "http://shiliu153.cn/"),
    ("shiliu153.cn (https)", "https://shiliu153.cn/"),
    ("shiliu153.me (https)", "https://shiliu153.me/"),
    ("wb.shiliu153.me (https)", "https://wb.shiliu153.me/healthz"),
]

# 存活判定走进程名（宝塔托管的服务 systemd 状态不可信）
PROCESSES = [
    ("nginx", "nginx"),
    ("mysqld", "mysqld"),
    ("redis-server", "redis"),
]

# 真 systemd 单元
UNITS = ["docker"]

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

# ---- 硬故障阈值（克刻口径：只有这几类会进结论） --------------------------

DISK_HARD_PCT = 90
CERT_HARD_DAYS = 7
BACKUP_MAX_AGE_HOURS = 26

BACKUP_ROOT = Path("/www/backup/database")
CERT_CONF_DIR = Path("/www/server/panel/vhost/nginx")
SECURE_LOG = Path("/var/log/secure")
NGINX_LOG_DIR = Path("/www/wwwlogs")
REQUEST_LOG_DIR = WB_DIR / "data" / "request-logs"

SNAPSHOT_PATH = Path(os.environ.get("NOTIFY_STATS", "/var/lib/notify/daily-stats.jsonl"))
SNAPSHOT_RETAIN_DAYS = 400
CREDIT_WINDOW_DAYS = 7

# 已知项白名单：(站点显示名, 状态关键字, 原因)。命中者永不进硬故障。
KNOWN_ISSUES = [
    ("shiliu153.cn (https)", "证书不匹配", ".cn 正在备案中，暂不处理证书"),
]

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


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


def percentile(values, p):
    """nearest-rank 分位。会就地排序 values。空列表返回 None。"""
    if not values:
        return None
    values.sort()
    return values[min(len(values) - 1, int(len(values) * p))]


def delta(today_value, yesterday_value):
    """环比字符串。任一侧为 None 或基数为 0 → '—'。"""
    if today_value is None or yesterday_value is None:
        return "—"
    if yesterday_value == 0:
        return "—"
    return "%+.0f%%" % (100.0 * (today_value - yesterday_value) / yesterday_value)


def read_jsonl(path):
    """逐行解析 JSONL，返回 (rows, 坏行数)。文件缺失 → ([], 0)。"""
    rows = []
    bad = 0
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    bad += 1
    except OSError:
        return [], 0
    return rows, bad


def month_day_prefix(moment):
    """syslog 的 'Oct 16 ' 前缀（英文月份 + 空格填充日 + 尾随空格）。

    不用 strftime：%b 受 locale 影响，而日志固定是英文月份。
    尾随空格是必须的——否则 'Oct 1' 会匹配到 'Oct 16'。
    """
    return "%s %2d " % (MONTH_ABBR[moment.month - 1], moment.day)


def known_reason(label, note):
    """命中已知项白名单则返回原因，否则 None。"""
    for known_label, keyword, reason in KNOWN_ISSUES:
        if label == known_label and keyword in note:
            return reason
    return None


def is_known_issue(label, note):
    """是否命中已知项白名单。"""
    return known_reason(label, note) is not None


ISO_HOUR_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})")


def _hour_of(row):
    m = ISO_HOUR_RE.match(str(row.get("time") or ""))
    return int(m.group(4)) if m else None


def _hhmm_of(row):
    m = ISO_HOUR_RE.match(str(row.get("time") or ""))
    return "%s:%s" % (m.group(4), m.group(5)) if m else "—"


def _account_name(row):
    """'shiliu153(69737e40)' → 'shiliu153'。"""
    return str(row.get("account") or "?").split("(")[0]


def requests_metrics(rows):
    """把 requests-*.jsonl 的行汇总成日报指标。rows 为空 → None。"""
    if not rows:
        return None
    durations = [int(r.get("duration_ms") or 0) for r in rows]
    ttfbs = [int(r.get("ttfb_ms") or 0) for r in rows]
    failed = sum(1 for r in rows if r.get("ok") is False or (r.get("status") or 0) >= 400)
    retries = sum(1 for r in rows if (r.get("attempts") or 1) > 1)
    tokens_total = sum(int(r.get("total_tokens") or 0) for r in rows)
    hit = sum(int(r.get("cache_hit_tokens") or 0) for r in rows)
    miss = sum(int(r.get("cache_miss_tokens") or 0) for r in rows)

    hours = collections.Counter(h for h in (_hour_of(r) for r in rows) if h is not None)
    peak_hour, peak_count = hours.most_common(1)[0] if hours else (None, 0)
    slowest = max(rows, key=lambda r: int(r.get("duration_ms") or 0))

    return {
        "count": len(rows),
        "failed": failed,
        "p50": percentile(durations, 0.5),
        "p90": percentile(durations, 0.9),
        "p99": percentile(durations, 0.99),
        "max": max(durations),
        "ttfb_p50": percentile(ttfbs, 0.5),
        "ttfb_p90": percentile(ttfbs, 0.9),
        "retries": retries,
        "tokens_total": tokens_total,
        "cache_hit_pct": round(100.0 * hit / (hit + miss), 2) if (hit + miss) else None,
        "by_account": dict(collections.Counter(_account_name(r) for r in rows)),
        "by_model": dict(collections.Counter(str(r.get("model") or "?") for r in rows)),
        "by_ip": dict(collections.Counter(str(r.get("client_ip") or "?") for r in rows)),
        "by_ua": dict(collections.Counter(str(r.get("user_agent") or "?") for r in rows)),
        "peak_hour": peak_hour,
        "peak_hour_count": peak_count,
        "slowest": {"account": _account_name(slowest), "hhmm": _hhmm_of(slowest),
                    "ms": int(slowest.get("duration_ms") or 0)},
    }


def _write_snapshot_rows(rows, path):
    """按日期排序后原子写入快照文件。"""
    path = Path(path)
    ordered = sorted(rows, key=lambda r: str(r["date"]))
    text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in ordered)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(str(tmp), "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(str(tmp), str(path))


def load_snapshot(path):
    """读取快照行，按日期升序。文件缺失或损坏 → []。"""
    rows, _bad = read_jsonl(path)
    good = [r for r in rows if isinstance(r, dict) and r.get("date")]
    good.sort(key=lambda r: str(r["date"]))
    return good


def save_snapshot(metrics, path, today):
    """写入今天这一行：替换同日行、裁剪超期行、原子替换。

    任何失败都只打印一行警告——日报必须照发。
    """
    try:
        rows = [r for r in load_snapshot(path) if str(r.get("date")) != today]
        rows.append(metrics)
        cutoff = (datetime.strptime(today, "%Y-%m-%d")
                  - timedelta(days=SNAPSHOT_RETAIN_DAYS)).strftime("%Y-%m-%d")
        rows = [r for r in rows if str(r["date"]) >= cutoff]
        _write_snapshot_rows(rows, path)
    except Exception as exc:  # noqa: BLE001 - 快照失败不得影响日报
        print("快照写入失败：%s: %s" % (type(exc).__name__, exc))


def find_yesterday(rows, today):
    """快照里日期为今天前一天的那行；没有 → None。"""
    want = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    for row in rows:
        if str(row.get("date")) == want:
            return row
    return None


def find_credit_base(rows, today, days=CREDIT_WINDOW_DAYS):
    """最近 days 天内最早一条含余额记录的快照行；没有 → None。"""
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    candidates = [r for r in rows
                  if start <= str(r.get("date")) < today
                  and isinstance(r.get("credits"), dict) and r["credits"]]
    if not candidates:
        return None
    return min(candidates, key=lambda r: str(r["date"]))


def daily_consumption(base_row, today_row):
    """{账号: 日均消耗}。余额未下降、账号缺失、间隔非正 → 该账号不出现。"""
    if not base_row or not today_row:
        return {}
    try:
        gap = (datetime.strptime(str(today_row["date"]), "%Y-%m-%d")
               - datetime.strptime(str(base_row["date"]), "%Y-%m-%d")).days
    except (KeyError, ValueError):
        return {}
    if gap <= 0:
        return {}
    base = base_row.get("credits") or {}
    now = today_row.get("credits") or {}
    out = {}
    for name, entry in now.items():
        before = base.get(name)
        if not isinstance(before, dict) or not isinstance(entry, dict):
            continue
        left_before, left_now = before.get("left"), entry.get("left")
        if left_before is None or left_now is None:
            continue
        spent = (left_before - left_now) / float(gap)
        if spent > 0:
            out[name] = spent
    return out


BACKFILL_LOG_RE = re.compile(r"^requests-(\d{4}-\d{2}-\d{2})\.jsonl$")


def backfill_snapshot(path, log_dir, today):
    """把 jsonl 覆盖、但快照里没有的日期补成快照行。

    只填 requests 字段，其余留 null；已存在的日期不覆盖。返回新增行数。
    """
    try:
        log_dir = Path(log_dir)
        rows = load_snapshot(path)
        known = {str(r.get("date")) for r in rows}
        added = 0
        for log_path in sorted(log_dir.glob("requests-*.jsonl")):
            m = BACKFILL_LOG_RE.match(log_path.name)
            if not m:
                continue
            day = m.group(1)
            if day >= today or day in known:
                continue
            raw, _bad = read_jsonl(log_path)
            metrics = requests_metrics(raw)
            if metrics is None:
                continue
            rows.append({"date": day, "ts": None, "host": None, "services": None, "sites": None,
                         "requests": metrics, "credits": None, "certs": None, "backup": None,
                         "system": None, "hard_failures": [], "backfill": True})
            known.add(day)
            added += 1
        if added:
            _write_snapshot_rows(rows, path)
        return added
    except Exception as exc:  # noqa: BLE001 - 回填失败不得影响日报
        print("快照回填失败：%s: %s" % (type(exc).__name__, exc))
        return 0


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
            # df -P 的容量列单位是 1K 块
            total = int(parts[1]) * 1024
            used = int(parts[2]) * 1024
            rows.append(("磁盘 /", "%s / %s (%s)" % (human_bytes(used), human_bytes(total), parts[4])))
    else:
        rows.append(("磁盘 /", "采集失败"))

    code, out = run(["free", "-m"])
    if code == 0:
        for line in out.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                total, used, avail = int(parts[1]), int(parts[2]), int(parts[6])
                rows.append(
                    (
                        "内存",
                        "%dM / %dM (%.0f%%，可用 %dM)" % (used, total, 100.0 * used / total, avail),
                    )
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
        failed = [line.split()[0] for line in out.splitlines() if line.strip()]
        rows.append(("失败的 systemd 单元", "无" if not failed else "、".join(failed)))

    table(["项目", "值"], rows)


# ---- 二、服务存活 ---------------------------------------------------------


def collect_services():
    section("二、服务存活")
    rows = []

    for process, label in PROCESSES:
        code, out = run(["pgrep", "-x", process])
        count = len([line for line in out.splitlines() if line.strip()]) if code == 0 else 0
        rows.append((label, "✅ 运行中（%d 个进程）" % count if count else "❌ 未运行"))

    for unit in UNITS:
        code, out = run(["systemctl", "is-active", unit])
        state = out or "unknown"
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
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                rows.append((label, "⚠️ 证书不匹配", "域名不在证书 SAN 内"))
            elif isinstance(reason, ssl.SSLError):
                rows.append((label, "⚠️ TLS 错误", str(reason)[:60]))
            else:
                rows.append((label, "❌ 不可达", str(reason)[:60]))
        except Exception as exc:  # noqa: BLE001
            rows.append((label, "❌ 不可达", type(exc).__name__))
    table(["站点", "状态", "备注"], rows)


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

    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(str(WB_DIR / "data" / "usage.json"), "r", encoding="utf-8") as fh:
            usage = json.load(fh)
        buckets = usage.get("buckets", [])
        today_buckets = [b for b in buckets if str(b.get("s", "")).startswith("h:" + today)]
        today_sum = _sum_buckets(today_buckets)
        window_sum = _sum_buckets(buckets)

        def hit_rate(summary):
            denom = summary["ch"] + summary["cm"]
            return "%.2f%%" % (100.0 * summary["ch"] / denom) if denom else "—"

        table(
            ["指标", "今日", "累计（本机记录内）"],
            [
                ("请求数", today_sum["q"], window_sum["q"]),
                ("错误数", today_sum["e"], window_sum["e"]),
                ("prompt tokens", human_tokens(today_sum["p"]), human_tokens(window_sum["p"])),
                ("completion tokens", human_tokens(today_sum["c"]), human_tokens(window_sum["c"])),
                ("总 tokens", human_tokens(today_sum["t"]), human_tokens(window_sum["t"])),
                ("缓存命中率", hit_rate(today_sum), hit_rate(window_sum)),
            ],
        )
    except Exception as exc:  # noqa: BLE001
        print("今日用量采集失败：%s: %s" % (type(exc).__name__, exc))

    print()
    try:
        status = _wb_get("/status")
        rows = []
        for account in status.get("accounts", []):
            tokens = account.get("token_usage", {}) or {}
            rows.append(
                (
                    account.get("nickname") or str(account.get("uid", "?"))[:8],
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


# ---- 五、请求归档 ---------------------------------------------------------


def collect_request_logs():
    section("五、请求归档")
    log_dir = WB_DIR / "data" / "request-logs"
    today = datetime.now().strftime("%Y-%m-%d")
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
        print(
            "今日归档 requests-%s.jsonl：%s"
            % (today, "存在" if today_file.exists() else "尚未生成")
        )
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
