#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# shellcheck source=runtime_bootstrap_lib.sh
. "$SCRIPT_DIR/runtime_bootstrap_lib.sh"

INPUT_FILE="${MIM_RUNTIME_BOOTSTRAP_INPUT_FILE:-$(mim_rtb_default_input_file "$SCRIPT_DIR")}"
CONTRACT_PY=$(mim_rtb_contract_path "$SCRIPT_DIR")

MODE=
PLAN_OUT=
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --plan)
      MODE=plan
      shift
      ;;
    --out)
      [[ "$#" -ge 2 ]] || mim_rtb_fail "Missing value for --out"
      PLAN_OUT=$2
      shift 2
      ;;
    --*)
      mim_rtb_fail "Unknown argument: $1"
      ;;
    *)
      mim_rtb_fail "Positional arguments are not supported"
      ;;
  esac
done

[[ "$MODE" == "plan" && -n "$PLAN_OUT" ]] || mim_rtb_fail "Usage: plan.sh --plan --out .state/<name>.json"
mim_rtb_assert_plan_create_path "$SCRIPT_DIR" "$PLAN_OUT"
mim_rtb_assert_private_regular_file "Bootstrap input file" "$INPUT_FILE"
command -v gcloud >/dev/null 2>&1 || mim_rtb_fail "gcloud CLI is required"
command -v python3 >/dev/null 2>&1 || mim_rtb_fail "python3 is required"
[[ -f "$CONTRACT_PY" ]] || mim_rtb_fail "Bootstrap contract helper is required"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
CANONICAL_FILE="$TMP_DIR/canonical.json"
SUMMARY_FILE="$TMP_DIR/summary.json"
SECRET_JSON="$TMP_DIR/secret.json"
VERSIONS_JSON="$TMP_DIR/versions.json"
METADATA_JSON="$TMP_DIR/current-secret-metadata.json"

python3 "$CONTRACT_PY" validate \
  --input "$INPUT_FILE" \
  --canonical-output "$CANONICAL_FILE" \
  --summary-output "$SUMMARY_FILE"
chmod 600 "$CANONICAL_FILE" "$SUMMARY_FILE"

OPERATOR_EMAIL=$(python3 - "$SUMMARY_FILE" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["operator_email"])
PY
)
PROJECT_ID=$(python3 - "$SUMMARY_FILE" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["project_id"])
PY
)
INPUT_SHA256=$(python3 - "$SUMMARY_FILE" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["input_sha256"])
PY
)
GENERATED_AT="${MIM_RUNTIME_BOOTSTRAP_PLAN_GENERATED_AT:-$(mim_rtb_now_epoch)}"
EXPIRES_AT="${MIM_RUNTIME_BOOTSTRAP_PLAN_EXPIRES_AT:-$((GENERATED_AT + MIM_RTB_PLAN_MAX_AGE_SECONDS))}"

SECRET_STATE=$(mim_rtb_gcloud_optional_output \
  "Unable to inspect the runtime bootstrap secret" \
  "$SECRET_JSON" \
  secrets describe "$MIM_RTB_TARGET_SECRET_NAME" \
  '--format=json' \
  --account="$OPERATOR_EMAIL" \
  --project="$PROJECT_ID")

STATUS=ready
if [[ "$SECRET_STATE" == "exists" ]]; then
  mim_rtb_gcloud_capture \
    "Unable to inspect runtime bootstrap secret versions" \
    secrets versions list \
    --secret="$MIM_RTB_TARGET_SECRET_NAME" \
    '--format=json' \
    --account="$OPERATOR_EMAIL" \
    --project="$PROJECT_ID" >"$VERSIONS_JSON"
  python3 "$CONTRACT_PY" normalize-secret-metadata \
    --secret-json "$SECRET_JSON" \
    --versions-json "$VERSIONS_JSON" \
    --output "$METADATA_JSON"
else
  STATUS=blocked
  printf '{"secret":null,"versions":[],"latest_enabled_version":null}\n' >"$METADATA_JSON"
fi

python3 - "$SUMMARY_FILE" "$METADATA_JSON" "$PLAN_OUT" "$STATUS" "$GENERATED_AT" "$EXPIRES_AT" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
metadata = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
plan_path = Path(sys.argv[3])
status = sys.argv[4]
generated_at = int(sys.argv[5])
expires_at = int(sys.argv[6])
blocked = status != "ready"
payload = {
    "schema_version": 1,
    "target_secret_name": "mim-runtime-bootstrap",
    "target_secret_resource": (
        "projects/mim-prod-123456/secrets/mim-runtime-bootstrap"
    ),
    "operator_account": summary["operator_email"],
    "input_sha256": summary["input_sha256"],
    "generated_at_epoch": generated_at,
    "expires_at_epoch": expires_at,
    "status": status,
    "blockers": (
        [
            {
                "code": "missing-target-secret",
                "message": "central infra must create the secret container before bootstrap apply.",
            }
        ]
        if blocked
        else []
    ),
    "actions": (
        []
        if blocked
        else [
            {
                "kind": "add_secret_version",
                "target_secret_name": "mim-runtime-bootstrap",
            }
        ]
    ),
    "current_secret_metadata": metadata,
}
plan_path.write_text(
    json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
chmod 600 "$PLAN_OUT"
mim_rtb_write_hash_sidecar "$PLAN_OUT"
printf 'Wrote reviewed plan to %s\n' "$PLAN_OUT"
