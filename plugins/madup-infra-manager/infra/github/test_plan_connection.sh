#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLAN_SCRIPT="$SCRIPT_DIR/plan_connection.sh"
CONTRACT_LIB="$SCRIPT_DIR/../release/task18_lib.sh"
TEST_LIB="$SCRIPT_DIR/../release/test_task18_lib.sh"
. "$CONTRACT_LIB"
. "$TEST_LIB"
export MIM_TASK18_GITHUB_WEBHOOK_URL

TMP_DIR=$(mktemp -d)
STATE_TOKEN=$$
mkdir -p "$SCRIPT_DIR/.state"

GH_LOG="$TMP_DIR/gh.log"
GCLOUD_LOG="$TMP_DIR/gcloud.log"
STUB_BIN="$TMP_DIR/bin"
mkdir -p "$STUB_BIN"

cat >"$STUB_BIN/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${GH_LOG:?}"
python3 - "$@" <<'PY'
import json, os, sys
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
    "111111111": {"id": 111111111, "name": "campaign-one", "full_name": "madupmarketing/campaign-one", "html_url": "https://github.com/madupmarketing/campaign-one", "fork": False, "owner": {"login": "madupmarketing"}},
    "222222222": {"id": 222222222, "name": "campaign-two", "full_name": "madupmarketing/campaign-two", "html_url": "https://github.com/madupmarketing/campaign-two", "fork": False, "owner": {"login": "madupmarketing"}},
}
if endpoint.startswith("/repositories/"):
    print(json.dumps(repo_by_id[endpoint.rsplit("/", 1)[-1]]))
elif endpoint.startswith("/repos/") and endpoint.endswith("/installation"):
    print(json.dumps({
        "id": int(os.environ.get("GH_INSTALLATION_ID", "909090")),
        "repository_selection": "selected",
        "target_type": "Organization",
        "account": {"login": "madupmarketing"},
        "permissions": {"contents": "read", "metadata": "read"},
    }))
elif endpoint.startswith("/user/installations/") and endpoint.endswith("/repositories?per_page=100"):
    print(json.dumps({"total_count": 2, "repositories": [repo_by_id["111111111"], repo_by_id["222222222"]]}))
