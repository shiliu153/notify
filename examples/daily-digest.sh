#!/bin/sh
# 生成日报并发送。供 cron 调用：
#     0 8 * * * /opt/notify/examples/daily-digest.sh
#
# 环境变量：
#     NOTIFY_HOME      项目根，默认 /opt/notify
#     NOTIFY_PYTHON    指定解释器，默认自动挑 >= 3.7 的
#     NOTIFY_DEDUP_KEY 去重键，默认 daily-digest
set -u

NOTIFY_HOME="${NOTIFY_HOME:-/opt/notify}"
DEDUP_KEY="${NOTIFY_DEDUP_KEY:-daily-digest}"
OUT="${NOTIFY_DAILY_OUT:-/tmp/notify-daily-report.md}"
ERR="${NOTIFY_DAILY_OUT:-/tmp/notify-daily-report}.err"

# 与 bin/notify 同样的挑法：系统 python3 可能是 3.6
pick_python() {
    for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' >/dev/null 2>&1; then
                printf '%s' "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

if [ -n "${NOTIFY_PYTHON:-}" ]; then
    PYTHON="$NOTIFY_PYTHON"
elif PYTHON="$(pick_python)"; then
    :
else
    PYTHON=python3
fi

"$PYTHON" "$NOTIFY_HOME/examples/daily-report.py" > "$OUT" 2>"$ERR"
generated=$?

if [ "$generated" -ne 0 ] || [ ! -s "$OUT" ]; then
    # 日报生成失败也要发信说明，绝不静默
    {
        echo "# 服务器日报生成失败 $(date +%F)"
        echo
        echo "daily-report.py 退出码 $generated"
        echo
        echo '```'
        tail -20 "$ERR" 2>/dev/null
        echo '```'
    } > "$OUT"
fi

"$NOTIFY_HOME/bin/notify" send \
    --title "服务器日报 $(date +%F)" \
    --body-file "$OUT" \
    --dedup-key "$DEDUP_KEY"
