"""JSONL 结构化日志：每次投递尝试一行。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional


def format_ts(ts: Optional[float] = None) -> str:
    """本地时间，形如 2026-10-16T20:00:00+0800。"""
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S%z", time.localtime(ts if ts is not None else time.time())
    )


def log_event(path, event: str, **fields) -> Dict[str, object]:
    record: Dict[str, object] = {"ts": format_ts(), "event": event}
    record.update(fields)
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(str(target), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def tail(path, n: int = 20) -> List[Dict[str, object]]:
    target = Path(path).expanduser()
    if not target.exists():
        return []
    if n <= 0:
        return []
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    records: List[Dict[str, object]] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            parsed = {"event": "unparsable", "raw": line}
        if isinstance(parsed, dict):
            records.append(parsed)
        else:
            records.append({"event": "unparsable", "raw": line})
    return records
