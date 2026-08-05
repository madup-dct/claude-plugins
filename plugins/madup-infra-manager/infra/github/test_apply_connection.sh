#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLAN_SCRIPT="$SCRIPT_DIR/plan_connection.sh"
APPLY_SCRIPT="$SCRIPT_DIR/apply_connection.sh"
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
HOOK_BODY_DIR="$TMP_DIR/hook-bodies"
mkdir -p "$HOOK_BODY_DIR"
STUB_BIN="$TMP_DIR/bin"
mkdir -p "$STUB_BIN"

cat >"$STUB_BIN/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${GH_LOG:?}"
python3 - "$@" <<'PY'
import json, os, sys
from pathlib import Path

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

repo_name_map = {
    "campaign-one": 111111111,
    "campaign-two": 222222222,
}
repo_by_id = {
    "111111111": {"id": 111111111, "name": "campaign-one", "full_name": "madupmarketing/campaign-one", "html_url": "https://github.com/madupmarketing/campaign-one", "fork": False, "owner": {"login": "madupmarketing"}, "permissions": {"admin": True}},
    "222222222": {"id": 222222222, "name": "campaign-two", "full_name": "madupmarketing/campaign-two", "html_url": "https://github.com/madupmarketing/campaign-two", "fork": False, "owner": {"login": "madupmarketing"}, "permissions": {"admin": True}},
}

def hook_payload(repo, mode):
    base_id = 41 if repo == "campaign-one" else 42
    payload = {
        "id": base_id,
        "active": True,
        "events": ["push"],
        "config": {
            "url": os.environ["MIM_TASK18_GITHUB_WEBHOOK_URL"],
            "content_type": "json",
            "insecure_ssl": "0",
        },
    }
    if mode == "drift":
        payload["events"] = ["push", "pull_request"]
    return payload

if endpoint.startswith("/repositories/"):
    print(json.dumps({k: v for k, v in repo_by_id[endpoint.rsplit("/", 1)[-1]].items() if k != "permissions"}))
elif endpoint.startswith("/repos/") and endpoint.endswith("/installation"):
    print(json.dumps({
        "id": 909090,
        "repository_selection": "selected",
        "target_type": "Organization",
        "account": {"login": "madupmarketing"},
        "permissions": {"contents": "read", "metadata": "read"},
    }))
elif endpoint.startswith("/user/installations/") and endpoint.endswith("/repositories?per_page=100"):
    print(json.dumps({"total_count": 2, "repositories": [{k: v for k, v in repo_by_id["111111111"].items() if k != "permissions"}, {k: v for k, v in repo_by_id["222222222"].items() if k != "permissions"}]}))
elif endpoint.startswith("/repos/") and endpoint.count("/") == 3:
    repo = endpoint.rsplit("/", 1)[-1]
    repo_id = str(repo_name_map[repo])
    print(json.dumps(repo_by_id[repo_id]))
elif endpoint.startswith("/repos/") and endpoint.endswith("/hooks"):
    repo = endpoint.split("/")[3]
    mode = os.environ.get("GH_HOOK_MODE", "absent")
    if "--method" in args and "POST" in args:
        body = Path(args[args.index("--input") + 1]).read_text()
        Path(os.environ["HOOK_BODY_DIR"]).joinpath(f"{repo}.json").write_text(body)
        payload = json.loads(body)
        created_id = 541 if repo == "campaign-one" else 542
        print(json.dumps({"id": created_id, "name": "web", "active": payload["active"], "events": payload["events"], "config": {"url": payload["config"]["url"], "content_type": payload["config"]["content_type"], "insecure_ssl": payload["config"]["insecure_ssl"], "secret": "********"}}))
    else:
        if mode == "duplicate":
            hook = hook_payload(repo, "exact")
            print(json.dumps([hook, {**hook, "id": hook["id"] + 100}]))
        elif mode == "absent":
            print("[]")
        else:
            print(json.dumps([hook_payload(repo, mode)]))
