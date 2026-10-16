#!/usr/bin/env python3
"""为 shiliu153 这台 ECS 定制的日报生成器。

只做一件事：把当天的状态汇总成 markdown 打到 stdout，交给 notify 发送。

    python3.11 daily-report.py > /tmp/daily.md
    /opt/notify/bin/notify send --title "服务器日报 $(date +%F)" \
        --body-file /tmp/daily.md --dedup-key daily-digest

设计原则：
1. **结论置顶**。先全采（collector 返回 dict、不打印），再判定硬故障，
   最后一次性渲染——否则结论层无法置顶。
2. **任何一段采集失败都不许让整封日报消失**——每节独立 try，失败写一行原因。
3. **结论层只报硬故障**（进程/端口/容器、站点不可用、磁盘满、备份超期、证书临期）；
   其余全部进运营明细，明细里不加告警标记。
4. **不误报**。nginx / mysqld 由宝塔直接拉起，systemd 的 `nginx.service` /
   `mysqld.service` 是 LSB 脚本、`is-active` 恒为 inactive，所以存活判定走
   进程名与端口，不走 systemd（docker 是真 systemd 单元，除外）。
   同理，journal 的错误按 unit 分类计数——84 行里 82 行是 sshd 的公网扫描噪音。
5. **环比靠快照**（/var/lib/notify/daily-stats.jsonl，一行一天）。快照写入失败
   只打印一行警告，绝不影响日报。
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


OPENSSL_DATE_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{4})")


def parse_openssl_date(text):
    """'Jan  2 09:30:03 2027 GMT' → naive UTC datetime。无法解析 → None。"""
    m = OPENSSL_DATE_RE.match(str(text or "").strip())
    if not m or m.group(1) not in MONTH_ABBR:
        return None
    return datetime(int(m.group(6)), MONTH_ABBR.index(m.group(1)) + 1, int(m.group(2)),
                    int(m.group(3)), int(m.group(4)), int(m.group(5)))


def collect_backup(root=None, runner=run, now=None):
    """最新数据库备份的年龄。

    用年龄而非"日期==今天"：日报 22:00 跑、备份 03:30 跑，但凌晨手工重跑时
    "今天还没备份"是正常的，不该报警。
    """
    root = Path(root) if root else BACKUP_ROOT
    now = now if now is not None else time.time()
    code, out = runner(["find", str(root), "-type", "f", "-name", "*.gz",
                        "-printf", "%T@ %s %p\n"])
    if code != 0:
        return None
    newest = None
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            mtime, size = float(parts[0]), int(parts[1])
        except ValueError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, size, parts[2])
    if newest is None:
        return {"latest": None, "age_hours": None, "ok": False, "size": None}
    age_hours = (now - newest[0]) / 3600.0
    return {"latest": datetime.fromtimestamp(newest[0]).strftime("%Y-%m-%dT%H:%M:%S"),
            "age_hours": round(age_hours, 1),
            "ok": age_hours <= BACKUP_MAX_AGE_HOURS,
            "size": newest[1]}


CERT_LINE_RE = re.compile(r"^\s*ssl_certificate\s+(\S+?);")


def collect_certs(conf_dir=None, runner=run, now=None):
    """从 nginx vhost 配置里取证书路径，逐个读到期日（剩余天数）。"""
    conf_dir = Path(conf_dir) if conf_dir else CERT_CONF_DIR
    now = now if now is not None else time.time()
    paths = []
    for conf in sorted(conf_dir.glob("*.conf")):
        try:
            text = conf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = CERT_LINE_RE.match(line)
            if m and m.group(1) not in paths:
                paths.append(m.group(1))
    if not paths:
        return None

    today = datetime.fromtimestamp(now, timezone.utc).date()
    result = {}
    for path in paths:
        code, out = runner(["openssl", "x509", "-in", path, "-noout", "-enddate", "-subject"])
        if code != 0:
            result[path] = None
            continue
        domain, enddate = None, None
        for line in out.splitlines():
            if line.startswith("notAfter="):
                enddate = line.split("=", 1)[1].strip()
            elif line.startswith("subject="):
                m = re.search(r"CN\s*=\s*([^\s,]+)", line)
                if m:
                    domain = m.group(1)
        expires = parse_openssl_date(enddate) if enddate else None
        result[domain or path] = (expires.date() - today).days if expires else None
    return result


JOURNAL_UNIT_RE = re.compile(r"^\S+\s+\d+\s+\S+\s+\S+\s+([A-Za-z0-9_.@-]+)\[")


def collect_journal(runner=run):
    """按 unit 分类统计 journal 的 err 级日志。

    不按行数报警：这台机器上 84 行里 82 行是 sshd 的公网扫描噪音。
    """
    code, out = runner(["journalctl", "--since", "today", "-p", "err", "--no-pager"])
    if code != 0:
        return None
    by_unit = {}
    total = 0
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        total += 1
        m = JOURNAL_UNIT_RE.match(stripped)
        unit = m.group(1) if m else "other"
        by_unit[unit] = by_unit.get(unit, 0) + 1
    return {"total": total, "by_unit": by_unit, "sshd_scan": by_unit.get("sshd", 0)}


def collect_ssh(log_path=None, today=None):
    """今日 SSH 登录统计（/var/log/secure）。"""
    log_path = Path(log_path) if log_path else SECURE_LOG
    today = today or datetime.now()
    prefix = month_day_prefix(today)
    failed = accepted = 0
    try:
        with open(str(log_path), "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.startswith(prefix):
                    continue
                if "Failed password" in line or "Invalid user" in line:
                    failed += 1
                elif "Accepted " in line:
                    accepted += 1
    except OSError:
        return None
    return {"failed": failed, "accepted": accepted}


def collect_nginx_errors(log_dir=None, today=None):
    """今日 nginx error log 新增行数。

    日期格式是 nginx 自己的 2026/10/16，不是 syslog 的 16/Oct/2026。
    """
    log_dir = Path(log_dir) if log_dir else NGINX_LOG_DIR
    today = today or datetime.now()
    needle = "%04d/%02d/%02d" % (today.year, today.month, today.day)
    total = 0
    files = 0
    for path in sorted(log_dir.glob("*.error.log")):
        files += 1
        try:
            with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if needle in line:
                        total += 1
        except OSError:
            continue
    return {"today_lines": total, "files": files}


def count_checks(cert_count):
    """硬故障巡检项总数。"""
    return (len(PROCESSES) + len(UNITS) + 1 + len(PORTS) + len(SITES)
            + 1 + 1 + cert_count)


def count_service_checks(metrics):
    """服务类巡检的 (正常数, 总数)，即第一节"进程 · 端口 · 容器"那一行。"""
    services = metrics.get("services") or {}
    procs = services.get("procs") or []
    units = services.get("units") or []
    ports = services.get("ports") or []
    container = services.get("container")
    total = len(procs) + len(units) + len(ports) + (1 if container else 0)
    ok = (sum(1 for p in procs if p["ok"]) + sum(1 for u in units if u["ok"])
          + sum(1 for p in ports if p["ok"])
          + (1 if container and container["ok"] else 0))
    return ok, total


def count_known(metrics):
    """已知项条数。"""
    return sum(1 for site in (metrics.get("sites") or []) if site.get("known"))


def hard_failures(metrics):
    """硬故障清单（克刻口径）。

    判据自身采集失败（字段为 None）时**不进清单**：宁可漏报一次，
    也不要因为采集器偶发抽风而天天误报。
    """
    problems = []

    services = metrics.get("services")
    if services:
        for proc in (services.get("procs") or []):
            if not proc["ok"]:
                problems.append("进程 %s 未运行" % proc["label"])
        for unit in (services.get("units") or []):
            if not unit["ok"]:
                problems.append("systemd 单元 %s 非 active（%s）" % (unit["label"], unit["state"]))
        container = services.get("container")
        if container and not container["ok"]:
            problems.append("容器 %s 非 running" % container["name"])
        for port in (services.get("ports") or []):
            if not port["ok"]:
                problems.append("端口 %d（%s）未监听" % (port["port"], port["label"]))

    for site in (metrics.get("sites") or []):
        if not site["ok"] and not site.get("known"):
            problems.append("站点 %s 不可用（%s）" % (site["label"], site.get("note") or "原因未知"))

    host = metrics.get("host")
    if host and host.get("disk_pct") is not None and host["disk_pct"] >= DISK_HARD_PCT:
        problems.append("磁盘 / 已用 %d%%（阈值 %d%%）" % (host["disk_pct"], DISK_HARD_PCT))

    backup = metrics.get("backup")
    if backup and not backup["ok"]:
        if backup.get("age_hours") is None:
            problems.append("找不到任何数据库备份")
        else:
            problems.append("最新备份已 %.1f 小时前（阈值 %d 小时）"
                            % (backup["age_hours"], BACKUP_MAX_AGE_HOURS))

    for domain, days in (metrics.get("certs") or {}).items():
        if days is None:
            problems.append("证书 %s 无法读取到期时间" % domain)
        elif days <= CERT_HARD_DAYS:
            problems.append("证书 %s 仅剩 %d 天" % (domain, days))

    return problems


def table_lines(headers, rows):
    """markdown 表格行（不打印）。"""
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "|" + "|".join([" --- "] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return out


def backup_line(backup):
    """第一节的备份现状。"""
    if not backup:
        return "采集失败"
    if backup.get("age_hours") is None:
        return "❌ 找不到任何备份"
    mark = "✅" if backup["ok"] else "❌"
    when = str(backup.get("latest") or "")[:16].replace("T", " ")
    return "%s %s（%s，阈值 %d 小时）" % (mark, when,
                                        human_bytes(backup.get("size") or 0),
                                        BACKUP_MAX_AGE_HOURS)


def certs_line(certs):
    """第一节的证书现状。"""
    if not certs:
        return "采集失败"
    parts = ["%s 剩 %s 天" % (domain, days if days is not None else "?")
             for domain, days in sorted(certs.items())]
    return " ｜ ".join(parts) + "（阈值 %d 天）" % CERT_HARD_DAYS


def render_conclusion(metrics, problems):
    """顶部那一行结论。"""
    if problems:
        return "❌ %d 项硬故障" % len(problems)
    total = count_checks(len(metrics.get("certs") or {}))
    known = count_known(metrics)
    if known:
        return "✅ 无硬故障 ｜ %d 项巡检（%d 项已知）" % (total, known)
    return "✅ 无硬故障 ｜ %d 项巡检" % total


def render_hard_checks(metrics, problems):
    """第一节：硬故障巡检。"""
    lines = ["## 一、硬故障巡检", ""]
    if problems:
        lines.append("**硬故障**")
        lines.append("")
        for problem in problems:
            lines.append("- ❌ %s" % problem)
        lines.append("")

    ok, total = count_service_checks(metrics)
    sites = metrics.get("sites") or []
    host = metrics.get("host") or {}
    sites_ok = sum(1 for site in sites if site.get("ok"))
    rows = [
        ("进程 · 端口 · 容器", "%d / %d 正常" % (ok, total) if total else "采集失败"),
        ("站点可用性", "%d / %d 可达（%d 项已知）" % (sites_ok, len(sites), count_known(metrics))
            if sites else "采集失败"),
        ("磁盘 /", "%d%%（阈值 %d%%）" % (host["disk_pct"], DISK_HARD_PCT)
            if host.get("disk_pct") is not None else "采集失败"),
        ("MySQL 备份", backup_line(metrics.get("backup"))),
        ("站点证书", certs_line(metrics.get("certs"))),
    ]
    lines.extend(table_lines(["判据", "现状"], rows))

    known = [site for site in sites if site.get("known")]
    if known:
        lines.append("")
        lines.append("> 已知项（不报警）：%s"
                     % "；".join("%s %s —— %s" % (site["url"], site["note"], site["known_reason"])
                                 for site in known))
    return lines


def _human_or_dash(value):
    return "—" if value is None else human_tokens(value)


def _ms(value):
    return "—" if value is None else "%.1fs" % (value / 1000.0)


def _pct(value):
    return "—" if value is None else "%.2f%%" % value


def _dist(counts, limit=3):
    if not counts:
        return "—"
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return " ｜ ".join("%s %d" % (name, count) for name, count in items)


def _yesterday_requests(history):
    return (history.get("yesterday") or {}).get("requests") or {}


def render_requests(metrics, history):
    """运营明细：请求质量。"""
    lines = []
    today = metrics.get("requests")
    if not today:
        lines.append("### 请求质量")
        lines.append("")
        lines.append("今日请求归档尚未生成或采集失败。")
        return lines

    yesterday = _yesterday_requests(history)
    lines.append("### 请求质量（今日 %d 次）" % today["count"])
    lines.append("")
    rows = [
        ("请求数", today["count"], yesterday.get("count", "—"),
         delta(today["count"], yesterday.get("count"))),
        ("失败数", today["failed"], yesterday.get("failed", "—"),
         delta(today["failed"], yesterday.get("failed"))),
        ("延迟 P50 / P90 / P99",
         "%s / %s / %s" % (_ms(today["p50"]), _ms(today["p90"]), _ms(today["p99"])),
         "%s / %s / %s" % (_ms(yesterday.get("p50")), _ms(yesterday.get("p90")),
                           _ms(yesterday.get("p99"))), "—"),
        ("TTFB P50 / P90", "%s / %s" % (_ms(today["ttfb_p50"]), _ms(today["ttfb_p90"])),
         "%s / %s" % (_ms(yesterday.get("ttfb_p50")), _ms(yesterday.get("ttfb_p90"))), "—"),
        ("重试", "%d 次（%.2f%%）" % (today["retries"],
                                    100.0 * today["retries"] / today["count"]),
         yesterday.get("retries", "—"),
         delta(today["retries"], yesterday.get("retries"))),
        ("总 tokens", human_tokens(today["tokens_total"]),
         _human_or_dash(yesterday.get("tokens_total")),
         delta(today["tokens_total"], yesterday.get("tokens_total"))),
        ("缓存命中率", _pct(today["cache_hit_pct"]), _pct(yesterday.get("cache_hit_pct")), "—"),
    ]
    lines.extend(table_lines(["指标", "今日", "昨日", "变化"], rows))
    lines.append("")

    slowest = today["slowest"]
    lines.append("- 最慢一条：%s（%s，%s）" % (_ms(slowest["ms"]), slowest["account"],
                                              slowest["hhmm"]))
    lines.append("- 账号：%s" % _dist(today["by_account"]))
    lines.append("- 模型：%s" % _dist(today["by_model"]))
    lines.append("- 来源：%s（%s）" % (_dist(today["by_ip"]), _dist(today["by_ua"])))
    if today.get("peak_hour") is not None:
        lines.append("- 峰值时段：%02d:00–%02d:00（%d 次）"
                     % (today["peak_hour"], (today["peak_hour"] + 1) % 24,
                        today["peak_hour_count"]))
    return lines


def _credit_sort_key(credits, rates, name):
    entry = credits.get(name) or {}
    left = entry.get("left")
    rate = rates.get(name)
    if rate and left is not None:
        return (0, int(left / rate), name)
    return (1, 0, name)


def _num(value):
    if value is None:
        return "?"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def render_credits(metrics, history):
    """运营明细：额度。按可撑天数升序（最紧的在前）。"""
    lines = ["### 额度", ""]
    credits = metrics.get("credits")
    if not credits:
        lines.append("账号余额采集失败。")
        return lines

    base = history.get("credit_base")
    today_row = {"date": history.get("today"), "credits": credits}
    rates = daily_consumption(base, today_row) if base else {}

    rows = []
    for name in sorted(credits, key=lambda n: _credit_sort_key(credits, rates, n)):
        entry = credits.get(name) or {}
        left = entry.get("left")
        rate = rates.get(name)
        rows.append((name,
                     "—" if left is None else "%s / %s" % (_num(left), _num(entry.get("total"))),
                     "—" if rate is None else "%.1f" % rate,
                     "—" if (not rate or left is None) else "%d" % int(left / rate)))
    lines.extend(table_lines(["账号", "余额", "日均消耗", "可撑天数"], rows))
    if not base:
        lines.append("")
        lines.append("> 日均消耗需要至少两条余额记录，明天起可用。")
    return lines


def render_system(metrics, history, failures):
    """运营明细：系统。"""
    lines = ["### 系统", ""]
    host = metrics.get("host")
    yesterday_host = (history.get("yesterday") or {}).get("host") or {}

    if host and host.get("mem_pct") is not None:
        mem = "内存 %d%%" % host["mem_pct"]
        if yesterday_host.get("mem_pct") is not None:
            mem += "（昨日 %d%%）" % yesterday_host["mem_pct"]
    else:
        mem = "内存 采集失败"
    if host and host.get("load1") is not None:
        load = "负载 %.2f / %.2f / %.2f" % (host["load1"], host["load5"], host["load15"])
    else:
        load = "负载 采集失败"
    disk = ("磁盘 %d%%" % host["disk_pct"]) if host and host.get("disk_pct") is not None \
        else "磁盘 采集失败"
    lines.append("- %s ｜ %s ｜ %s" % (mem, load, disk))

    journal = metrics.get("journal")
    if journal:
        parts = ["%s %d" % (unit, count) for unit, count in
                 sorted(journal["by_unit"].items(), key=lambda kv: (-kv[1], kv[0]))]
        lines.append("- journal 错误 %d 行：%s" % (journal["total"], " ｜ ".join(parts) or "无"))
    else:
        lines.append("- journal 错误 采集失败")

    nginx_errors = metrics.get("nginx_errors")
    lines.append("- nginx error log 今日 %s 行"
                 % (nginx_errors["today_lines"] if nginx_errors else "采集失败"))

    ssh = metrics.get("ssh")
    lines.append("- SSH：失败 %s 次 ｜ 成功 %s 次"
                 % ((ssh["failed"], ssh["accepted"]) if ssh else ("采集失败", "采集失败")))

    if host and host.get("failed_units"):
        lines.append("- 失败的 systemd 单元：%s" % "、".join(host["failed_units"]))
    if host and host.get("uptime_s") is not None:
        days, remainder = divmod(int(host["uptime_s"]), 86400)
        lines.append("- 运行时长：%d 天 %d 小时" % (days, remainder // 3600))
    if failures:
        lines.append("- 采集失败的节：%s" % "、".join(sorted(failures)))
    return lines


def render_ops(metrics, history, failures):
    """第二节：运营明细。"""
    lines = ["## 二、运营明细", ""]
    lines.extend(render_requests(metrics, history))
    lines.append("")
    lines.extend(render_credits(metrics, history))
    lines.append("")
    lines.extend(render_system(metrics, history, failures))
    return lines


def render(metrics, history, problems, failures, now=None, hostname=None):
    """渲染完整日报（markdown）。"""
    now = now or datetime.now()
    hostname = hostname or socket.gethostname()
    host = metrics.get("host") or {}

    lines = ["# 服务器日报 %s" % now.strftime("%Y-%m-%d"), ""]
    lines.append(render_conclusion(metrics, problems))
    uptime = ""
    if host.get("uptime_s") is not None:
        uptime = " ｜ 已运行 %d 天" % (int(host["uptime_s"]) // 86400)
    lines.append("生成时间 %s ｜ 主机 %s%s"
                 % (now.strftime("%Y-%m-%d %H:%M:%S"), hostname, uptime))
    lines.append("")
    lines.extend(render_hard_checks(metrics, problems))
    lines.append("")
    lines.extend(render_ops(metrics, history, failures))
    lines.append("")
    lines.append("---")
    lines.append("由 notify + daily-report.py 自动生成")
    return "\n".join(lines) + "\n"


# ---- main -----------------------------------------------------------------


COLLECTORS = [
    ("host", collect_host),
    ("services", collect_services),
    ("sites", collect_sites),
    ("backup", collect_backup),
    ("certs", collect_certs),
    ("requests", collect_requests),
    ("credits", collect_credits),
    ("journal", collect_journal),
    ("ssh", collect_ssh),
    ("nginx_errors", collect_nginx_errors),
]


def collect_all(collectors=None, failures=None):
    """跑完全部采集器。单个失败记 None，不中断其余。"""
    collectors = COLLECTORS if collectors is None else collectors
    metrics = {}
    for name, func in collectors:
        try:
            metrics[name] = func()
        except Exception as exc:  # noqa: BLE001 - 单节失败不影响整封
            metrics[name] = None
            if failures is not None:
                failures[name] = "%s: %s" % (type(exc).__name__, exc)
    return metrics


def build_snapshot_row(metrics, problems, today):
    """把今天的采集结果压成快照的一行。"""
    journal = metrics.get("journal") or {}
    ssh = metrics.get("ssh") or {}
    nginx_errors = metrics.get("nginx_errors") or {}
    return {
        "date": today,
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "host": metrics.get("host"),
        "services": metrics.get("services"),
        "sites": [{"label": site["label"], "ok": site["ok"], "code": site.get("code"),
                   "elapsed": site.get("elapsed"), "known": site.get("known")}
                  for site in (metrics.get("sites") or [])],
        "requests": metrics.get("requests"),
        "credits": metrics.get("credits"),
        "certs": metrics.get("certs"),
        "backup": metrics.get("backup"),
        "system": {"journal_total": journal.get("total"),
                   "journal_by_unit": journal.get("by_unit"),
                   "ssh_failed": ssh.get("failed"),
                   "ssh_accepted": ssh.get("accepted"),
                   "nginx_err_lines": nginx_errors.get("today_lines")},
        "hard_failures": problems,
        "backfill": False,
    }


def main(argv=None, collectors=None, stats_path=None, out=None, today=None, log_dir=None):
    """生成日报 → 写 stdout → 更新快照。始终返回 0。"""
    out = out or sys.stdout
    stats_path = Path(stats_path) if stats_path else SNAPSHOT_PATH
    log_dir = Path(log_dir) if log_dir else REQUEST_LOG_DIR
    today = today or datetime.now().strftime("%Y-%m-%d")

    failures = {}
    metrics = collect_all(collectors, failures)

    backfill_snapshot(stats_path, log_dir, today)

    rows = load_snapshot(stats_path)
    yesterday = find_yesterday(rows, today)
    base = find_credit_base(rows, today)
    gap = 0
    if base:
        try:
            gap = (datetime.strptime(today, "%Y-%m-%d")
                   - datetime.strptime(str(base["date"]), "%Y-%m-%d")).days
        except (KeyError, ValueError):
            gap = 0
    history = {"today": today, "yesterday": yesterday,
               "credit_base": base, "credit_gap_days": gap}

    problems = hard_failures(metrics)
    out.write(render(metrics, history, problems, failures))

    save_snapshot(build_snapshot_row(metrics, problems, today), stats_path, today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
