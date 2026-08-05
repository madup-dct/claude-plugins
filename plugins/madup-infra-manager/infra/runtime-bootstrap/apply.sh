#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# shellcheck source=runtime_bootstrap_lib.sh
. "$SCRIPT_DIR/runtime_bootstrap_lib.sh"

INPUT_FILE="${MIM_RUNTIME_BOOTSTRAP_INPUT_FILE:-$(mim_rtb_default_input_file "$SCRIPT_DIR")}"
CONTRACT_PY=$(mim_rtb_contract_path "$SCRIPT_DIR")

MODE=
PLAN_FILE=
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --apply)
      MODE=apply
      shift
      ;;
    --plan-file)
      [[ "$#" -ge 2 ]] || mim_rtb_fail "Missing value for --plan-file"
      PLAN_FILE=$2
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

[[ "$MODE" == "apply" && -n "$PLAN_FILE" ]] || mim_rtb_fail "Usage: apply.sh --apply --plan-file .state/<name>.json"
mim_rtb_assert_plan_read_path "$SCRIPT_DIR" "$PLAN_FILE"
mim_rtb_validate_plan_hash_and_age "$PLAN_FILE"
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
CURRENT_METADATA_JSON="$TMP_DIR/current-secret-metadata.json"
PLAN_METADATA_JSON="$TMP_DIR/plan-secret-metadata.json"

python3 "$CONTRACT_PY" validate \
  --input "$INPUT_FILE" \
  --canonical-output "$CANONICAL_FILE" \
  --summary-output "$SUMMARY_FILE"
chmod 600 "$CANONICAL_FILE" "$SUMMARY_FILE"

PLAN_STATUS=$(python3 - "$PLAN_FILE" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["status"])
PY
)
[[ "$PLAN_STATUS" == "ready" ]] || mim_rtb_fail "Reviewed plan contains blockers"

PLAN_INPUT_SHA=$(python3 - "$PLAN_FILE" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["input_sha256"])
PY
)
CURRENT_INPUT_SHA=$(python3 - "$SUMMARY_FILE" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["input_sha256"])
PY
)
[[ "$PLAN_INPUT_SHA" == "$CURRENT_INPUT_SHA" ]] || mim_rtb_fail "Input SHA-256 does not match the reviewed plan"

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

python3 - "$PLAN_FILE" "$PLAN_METADATA_JSON" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
Path(sys.argv[2]).write_text(
    json.dumps(plan["current_secret_metadata"], sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY

SECRET_STATE=$(mim_rtb_gcloud_optional_output \
  "Unable to inspect the runtime bootstrap secret" \
  "$SECRET_JSON" \
  secrets describe "$MIM_RTB_TARGET_SECRET_NAME" \
  '--format=json' \
  --account="$OPERATOR_EMAIL" \
  --project="$PROJECT_ID")
[[ "$SECRET_STATE" == "exists" ]] || mim_rtb_fail "Discovery drift detected"

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
  --output "$CURRENT_METADATA_JSON"

cmp -s "$PLAN_METADATA_JSON" "$CURRENT_METADATA_JSON" || mim_rtb_fail "Discovery drift detected"

ADDED_VERSION_NAME=$(mim_rtb_gcloud_capture \
  "Unable to add runtime bootstrap secret version" \
  secrets versions add "$MIM_RTB_TARGET_SECRET_NAME" \
  --data-file="$CANONICAL_FILE" \
  '--format=value(name)' \
  --impersonate-service-account="$MIM_RTB_RELEASE_SERVICE_ACCOUNT" \
  --account="$OPERATOR_EMAIL" \
  --project="$PROJECT_ID")

[[ "$ADDED_VERSION_NAME" =~ ^projects/${MIM_RTB_CENTRAL_PROJECT_ID}/secrets/${MIM_RTB_TARGET_SECRET_NAME}/versions/[1-9][0-9]*$ ]] || mim_rtb_fail "Added secret version response is invalid"
ADDED_VERSION_NUMBER=${ADDED_VERSION_NAME##*/}

READBACK_JSON="$TMP_DIR/readback.json"
mim_rtb_gcloud_capture \
  "Unable to read back the added runtime bootstrap secret version" \
  secrets versions describe "$ADDED_VERSION_NUMBER" \
  --secret="$MIM_RTB_TARGET_SECRET_NAME" \
  '--format=json' \
  --account="$OPERATOR_EMAIL" \
  --project="$PROJECT_ID" >"$READBACK_JSON"

if ! python3 - "$READBACK_JSON" "$ADDED_VERSION_NAME" <<'PY'
import json, sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_name = sys.argv[2]
if payload.get("name") != expected_name or payload.get("state") != "ENABLED":
    raise SystemExit(1)
PY
then
  mim_rtb_fail "Added secret version readback is invalid"
fi

printf '%s\n' "$ADDED_VERSION_NAME"