elif endpoint.startswith("/repos/") and endpoint.endswith("/hooks"):
    mode = os.environ.get("GH_HOOK_MODE", "absent")
    repo = endpoint.split("/")[3]
    hook = {
        "id": 41 if repo == "campaign-one" else 42,
        "active": mode != "inactive",
        "events": ["push"] if mode != "drift" else ["push", "pull_request"],
        "config": {
            "url": os.environ["MIM_TASK18_GITHUB_WEBHOOK_URL"],
            "content_type": "json",
            "insecure_ssl": "0" if mode != "ssl" else "1",
        },
    }
    if mode == "duplicate":
        print(json.dumps([hook, {**hook, "id": 99}]))
    elif mode == "absent":
        print("[]")
    else:
        print(json.dumps([hook]))
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
  auth\ list\ *"--format=value(account)"*) printf '%s\n' "${GCLOUD_ACTIVE_ACCOUNT:-operator.test@madup.com}" ;;
  auth\ list\ *"--format=value(account)"*"--account=operator.test@madup.com"*)
    printf '%s\n' "${GCLOUD_ACTIVE_ACCOUNT:-operator.test@madup.com}"
    ;;
  projects\ describe\ mim-prod-123456\ *"--account=operator.test@madup.com"*"--format=value(projectNumber)"*)
    printf '%s\n' "${GCLOUD_PROJECT_NUMBER:-123456789012}"
    ;;
  builds\ connections\ describe\ *"--account=operator.test@madup.com"*"--format=json"*)
    case "${GCLOUD_CONNECTION_STATE:-exists}" in
      exists)
        printf '%s\n' "{\"name\":\"projects/mim-prod-123456/locations/asia-northeast3/connections/mim-github-source\",\"githubConfig\":{\"appInstallationId\":\"${GCLOUD_CONNECTION_APP_INSTALLATION_ID:-909090}\",\"authorizerCredential\":{\"oauthTokenSecretVersion\":\"${GCLOUD_CONNECTION_SECRET_VERSION:-projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/5}\"}}}"
        ;;
      missing) printf 'NOT_FOUND: connection missing\n' >&2; exit 1 ;;
      *) printf 'unexpected connection state\n' >&2; exit 99 ;;
    esac
    ;;
  builds\ repositories\ describe\ *"--account=operator.test@madup.com"*"--format=json"*)
    case "$*" in
      *"campaign-one"*) printf '%s\n' "${GCLOUD_REPOSITORY_111_JSON:-{\"name\":\"projects/mim-prod-123456/locations/asia-northeast3/connections/mim-github-source/repositories/campaign-one\",\"remoteUri\":\"https://github.com/madupmarketing/campaign-one.git\"}}" ;;
      *"campaign-two"*) printf '%s\n' "${GCLOUD_REPOSITORY_222_JSON:-{\"name\":\"projects/mim-prod-123456/locations/asia-northeast3/connections/mim-github-source/repositories/campaign-two\",\"remoteUri\":\"https://github.com/madupmarketing/campaign-two.git\"}}" ;;
      *) printf 'unexpected repository describe invocation: %s\n' "$*" >&2; exit 99 ;;
    esac
    ;;
  secrets\ versions\ list\ *"--secret=mim-github-authorizer-token"*"--account=operator.test@madup.com"*"--format=json"*)
    case "${GCLOUD_AUTHORIZER_SECRET_LIST_STATE:-exists}" in
      exists)
        if [[ -n "${GCLOUD_AUTHORIZER_SECRET_LIST_JSON-}" ]]; then
          printf '%s\n' "$GCLOUD_AUTHORIZER_SECRET_LIST_JSON"
        else
          printf '%s\n' '[{"name":"projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/5","state":"ENABLED"}]'
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
  secrets\ versions\ describe\ *"--secret=mim-github-authorizer-token"*"--account=operator.test@madup.com"*"--format=json"*)
    if [[ -n "${GCLOUD_AUTHORIZER_SECRET_DESCRIBE_JSON-}" ]]; then
      printf '%s\n' "$GCLOUD_AUTHORIZER_SECRET_DESCRIBE_JSON"
    else
      printf '%s\n' '{"name":"projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/5","state":"ENABLED"}'
    fi
    ;;
  secrets\ versions\ list\ *"--secret=mim-github-webhook"*"--account=operator.test@madup.com"*"--format=json"*)
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
  secrets\ versions\ describe\ *"--secret=mim-github-webhook"*"--account=operator.test@madup.com"*"--format=json"*)
    if [[ -n "${GCLOUD_WEBHOOK_SECRET_DESCRIBE_JSON-}" ]]; then
      printf '%s\n' "$GCLOUD_WEBHOOK_SECRET_DESCRIBE_JSON"
    else
      printf '%s\n' '{"name":"projects/mim-prod-123456/secrets/mim-github-webhook/versions/7","state":"ENABLED"}'
    fi
    ;;
  *) printf 'unexpected gcloud invocation: %s\n' "$*" >&2; exit 99 ;;
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
  PATH="$STUB_BIN:$PATH" GH_LOG="$GH_LOG" GCLOUD_LOG="$GCLOUD_LOG" MIM_CONFIG_FILE="$config_path" MIM_PROTECTED_PROJECTS_FILE="$protected_path" "$@" >"$output_path" 2>&1
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