elif endpoint.startswith("/repos/") and "/hooks/" in endpoint:
    repo = endpoint.split("/")[3]
    mode = os.environ.get("GH_HOOK_MODE", "drift")
    if "--method" in args and "PATCH" in args:
        body = Path(args[args.index("--input") + 1]).read_text()
        Path(os.environ["HOOK_BODY_DIR"]).joinpath(f"{repo}-patch.json").write_text(body)
        payload = json.loads(body)
        print(json.dumps({"id": 41 if repo == "campaign-one" else 42, "name": "web", "active": payload["active"], "events": payload["events"], "config": {"url": payload["config"]["url"], "content_type": payload["config"]["content_type"], "insecure_ssl": payload["config"]["insecure_ssl"], "secret": "********"}}))
    else:
        exact = hook_payload(repo, "exact")
        print(json.dumps({"id": exact["id"], "name": "web", "active": exact["active"], "events": exact["events"], "config": {**exact["config"], "secret": "********"}}))
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
  auth\ list\ *"--format=value(account)"*"--account=operator.test@madup.com"*) printf '%s\n' "${GCLOUD_ACTIVE_ACCOUNT:-operator.test@madup.com}" ;;
  projects\ describe\ mim-prod-123456\ *"--account=operator.test@madup.com"*"--format=value(projectNumber)"*) printf '%s\n' "${GCLOUD_PROJECT_NUMBER:-123456789012}" ;;
  builds\ connections\ describe\ *"--account=operator.test@madup.com"*"--format=json"*)
    case "${GCLOUD_CONNECTION_STATE:-exists}" in
      exists) printf '%s\n' '{"name":"projects/mim-prod-123456/locations/asia-northeast3/connections/mim-github-source","githubConfig":{"appInstallationId":"909090","authorizerCredential":{"oauthTokenSecretVersion":"projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/5"}}}' ;;
      missing) printf 'NOT_FOUND: connection missing\n' >&2; exit 1 ;;
      *) printf 'unexpected connection state\n' >&2; exit 99 ;;
    esac
    ;;
  builds\ repositories\ describe\ *"campaign-one"*"--account=operator.test@madup.com"*"--format=json"*) printf '%s\n' '{"name":"projects/mim-prod-123456/locations/asia-northeast3/connections/mim-github-source/repositories/campaign-one","remoteUri":"https://github.com/madupmarketing/campaign-one.git"}' ;;
  builds\ repositories\ describe\ *"campaign-two"*"--account=operator.test@madup.com"*"--format=json"*) printf '%s\n' '{"name":"projects/mim-prod-123456/locations/asia-northeast3/connections/mim-github-source/repositories/campaign-two","remoteUri":"https://github.com/madupmarketing/campaign-two.git"}' ;;
  secrets\ versions\ list\ *"--secret=mim-github-authorizer-token"*"--account=operator.test@madup.com"*"--format=json"*) printf '%s\n' '[{"name":"projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/5","state":"ENABLED"}]' ;;
  secrets\ versions\ describe\ *"--secret=mim-github-authorizer-token"*"--account=operator.test@madup.com"*"--format=json"*)
    if [[ -n "${GCLOUD_AUTHORIZER_SECRET_DESCRIBE_JSON-}" ]]; then
      printf '%s\n' "$GCLOUD_AUTHORIZER_SECRET_DESCRIBE_JSON"
    else
      printf '%s\n' '{"name":"projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/5","state":"ENABLED"}'
    fi
    ;;
  secrets\ versions\ list\ *"--secret=mim-github-webhook"*"--account=operator.test@madup.com"*"--format=json"*) printf '%s\n' '[{"name":"projects/mim-prod-123456/secrets/mim-github-webhook/versions/7","state":"ENABLED"}]' ;;
  secrets\ versions\ describe\ *"--secret=mim-github-webhook"*"--account=operator.test@madup.com"*"--format=json"*) printf '%s\n' '{"name":"projects/mim-prod-123456/secrets/mim-github-webhook/versions/7","state":"ENABLED"}' ;;
  secrets\ describe\ mim-github-webhook\ *"--format=value(name)"*"--account=operator.test@madup.com"*) printf 'projects/mim-prod-123456/secrets/mim-github-webhook\n' ;;
  secrets\ versions\ access\ 7\ *"--secret=mim-github-webhook"*"--out-file="*"--account=operator.test@madup.com"*)
    out_file=
    for arg in "$@"; do
      case "$arg" in
        --out-file=*)
          out_file=${arg#--out-file=}
          ;;
      esac
    done
    [[ -n "$out_file" ]] || exit 99
    printf '%s' 'task18-github-webhook-secret-value-0123456789' >"$out_file"
    ;;
  builds\ connections\ create\ github\ *"--account=operator.test@madup.com"*) : ;;
  builds\ repositories\ create\ *"--account=operator.test@madup.com"*) : ;;
  *) printf 'unexpected gcloud invocation: %s\n' "$*" >&2; exit 99 ;;
