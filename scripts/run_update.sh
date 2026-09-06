#!/usr/bin/env bash
# Linux orchestrator, used by the scheduled cloud routine.
#
# Deliberately does NOT re-arm any schedule: the cloud run is driven by its own cron,
# and hta_schedule.py only exists to drive the Windows task on the user's PC.
#
# Prints NEW_MATCHES=<n> as its last line so the caller can decide whether a republish
# is warranted. Exits non-zero only if the pipeline genuinely failed.
set -uo pipefail

cd "$(dirname "$0")/.."
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

PY="${PYTHON:-python3}"

echo "== installing requirements =="
"$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt || {
    echo "FATAL: could not install requirements"; exit 2; }

echo "== fetch =="
if ! "$PY" scripts/hta_fetch.py; then
    echo "FATAL: hta_fetch.py failed"; exit 1
fi

echo "== aggregate =="
if ! "$PY" scripts/hta_aggregate.py; then
    echo "FATAL: hta_aggregate.py failed"; exit 1
fi

# Optional: fetches a photo for any newly signed player. A failure here only means an
# initials avatar, so it must never fail the run.
echo "== images =="
"$PY" scripts/hta_images.py || echo "note: image step skipped or partial"

echo "== build =="
if ! "$PY" scripts/hta_build.py; then
    echo "FATAL: hta_build.py failed"; exit 1
fi

# Advisory only - a Transfermarkt mismatch is reported, never fatal.
"$PY" scripts/hta_verify.py --quiet || echo "note: cross-check unavailable or mismatched"

NEW=$("$PY" - <<'PYEOF'
import json
try:
    with open("data/fetch_status.json", encoding="utf-8") as fh:
        print(len(json.load(fh).get("newly_ingested") or []))
except Exception:
    print(0)
PYEOF
)

echo "NEW_MATCHES=${NEW}"
