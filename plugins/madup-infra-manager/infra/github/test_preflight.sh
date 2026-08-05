#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PREFLIGHT_SCRIPT="$SCRIPT_DIR/preflight.sh"
CONTRACT_LIB="$SCRIPT_DIR/../release/task18_lib.sh"
TEST_LIB="$SCRIPT_DIR/../release/test_task18_lib.sh"
. "$CONTRACT_LIB"
. "$TEST_LIB"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

GH_LOG="$TMP_DIR/gh.log"
GCLOUD_LOG="$TMP_DIR/gcloud.log"
STUB_BIN="$TMP_DIR/bin"
mkdir -p "$STUB_BIN"

cat >"$STUB_BIN/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${GH_LOG:?}"
python3 - "$@" <<'PY'
import json
import sys

args = sys.argv[1:]
if not args or args[0] != "api":
    raise SystemExit("unexpected gh invocation: " + " ".join(args))

endpoint = None
for value in args[1:]:
    if not value.startswith("-"):
        endpoint = value
        break

if endpoint is None:
    raise SystemExit("missing gh api endpoint")

repo_by_id = {
    "111111111": {
        "id": 111111111,
        "name": "campaign-one",
        "full_name": "madupmarketing/campaign-one",
        "fork": False,
        "owner": {"login": "madupmarketing"},
    },
    "222222222": {
        "id": 222222222,
        "name": "campaign-two",
        "full_name": "madupmarketing/campaign-two",
        "fork": False,
        "owner": {"login": "madupmarketing"},
    },
}

if endpoint.startswith("/repositories/"):
    print(json.dumps(repo_by_id[endpoint.rsplit("/", 1)[-1]]))
elif endpoint.startswith("/repos/") and endpoint.endswith("/installation"):
    print(json.dumps({
        "id": 909090,
        "repository_selection": "selected",
        "target_type": "Organization",
        "account": {"login": "madupmarketing"},
        "permissions": {"contents": "read", "metadata": "read"},
    }))
elif endpoint.startswith("/user/installations/") and endpoint.endswith("/repositories?per_page=100"):
    print(json.dumps({
        "total_count": 2,
        "repositories": [repo_by_id["111111111"], repo_by_id["222222222"]],
    }))
else:
    raise SystemExit("unexpected gh invocation: " + " ".join(args))
PY
EOF
chmod +x "$STUB_BIN/gh"

cat >"$STUB_BIN/gcloud" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${GCLOUD_LOG:?}"
case "$*" in
  auth\ list\ *"--format=value(account)"*)
    printf '%s\n' "${GCLOUD_ACTIVE_ACCOUNT:-operator.test@madup.com}"
    ;;
  projects\ describe\ mim-prod-123456\ *"--format=value(projectNumber)"*)
    printf '%s\n' "${GCLOUD_PROJECT_NUMBER:-123456789012}"
    ;;
  secrets\ versions\ list\ *"--secret=mim-github-authorizer-token"*"--format=json"*)
    case "${GCLOUD_AUTHORIZER_SECRET_LIST_STATE:-exists}" in
      exists)
        if [[ -n "${GCLOUD_AUTHORIZER_SECRET_LIST_JSON-}" ]]; then
          printf '%s\n' "$GCLOUD_AUTHORIZER_SECRET_LIST_JSON"
        else
          printf '%s\n' '[{"name":"projects/123456789012/secrets/mim-github-authorizer-token/versions/5","state":"ENABLED"}]'
        fi
        ;;
      missing)
        printf 'NOT_FOUND: secret version missing\n' >&2
        exit 1
        ;;
      *)
        printf 'unexpected authorizer secret list state\n' >&2
        exit 99
        ;;
    esac
    ;;
  secrets\ versions\ list\ *"--secret=mim-github-webhook"*"--format=json"*)
    case "${GCLOUD_WEBHOOK_SECRET_LIST_STATE:-exists}" in
      exists)
        if [[ -n "${GCLOUD_WEBHOOK_SECRET_LIST_JSON-}" ]]; then
          printf '%s\n' "$GCLOUD_WEBHOOK_SECRET_LIST_JSON"
        else
          printf '%s\n' '[{"name":"projects/mim-prod-123456/secrets/mim-github-webhook/versions/7","state":"ENABLED"}]'
        fi
        ;;
      missing)
        printf 'NOT_FOUND: secret version missing\n' >&2
        exit 1
        ;;
      *)
        printf 'unexpected webhook secret list state\n' >&2
        exit 99
        ;;
    esac
    ;;
  *)
    printf 'unexpected gcloud invocation: %s\n' "$*" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$STUB_BIN/gcloud"

FAILURES=0

run_case() {
  local case_name=$1 expected_exit=$2 expected_substring=$3
  shift 3
  local config_path="$TMP_DIR/$case_name.env"
  local protected_path="$TMP_DIR/$case_name.protected"
  local output_path="$TMP_DIR/$case_name.out"

  task18_write_valid_config "$config_path"
  task18_write_protected_file "$protected_path"
  : >"$GH_LOG"
  : >"$GCLOUD_LOG"

  set +e
  PATH="$STUB_BIN:$PATH" \
    GH_LOG="$GH_LOG" \
    GCLOUD_LOG="$GCLOUD_LOG" \
    MIM_CONFIG_FILE="$config_path" \
    MIM_PROTECTED_PROJECTS_FILE="$protected_path" \
    "$@" >"$output_path" 2>&1
  local exit_code=$?
  set -e

  if [[ "$exit_code" -ne "$expected_exit" ]]; then
    printf 'FAIL %s: expected exit %s, got %s\n' "$case_name" "$expected_exit" "$exit_code" >&2
    cat "$output_path" >&2 || true
    FAILURES=$((FAILURES + 1))
    return
  fi

  task18_assert_contains "$output_path" "$expected_substring" "$case_name" || FAILURES=$((FAILURES + 1))
}

run_case accepts_single_enabled_numeric_secret_versions 0 "GitHub preflight checks passed." \
  bash "$PREFLIGHT_SCRIPT"

run_case rejects_empty_enabled_authorizer_versions 1 "GitHub authorizer token secret metadata must resolve to exactly one enabled numeric version resource" \
  env GCLOUD_AUTHORIZER_SECRET_LIST_JSON='[]' bash "$PREFLIGHT_SCRIPT"

run_case rejects_ambiguous_enabled_webhook_versions 1 "GitHub webhook secret metadata must resolve to exactly one enabled numeric version resource" \
  env GCLOUD_WEBHOOK_SECRET_LIST_JSON='[{"name":"projects/mim-prod-123456/secrets/mim-github-webhook/versions/7","state":"ENABLED"},{"name":"projects/mim-prod-123456/secrets/mim-github-webhook/versions/8","state":"ENABLED"}]' bash "$PREFLIGHT_SCRIPT"

run_case rejects_non_numeric_authorizer_version_resource 1 "GitHub authorizer token secret metadata must resolve to exactly one enabled numeric version resource" \
  env GCLOUD_AUTHORIZER_SECRET_LIST_JSON='[{"name":"projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/latest","state":"ENABLED"}]' bash "$PREFLIGHT_SCRIPT"

if [[ "$FAILURES" -ne 0 ]]; then
  printf 'FAIL: %s github preflight assertions failed\n' "$FAILURES" >&2
  exit 1
fi
printf 'PASS test_preflight.sh\n'