esac
EOF
chmod +x "$STUB_BIN/gcloud"

FAILURES=0
PLAN_PATH="$SCRIPT_DIR/.state/test-github-apply-plan-$STATE_TOKEN.json"
trap 'rm -rf "$TMP_DIR"; rm -f "$PLAN_PATH" "$PLAN_PATH.sha256" 2>/dev/null || true' EXIT

make_plan() {
  local config_path=$1
  local protected_path=$2
  local hook_mode=${3:-absent}
  PATH="$STUB_BIN:$PATH" GH_LOG="$GH_LOG" GCLOUD_LOG="$GCLOUD_LOG" HOOK_BODY_DIR="$HOOK_BODY_DIR" GH_HOOK_MODE="$hook_mode" MIM_CONFIG_FILE="$config_path" MIM_PROTECTED_PROJECTS_FILE="$protected_path" bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >/dev/null
}

run_apply_case() {
  local case_name=$1 expected_exit=$2 expected_substring=$3 plan_hook_mode=${4:-absent} apply_hook_mode=${5:-$4}
  local config_path="$TMP_DIR/$case_name.env"
  local protected_path="$TMP_DIR/$case_name.protected"
  local output_path="$TMP_DIR/$case_name.out"
  task18_write_valid_config "$config_path"
  task18_write_protected_file "$protected_path"
  : >"$GH_LOG"
  : >"$GCLOUD_LOG"
  rm -f "$HOOK_BODY_DIR"/*.json "$PLAN_PATH" "$PLAN_PATH.sha256"
  make_plan "$config_path" "$protected_path" "$plan_hook_mode"
  set +e
  PATH="$STUB_BIN:$PATH" GH_LOG="$GH_LOG" GCLOUD_LOG="$GCLOUD_LOG" HOOK_BODY_DIR="$HOOK_BODY_DIR" GH_HOOK_MODE="$apply_hook_mode" MIM_CONFIG_FILE="$config_path" MIM_PROTECTED_PROJECTS_FILE="$protected_path" bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$output_path" 2>&1
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

run_apply_case creates_absent_hook 0 "Applied reviewed plan with GitHub webhook secret version projects/mim-prod-123456/secrets/mim-github-webhook/versions/7." absent
task18_assert_contains "$GH_LOG" "api /repos/madupmarketing/campaign-one/hooks --method POST --input" creates_absent_hook || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GH_LOG" "api /repos/madupmarketing/campaign-one/hooks/541" creates_absent_hook || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "secrets versions access 7 --secret=mim-github-webhook --out-file=" creates_absent_hook || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$GCLOUD_LOG" "task18-github-webhook-secret-value-0123456789" creates_absent_hook || FAILURES=$((FAILURES + 1))

run_apply_case keeps_exact_hook_noop 0 "Applied reviewed plan with GitHub webhook secret version projects/mim-prod-123456/secrets/mim-github-webhook/versions/7." exact
task18_assert_not_contains "$GH_LOG" "api /repos/madupmarketing/campaign-one/hooks --method POST --input" keeps_exact_hook_noop || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$GH_LOG" "api /repos/madupmarketing/campaign-one/hooks/41 --method PATCH --input" keeps_exact_hook_noop || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$GCLOUD_LOG" "secrets versions access 7 --secret=mim-github-webhook --out-file=" keeps_exact_hook_noop || FAILURES=$((FAILURES + 1))

run_apply_case patches_drifted_hook 0 "Applied reviewed plan with GitHub webhook secret version projects/mim-prod-123456/secrets/mim-github-webhook/versions/7." drift
task18_assert_contains "$GH_LOG" "api /repos/madupmarketing/campaign-one/hooks/41 --method PATCH --input" patches_drifted_hook || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GH_LOG" "api /repos/madupmarketing/campaign-one/hooks/41" patches_drifted_hook || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "secrets versions access 7 --secret=mim-github-webhook --out-file=" patches_drifted_hook || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$GCLOUD_LOG" "task18-github-webhook-secret-value-0123456789" patches_drifted_hook || FAILURES=$((FAILURES + 1))

GCLOUD_CONNECTION_STATE=missing run_apply_case creates_connection_with_exact_authorizer_version 0 "Applied reviewed plan with GitHub webhook secret version projects/mim-prod-123456/secrets/mim-github-webhook/versions/7." absent
task18_assert_contains "$GCLOUD_LOG" "builds connections create github mim-github-source" creates_connection_with_exact_authorizer_version || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "--authorizer-token-secret-version=projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/5" creates_connection_with_exact_authorizer_version || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$GCLOUD_LOG" "/versions/latest" creates_connection_with_exact_authorizer_version || FAILURES=$((FAILURES + 1))

python3 - "$PLAN_PATH" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text())
data["runtime_secret_contract"]["secret_version"] = "projects/mim-prod-123456/secrets/mim-github-webhook/versions/8"
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY
printf '%s  %s\n' "$(LC_ALL=C shasum -a 256 "$PLAN_PATH" | awk '{print $1}')" "$(basename "$PLAN_PATH")" >"$PLAN_PATH.sha256"
chmod 600 "$PLAN_PATH.sha256"
set +e
PATH="$STUB_BIN:$PATH" GH_LOG="$GH_LOG" GCLOUD_LOG="$GCLOUD_LOG" HOOK_BODY_DIR="$HOOK_BODY_DIR" GH_HOOK_MODE=exact MIM_CONFIG_FILE="$TMP_DIR/keeps_exact_hook_noop.env" MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/keeps_exact_hook_noop.protected" bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/bad-secret-version.out" 2>&1
bad_secret_exit=$?
set -e
[[ "$bad_secret_exit" -ne 0 ]] || { printf 'FAIL bad_secret_version: expected failure\n' >&2; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$TMP_DIR/bad-secret-version.out" "Discovery drift detected" bad_secret_version || FAILURES=$((FAILURES + 1))

task18_write_valid_config "$TMP_DIR/bad-authorizer.env"
task18_write_protected_file "$TMP_DIR/bad-authorizer.protected"
: >"$GH_LOG"
: >"$GCLOUD_LOG"
rm -f "$HOOK_BODY_DIR"/*.json "$PLAN_PATH" "$PLAN_PATH.sha256"
GCLOUD_CONNECTION_STATE=missing PATH="$STUB_BIN:$PATH" GH_LOG="$GH_LOG" GCLOUD_LOG="$GCLOUD_LOG" HOOK_BODY_DIR="$HOOK_BODY_DIR" GH_HOOK_MODE=absent MIM_CONFIG_FILE="$TMP_DIR/bad-authorizer.env" MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/bad-authorizer.protected" bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >/dev/null
python3 - "$PLAN_PATH" <<'PY'
import hashlib, json, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text())
data["initial_state"]["authorizer_token_secret_version"]["name"] = "projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/latest"
for action in data["actions"]:
    if action["kind"] == "create_connection":
        action["authorizer_token_secret_version"] = "projects/mim-prod-123456/secrets/mim-github-authorizer-token/versions/latest"
data["discovery_hash"] = hashlib.sha256(
    json.dumps(data["initial_state"], sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY
printf '%s  %s\n' "$(LC_ALL=C shasum -a 256 "$PLAN_PATH" | awk '{print $1}')" "$(basename "$PLAN_PATH")" >"$PLAN_PATH.sha256"
chmod 600 "$PLAN_PATH.sha256"
set +e
GCLOUD_CONNECTION_STATE=missing PATH="$STUB_BIN:$PATH" GH_LOG="$GH_LOG" GCLOUD_LOG="$GCLOUD_LOG" HOOK_BODY_DIR="$HOOK_BODY_DIR" GH_HOOK_MODE=absent MIM_CONFIG_FILE="$TMP_DIR/bad-authorizer.env" MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/bad-authorizer.protected" bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/bad-authorizer-version.out" 2>&1
bad_authorizer_exit=$?
set -e
[[ "$bad_authorizer_exit" -ne 0 ]] || { printf 'FAIL bad_authorizer_version: expected failure\n' >&2; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$TMP_DIR/bad-authorizer-version.out" "Discovery drift detected" bad_authorizer_version || FAILURES=$((FAILURES + 1))

run_apply_case rejects_duplicate_hook 1 "Duplicate MIM GitHub webhook hooks are forbidden" absent duplicate

task18_assert_contains "$GCLOUD_LOG" "--account=operator.test@madup.com" gcloud_account_enforced || FAILURES=$((FAILURES + 1))

if [[ "$FAILURES" -ne 0 ]]; then
  printf 'FAIL: %s github apply assertions failed\n' "$FAILURES" >&2
  exit 1
fi
printf 'PASS test_apply_connection.sh\n'