PLAN_PATH="$SCRIPT_DIR/.state/test-github-plan-$STATE_TOKEN.json"
ACCOUNT_PLAN_PATH="$SCRIPT_DIR/.state/test-github-plan-account-$STATE_TOKEN.json"
DRIFT_PLAN_PATH="$SCRIPT_DIR/.state/test-github-plan-drift-$STATE_TOKEN.json"
DUP_PLAN_PATH="$SCRIPT_DIR/.state/test-github-plan-dup-$STATE_TOKEN.json"
WEBHOOK_BLOCKED_PLAN_PATH="$SCRIPT_DIR/.state/test-github-plan-webhook-blocked-$STATE_TOKEN.json"
EXPLICIT_PLAN_PATH="$SCRIPT_DIR/.state/test-github-plan-explicit-$STATE_TOKEN.json"
ALIAS_PLAN_PATH="$SCRIPT_DIR/.state/test-github-plan-alias-$STATE_TOKEN.json"
WRONG_PROJECT_PLAN_PATH="$SCRIPT_DIR/.state/test-github-plan-wrong-project-$STATE_TOKEN.json"
AMBIGUOUS_PLAN_PATH="$SCRIPT_DIR/.state/test-github-plan-ambiguous-$STATE_TOKEN.json"
DISABLED_PLAN_PATH="$SCRIPT_DIR/.state/test-github-plan-disabled-$STATE_TOKEN.json"
trap 'rm -rf "$TMP_DIR"; rm -f "$PLAN_PATH" "$PLAN_PATH.sha256" "$ACCOUNT_PLAN_PATH" "$ACCOUNT_PLAN_PATH.sha256" "$DRIFT_PLAN_PATH" "$DRIFT_PLAN_PATH.sha256" "$DUP_PLAN_PATH" "$DUP_PLAN_PATH.sha256" "$WEBHOOK_BLOCKED_PLAN_PATH" "$WEBHOOK_BLOCKED_PLAN_PATH.sha256" "$EXPLICIT_PLAN_PATH" "$EXPLICIT_PLAN_PATH.sha256" "$ALIAS_PLAN_PATH" "$ALIAS_PLAN_PATH.sha256" "$WRONG_PROJECT_PLAN_PATH" "$WRONG_PROJECT_PLAN_PATH.sha256" "$AMBIGUOUS_PLAN_PATH" "$AMBIGUOUS_PLAN_PATH.sha256" "$DISABLED_PLAN_PATH" "$DISABLED_PLAN_PATH.sha256" 2>/dev/null || true' EXIT
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
run_case writes_github_plan 0 "Wrote reviewed plan" bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH"
python3 - "$PLAN_PATH" <<'PY'
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["version"] == "mim-github-connection-plan-v4"
assert plan["initial_state"]["connection"] == {
    "status": "exists",
    "app_installation_id": 909090,
    "authorizer_token_secret_version": "projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/5",
}
assert plan["initial_state"]["authorizer_token_secret_version"] == {
    "name": "projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/5",
    "state": "ENABLED",
}
assert plan["initial_state"]["webhook_secret_version"] == {
    "name": "projects/mim-prod-123456/secrets/mim-github-webhook/versions/7",
    "state": "ENABLED",
}
assert plan["initial_state"]["repository_hooks"]["campaign-one"] == {"status": "absent"}
assert all(action["kind"] != "create_connection" for action in plan["actions"])
assert plan["runtime_secret_contract"] == {
    "apply_env": "MIM_TASK18_GITHUB_WEBHOOK_SECRET",
    "minimum_length_bytes": 32,
    "secret_name": "mim-github-webhook",
    "secret_version": "projects/mim-prod-123456/secrets/mim-github-webhook/versions/7",
}
assert "latest" not in json.dumps(plan, sort_keys=True)
PY

run_case rejects_active_account_mismatch 1 "Active gcloud account does not match the configured operator" env GCLOUD_ACTIVE_ACCOUNT=wrong-account@madup.com bash "$PLAN_SCRIPT" --plan --out "$ACCOUNT_PLAN_PATH"

rm -f "$DRIFT_PLAN_PATH" "$DRIFT_PLAN_PATH.sha256"
run_case blocks_connection_drift 0 "Wrote reviewed plan" env GCLOUD_CONNECTION_SECRET_VERSION=projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/8 bash "$PLAN_SCRIPT" --plan --out "$DRIFT_PLAN_PATH"
python3 - "$DRIFT_PLAN_PATH" <<'PY'
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert any(item["code"] == "connection-config-drift" for item in plan["blockers"])
PY

rm -f "$DUP_PLAN_PATH" "$DUP_PLAN_PATH.sha256"
run_case blocks_duplicate_hooks 1 "Duplicate MIM GitHub webhook hooks are forbidden" env GH_HOOK_MODE=duplicate bash "$PLAN_SCRIPT" --plan --out "$DUP_PLAN_PATH"

