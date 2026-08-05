#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/../release/task18_lib.sh"

CONFIG_FILE="${MIM_CONFIG_FILE:-$(mim_task18_default_config_file "$SCRIPT_DIR")}"

mim_task18_require_no_args "$@"
SNAPSHOT_DIR=$(mktemp -d)
PLAN_PATH="$SCRIPT_DIR/.state/task18-edge-preflight-$$.json"
trap 'rm -rf "$SNAPSHOT_DIR"; rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"' EXIT
SNAPSHOT_CONFIG=$(mim_task18_snapshot_config "$SCRIPT_DIR" "$CONFIG_FILE" "$SNAPSHOT_DIR")

MIM_CONFIG_FILE="$SNAPSHOT_CONFIG" bash "$SCRIPT_DIR/plan.sh" --plan --out "$PLAN_PATH" >/dev/null
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
if plan.get("status") != "ready":
    blockers = plan.get("blockers") or []
    message = blockers[0]["message"] if blockers else "Edge reviewed plan is not ready"
    raise SystemExit(message)
PY

printf 'Edge preflight checks passed.\n'
