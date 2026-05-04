#!/usr/bin/env bash
# Reproduces oscar issue 4016: two exclusive vouchers can both be applied to
# the same basket when added in a particular order.
#
# Drives the actual user flow with Playwright + a real Django runserver, so
# AppMap's HTTP middleware records each browser-driven request as a normal
# request recording.
#
# Run via the wrappers (not directly):
#     bin/run-tests.sh        bin/repro.sh    # no AppMap recording
#     bin/record-appmap.sh    bin/repro.sh    # with AppMap recording
#
# Exit codes:
#   0  bug REPRODUCED (both vouchers attached, no error — buggy behavior)
#   1  bug NOT reproduced (second voucher rejected or error shown)
#   2  setup or HTTP error

set -euo pipefail
cd /app/sandbox

DB_FILE=/tmp/oscar_repro.sqlite3
export DATABASE_NAME="$DB_FILE"
export DATABASE_ENGINE=django.db.backends.sqlite3
export DJANGO_SETTINGS_MODULE=settings
# /app/src first so the mounted oscar wins over the image-baked /tmp/repo;
# /app/sandbox second so DJANGO_SETTINGS_MODULE=settings resolves.
export PYTHONPATH=/app/src:/app/sandbox:${PYTHONPATH:-}

rm -f "$DB_FILE"
python manage.py migrate --noinput >/dev/null 2>&1

# Start runserver in the background. --noreload so AppMap instrumentation
# stays loaded in the same process; redirect stderr so we can show it on
# failure but not pollute happy-path output.
SERVER_LOG=/tmp/oscar_repro_server.log
python manage.py runserver --noreload 127.0.0.1:8000 \
    >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

# Wait for the port to listen — at most 30s.
for i in $(seq 1 60); do
    if python -c "
import socket, sys
s = socket.socket()
try: s.connect(('127.0.0.1', 8000)); s.close(); sys.exit(0)
except OSError: sys.exit(1)
" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "runserver died before listening — log:" >&2
    tail -50 "$SERVER_LOG" >&2
    exit 2
fi

python /app/bin/repro.py
RC=$?

# Show server log if there was a failure for diagnosis.
if [[ $RC -eq 2 ]]; then
    echo ""
    echo "--- server log ---" >&2
    tail -50 "$SERVER_LOG" >&2
fi

exit $RC
