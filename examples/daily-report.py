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


# ---- 采集层 ---------------------------------------------------------------


def probe_port(port):
    """本机端口是否可连接。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except OSError:
        return False


def collect_host(runner=run):
    """主机资源。采集失败的字段留 None（不是 0）。"""
    data = {"disk_used": None, "disk_total": None, "disk_pct": None,
            "mem_used": None, "mem_total": None, "mem_pct": None,
            "load1": None, "load5": None, "load15": None,
            "uptime_s": None, "failed_units": None}

    code, out = runner(["df", "-P", "/"])
    if code == 0 and len(out.splitlines()) >= 2:
        parts = out.splitlines()[1].split()
        if len(parts) >= 5:
            try:
                data["disk_total"] = int(parts[1]) * 1024
                data["disk_used"] = int(parts[2]) * 1024
                data["disk_pct"] = int(parts[4].rstrip("%"))
            except ValueError:
                pass

    code, out = runner(["free", "-m"])
    if code == 0:
        for line in out.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                try:
                    total, used = int(parts[1]), int(parts[2])
                except (IndexError, ValueError):
                    break
                data["mem_total"], data["mem_used"] = total, used
                data["mem_pct"] = int(round(100.0 * used / total))
                break

    try:
        load1, load5, load15 = os.getloadavg()
        data["load1"], data["load5"], data["load15"] = load1, load5, load15
    except (OSError, AttributeError):
        pass

    try:
        with open("/proc/uptime", "r") as fh:
            data["uptime_s"] = int(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        pass

    code, out = runner(["systemctl", "--failed", "--no-legend", "--no-pager"])
    if code == 0:
        data["failed_units"] = [line.split()[0] for line in out.splitlines() if line.strip()]

    return data


# ---- 二、服务存活 ---------------------------------------------------------


def collect_services(runner=run, probe=probe_port):
    """进程 / systemd 单元 / 容器 / 端口。

    存活判定走进程名与端口，不走 systemd：nginx / mysqld 由宝塔直接拉起，
    它们的 LSB 脚本 is-active 恒为 inactive。docker 是真 systemd 单元。
    """
    data = {"procs": [], "units": [], "container": None, "ports": []}

    for process, label in PROCESSES:
        code, out = runner(["pgrep", "-x", process])
        count = len([line for line in out.splitlines() if line.strip()]) if code == 0 else 0
        data["procs"].append({"label": label, "count": count, "ok": count > 0})

    for unit in UNITS:
        code, out = runner(["systemctl", "is-active", unit])
        state = out or "unknown"
        data["units"].append({"label": unit, "state": state,
                              "ok": code == 0 and state == "active"})

    code, out = runner(["docker", "inspect", "-f",
                        "{{.State.Status}}|{{.State.StartedAt}}", "workbuddy2api"])
    if code == 0 and "|" in out:
        status, started = out.split("|", 1)
        data["container"] = {"name": "workbuddy2api", "status": status,
                             "started": started[:19], "ok": status == "running"}
    else:
        data["container"] = {"name": "workbuddy2api", "status": None,
                             "started": None, "ok": False, "error": "查询失败"}

    for port, label in PORTS:
        data["ports"].append({"port": port, "label": label, "ok": bool(probe(port))})

    return data


# ---- 三、网站可用性 -------------------------------------------------------


def collect_sites(opener=None):
    """站点可用性。SSL 证书错误单独归类，便于已知项白名单匹配。"""
    opener = opener or urllib.request.urlopen
    rows = []
    for label, url in SITES:
        started = time.time()
        entry = {"label": label, "url": url, "ok": False, "code": None,
                 "elapsed": None, "note": "", "known": False, "known_reason": None}
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "notify-daily-report"})
            with opener(request, timeout=HTTP_TIMEOUT) as response:
                code = response.status
                body = response.read(4096).decode("utf-8", "replace")
            entry["code"] = code
            entry["elapsed"] = time.time() - started
            entry["ok"] = 200 <= code < 400
            if "healthz" in url:
                try:
                    payload = json.loads(body)
                    entry["note"] = "健康账号 %s/%s" % (payload.get("healthy"),
                                                        payload.get("total"))
                except ValueError:
                    pass
        except urllib.error.HTTPError as exc:
            entry["code"] = exc.code
            entry["elapsed"] = time.time() - started
            entry["note"] = "HTTP %d" % exc.code
        except urllib.error.URLError as exc:
            entry["elapsed"] = time.time() - started
            reason = exc.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                entry["note"] = "证书不匹配"
            elif isinstance(reason, ssl.SSLError):
                entry["note"] = "TLS 错误"
            else:
                entry["note"] = str(reason)[:60]
        except Exception as exc:  # noqa: BLE001
            entry["note"] = type(exc).__name__
        entry["known_reason"] = known_reason(entry["label"], entry["note"])
        entry["known"] = entry["known_reason"] is not None
        rows.append(entry)
    return rows


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


def collect_requests(log_dir=None, today=None):
    """今日请求质量（单一来源：requests-<today>.jsonl）。"""
    log_dir = Path(log_dir) if log_dir else REQUEST_LOG_DIR
    today = today or datetime.now().strftime("%Y-%m-%d")
    rows, _bad = read_jsonl(log_dir / ("requests-%s.jsonl" % today))
    return requests_metrics(rows)


def collect_credits(getter=None):
    """账号余额（来自 workbuddy2api 的 /status）。"""
    getter = getter or _wb_get
    status = getter("/status")
    out = {}
    for account in (status.get("accounts") or []):
        name = account.get("nickname") or str(account.get("uid", "?"))[:8]
        out[name] = {"left": account.get("credits"), "total": account.get("credits_total")}
    return out or None


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