rm -f "$WEBHOOK_BLOCKED_PLAN_PATH" "$WEBHOOK_BLOCKED_PLAN_PATH.sha256"
run_case blocks_missing_numeric_webhook_secret_version 0 "Wrote reviewed plan" env GCLOUD_WEBHOOK_SECRET_LIST_STATE=missing bash "$PLAN_SCRIPT" --plan --out "$WEBHOOK_BLOCKED_PLAN_PATH"
python3 - "$WEBHOOK_BLOCKED_PLAN_PATH" <<'PY'
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert any(item["code"] == "missing-github-webhook-secret-version" for item in plan["blockers"])
PY

run_case rejects_aliased_authorizer_secret_override 1 "MIM_TASK18_GITHUB_AUTHORIZER_SECRET_VERSION must reference the configured project, expected secret name, and an exact numeric version resource" env MIM_TASK18_GITHUB_AUTHORIZER_SECRET_VERSION=projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/latest bash "$PLAN_SCRIPT" --plan --out "$ALIAS_PLAN_PATH"
run_case rejects_wrong_project_authorizer_override 1 "MIM_TASK18_GITHUB_AUTHORIZER_SECRET_VERSION must reference the configured project, expected secret name, and an exact numeric version resource" env MIM_TASK18_GITHUB_AUTHORIZER_SECRET_VERSION=projects/wrong-project/secrets/mim-github-authorizer-token/versions/5 bash "$PLAN_SCRIPT" --plan --out "$WRONG_PROJECT_PLAN_PATH"
run_case rejects_ambiguous_enabled_authorizer_versions 1 "Multiple enabled GitHub authorizer token secret versions found" env GCLOUD_AUTHORIZER_SECRET_LIST_JSON='[{"name":"projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/5","state":"ENABLED"},{"name":"projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/6","state":"ENABLED"}]' bash "$PLAN_SCRIPT" --plan --out "$AMBIGUOUS_PLAN_PATH"
run_case rejects_disabled_explicit_authorizer_version 1 "MIM_TASK18_GITHUB_AUTHORIZER_SECRET_VERSION must reference an enabled secret version in the configured project" env MIM_TASK18_GITHUB_AUTHORIZER_SECRET_VERSION=projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/6 GCLOUD_AUTHORIZER_SECRET_DESCRIBE_JSON='{"name":"projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/6","state":"DISABLED"}' bash "$PLAN_SCRIPT" --plan --out "$DISABLED_PLAN_PATH"

rm -f "$EXPLICIT_PLAN_PATH" "$EXPLICIT_PLAN_PATH.sha256"
run_case accepts_exact_numeric_authorizer_override 0 "Wrote reviewed plan" env GCLOUD_CONNECTION_STATE=missing MIM_TASK18_GITHUB_AUTHORIZER_SECRET_VERSION=projects/123456789012/secrets/mim-github-authorizer-token/versions/6 GCLOUD_AUTHORIZER_SECRET_DESCRIBE_JSON='{"name":"projects/123456789012/secrets/mim-github-authorizer-token/versions/6","state":"ENABLED"}' bash "$PLAN_SCRIPT" --plan --out "$EXPLICIT_PLAN_PATH"
python3 - "$EXPLICIT_PLAN_PATH" <<'PY'
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "ready"
assert plan["initial_state"]["authorizer_token_secret_version"] == {
    "name": "projects/123456789012/secrets/mim-github-authorizer-token/versions/6",
    "state": "ENABLED",
}
create_actions = [item for item in plan["actions"] if item["kind"] == "create_connection"]
assert create_actions == [{
    "kind": "create_connection",
    "name": "mim-github-source",
    "authorizer_token_secret_version": "projects/123456789012/secrets/mim-github-authorizer-token/versions/6",
    "app_installation_id": 909090,
}]
PY

if [[ "$FAILURES" -ne 0 ]]; then
  printf 'FAIL: %s github plan assertions failed\n' "$FAILURES" >&2
  exit 1
fi
printf 'PASS test_plan_connection.sh\n'
