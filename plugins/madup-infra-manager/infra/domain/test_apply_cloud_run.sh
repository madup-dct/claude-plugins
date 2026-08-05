#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APPLY_SCRIPT="$SCRIPT_DIR/apply_cloud_run.sh"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

OPERATOR_EMAIL=operator.test@madup.com
PROJECT_ID=mim-prod-123456
ORGANIZATION_ID=123456789012
BILLING_ACCOUNT_ID=ABCDEF-123456-7890AB
PROJECT_NUMBER=123456789012
OTHER_PROJECT_ID=other-prod-654321
MUTATED_PROJECT_ID=mutated-prod-654321
EXPECTED_RUNTIME_SERVICE_ACCOUNT="mim-bootstrap-runtime@$PROJECT_ID.iam.gserviceaccount.com"
EXPECTED_IAP_SERVICE_AGENT="serviceAccount:service-$PROJECT_NUMBER@gcp-sa-iap.iam.gserviceaccount.com"
RUN_APP_SUFFIX=".a.run"".app"
EXPECTED_SERVICE_URL="https://synthetic-bootstrap-12345${RUN_APP_SUFFIX}"

STUB_BIN="$TMP_DIR/bin"
GCLOUD_LOG="$TMP_DIR/gcloud.log"
DOCKER_LOG="$TMP_DIR/docker.log"
DOCKER_ENV_LOG="$TMP_DIR/docker-env.log"
PYTHON_LOG="$TMP_DIR/python.log"
REAL_PYTHON3=$(command -v python3)
mkdir -p "$STUB_BIN"

cat >"$STUB_BIN/gcloud" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' "$*" >> "${GCLOUD_LOG:?}"

maybe_capture_and_mutate() {
  if [[ "${GCLOUD_CAPTURE_PREFLIGHT_ENV_ON_AUTH_LIST:-0}" == 1 && "${GCLOUD_CAPTURED_PREFLIGHT_ENV:-0}" == 0 ]]; then
    printf 'PREFLIGHT_ENV MIM_CONFIG_FILE=%s MIM_PROTECTED_PROJECTS_FILE=%s\n' \
      "${MIM_CONFIG_FILE:-}" "${MIM_PROTECTED_PROJECTS_FILE:-}" >> "${GCLOUD_LOG:?}"
    export GCLOUD_CAPTURED_PREFLIGHT_ENV=1
  fi

  if [[ "${GCLOUD_MUTATE_ORIGINAL_FILES_ON_AUTH_LIST:-0}" == 1 && "${GCLOUD_MUTATED_ORIGINAL_FILES:-0}" == 0 ]]; then
    cat >"${GCLOUD_MUTATE_CONFIG_PATH:?}" <<MUTATED_CONFIG
MIM_OPERATOR_EMAIL=${GCLOUD_MUTATE_OPERATOR_EMAIL:?}
MIM_PROJECT_ID=${GCLOUD_MUTATE_PROJECT_ID:?}
MIM_ORGANIZATION_ID=${GCLOUD_MUTATE_ORGANIZATION_ID:?}
MIM_BILLING_ACCOUNT_ID=${GCLOUD_MUTATE_BILLING_ACCOUNT_ID:?}
MUTATED_CONFIG
    chmod 600 "${GCLOUD_MUTATE_CONFIG_PATH:?}"
    printf '%s\n' "${GCLOUD_MUTATE_PROTECTED_BODY:?}" >"${GCLOUD_MUTATE_PROTECTED_PATH:?}"
    chmod 600 "${GCLOUD_MUTATE_PROTECTED_PATH:?}"
    export GCLOUD_MUTATED_ORIGINAL_FILES=1
  fi
}

if [[ "${1:-}" == "run" && "${2:-}" == "deploy" && "${3:-}" == "--help" ]]; then
  printf '%s\n' "${GCLOUD_RUN_DEPLOY_HELP:-}"
  exit "${GCLOUD_RUN_DEPLOY_HELP_EXIT:-0}"
fi

if [[ "${1:-}" == "alpha" && "${2:-}" == "run" && "${3:-}" == "deploy" && "${4:-}" == "--help" ]]; then
  printf '%s\n' "${GCLOUD_ALPHA_RUN_DEPLOY_HELP:-}"
  exit "${GCLOUD_ALPHA_RUN_DEPLOY_HELP_EXIT:-0}"
fi

if [[ "${1:-}" == "iap" && "${2:-}" == "web" && "${3:-}" == "add-iam-policy-binding" && "${4:-}" == "--help" ]]; then
  printf '%s\n' "${GCLOUD_IAP_BINDING_HELP:-}"
  exit "${GCLOUD_IAP_BINDING_HELP_EXIT:-0}"
fi

if [[ "${1:-}" == "alpha" && "${2:-}" == "iap" && "${3:-}" == "web" && "${4:-}" == "add-iam-policy-binding" && "${5:-}" == "--help" ]]; then
  printf '%s\n' "${GCLOUD_ALPHA_IAP_BINDING_HELP:-}"
  exit "${GCLOUD_ALPHA_IAP_BINDING_HELP_EXIT:-0}"
fi

case "$*" in
  auth\ list\ *"--format=value(account)"*)
    maybe_capture_and_mutate
    printf '%s\n' "${GCLOUD_ACTIVE_ACCOUNT:-}"
    ;;
  auth\ print-access-token*)
    printf '%s\n' "${GCLOUD_ACCESS_TOKEN:-ya29.short-lived-token}"
    ;;
  projects\ describe\ *"--format=value(projectId)"*)
    printf '%s\n' "${GCLOUD_PROJECT_DESCRIBE_ID:-}"
    ;;
  projects\ describe\ *"--format=value(parent.type)"*)
    printf '%s\n' "${GCLOUD_PROJECT_PARENT_TYPE:-}"
    ;;
  projects\ describe\ *"--format=value(parent.id)"*)
    printf '%s\n' "${GCLOUD_PROJECT_PARENT_ID:-}"
    ;;
  projects\ describe\ *"--format=value(projectNumber)"*)
    printf '%s\n' "${GCLOUD_PROJECT_NUMBER:-}"
    ;;
  billing\ projects\ describe\ *"--format=value(billingEnabled)"*)
    printf '%s\n' "${GCLOUD_BILLING_ENABLED:-}"
    ;;
  billing\ projects\ describe\ *"--format=value(billingAccountName)"*)
    printf '%s\n' "${GCLOUD_BILLING_ACCOUNT_NAME:-}"
    ;;
  services\ enable\ *)
    :
    ;;
  artifacts\ repositories\ describe\ *"--format=value(format)"*)
    case "${GCLOUD_ARTIFACT_REPO_STATUS:-not_found}" in
      exists)
        printf '%s\n' "${GCLOUD_ARTIFACT_REPO_FORMAT:-docker}"
        ;;
      not_found)
        printf 'NOT_FOUND: repository missing\n' >&2
        exit 1
        ;;
      error)
        printf '%s\n' "${GCLOUD_ARTIFACT_REPO_ERROR_TEXT:-PERMISSION_DENIED: repo describe failed}" >&2
        exit "${GCLOUD_ARTIFACT_REPO_ERROR_EXIT:-1}"
        ;;
    esac
    ;;
  artifacts\ repositories\ describe\ *"--format=value(name)"*)
    printf 'projects/%s/locations/%s/repositories/mim-bootstrap\n' "${GCLOUD_PROJECT_DESCRIBE_ID:-}" "${GCLOUD_ARTIFACT_REPO_LOCATION:-asia-northeast3}"
    ;;
  artifacts\ repositories\ create\ *)
    :
    ;;
  iam\ service-accounts\ describe\ *)
    case "${GCLOUD_RUNTIME_SA_STATUS:-not_found}" in
      exists)
        printf '%s\n' "${4:-}"
        ;;
      not_found)
        printf 'NOT_FOUND: service account missing\n' >&2
        exit 1
        ;;
      error)
        printf '%s\n' "${GCLOUD_RUNTIME_SA_ERROR_TEXT:-PERMISSION_DENIED: service account describe failed}" >&2
        exit "${GCLOUD_RUNTIME_SA_ERROR_EXIT:-1}"
        ;;
    esac
    ;;
  iam\ service-accounts\ create\ *)
    :
    ;;
  projects\ get-iam-policy\ *"--filter=bindings.role=roles/run.invoker"*)
    printf '%s\n' "${GCLOUD_PROJECT_RUN_INVOKERS:-}"
    ;;
  projects\ get-iam-policy\ *"--filter=bindings.role=roles/iap.httpsResourceAccessor"*)
    printf '%s\n' "${GCLOUD_PROJECT_IAP_ACCESSORS:-}"
    ;;
  projects\ get-iam-policy\ *)
    printf '%s\n' "${GCLOUD_PROJECT_IAM_POLICY_OUTPUT:-}"
    ;;
  alpha\ services\ identity\ create\ *)
    :
    ;;
  services\ identity\ create\ *)
    printf 'unexpected stable services identity create\n' >&2
    exit 91
    ;;
  run\ services\ describe\ *"--format=value(metadata.name)"*)
    case "${GCLOUD_RUN_SERVICE_STATUS:-not_found}" in
      exists)
        printf '%s\n' "${GCLOUD_RUN_SERVICE_NAME:-mim-bootstrap}"
        ;;
      not_found)
        printf 'ERROR: (gcloud.run.services.describe) Cannot find service [%s]\n' "${GCLOUD_RUN_SERVICE_NAME:-mim-bootstrap}" >&2
        exit 1
        ;;
      error)
        printf '%s\n' "${GCLOUD_RUN_SERVICE_ERROR_TEXT:-PERMISSION_DENIED: service describe failed}" >&2
        exit "${GCLOUD_RUN_SERVICE_ERROR_EXIT:-1}"
        ;;
    esac
    ;;
  run\ services\ describe\ *"--format=value(spec.template.spec.serviceAccountName)"*)
    printf '%s\n' "${GCLOUD_RUN_RUNTIME_SERVICE_ACCOUNT:-}"
    ;;
  run\ services\ describe\ *"--format=value(status.url)"*)
    printf '%s\n' "${GCLOUD_RUN_SERVICE_URL:-}"
    ;;
  run\ services\ get-iam-policy\ *)
    printf '%s\n' "${GCLOUD_RUN_IAM_MEMBERS:-}"
    ;;
  run\ services\ add-iam-policy-binding\ *)
    :
    ;;
  iap\ web\ add-iam-policy-binding\ *)
    :
    ;;
  iap\ web\ get-iam-policy\ *)
    printf '%s\n' "${GCLOUD_IAP_ACCESSORS:-}"
    ;;
  alpha\ iap\ web\ add-iam-policy-binding\ *)
    :
    ;;
  alpha\ iap\ web\ get-iam-policy\ *)
    printf '%s\n' "${GCLOUD_IAP_ACCESSORS:-}"
    ;;
  alpha\ run\ deploy\ *)
    :
    ;;
  run\ deploy\ *)
    :
    ;;
  *)
    printf 'unexpected gcloud invocation: %s\n' "$*" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$STUB_BIN/gcloud"

cat >"$STUB_BIN/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' "$*" >> "${DOCKER_LOG:?}"
printf 'DOCKER_CONFIG=%s :: %s\n' "${DOCKER_CONFIG:-}" "$*" >> "${DOCKER_ENV_LOG:?}"

docker_command=${1:-}
if [[ "$docker_command" == --config=* ]]; then
  docker_command=${2:-}
fi

case "$docker_command" in
  login|build|push)
    :
    ;;
  *)
    printf 'unexpected docker invocation: %s\n' "$*" >&2
    exit 98
    ;;
esac
EOF
chmod +x "$STUB_BIN/docker"

cat >"$STUB_BIN/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' "$*" >> "${PYTHON_LOG:?}"
exec "${REAL_PYTHON3:?}" "$@"
EOF
chmod +x "$STUB_BIN/python3"

cat >"$STUB_BIN/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "-C" && "${3:-}" == "rev-parse" && "${4:-}" == "--short=12" && "${5:-}" == "HEAD" ]]; then
  [[ "${2:-}" == "${GIT_EXPECTED_REPO_ROOT:?}" ]] || {
    printf 'unexpected repository root: %s\n' "${2:-}" >&2
    exit 96
  }
  printf '%s\n' "${GIT_HEAD_SHA:-deadbeefcafe}"
  exit 0
fi

printf 'unexpected git invocation: %s\n' "$*" >&2
exit 97
EOF
chmod +x "$STUB_BIN/git"

FAILURES=0
CURRENT_CONFIG_PATH=
CURRENT_PROTECTED_PATH=

write_private_file() {
  local path=$1
  local body=$2
  printf '%s' "$body" >"$path"
  chmod 600 "$path"
}

write_valid_config() {
  local config_path=$1
  write_private_file "$config_path" "$(cat <<EOF
MIM_OPERATOR_EMAIL=$OPERATOR_EMAIL
MIM_PROJECT_ID=$PROJECT_ID
MIM_ORGANIZATION_ID=$ORGANIZATION_ID
MIM_BILLING_ACCOUNT_ID=$BILLING_ACCOUNT_ID
EOF
)"
}

write_valid_protected() {
  local protected_path=$1
  write_private_file "$protected_path" "$OTHER_PROJECT_ID"$'\n'
}

assert_contains() {
  local file=$1
  local expected=$2
  local case_name=$3
  if ! grep -Fq -- "$expected" "$file"; then
    printf 'FAIL %s: missing %s\n' "$case_name" "$expected" >&2
    cat "$file" >&2 || true
    FAILURES=$((FAILURES + 1))
  fi
}

assert_not_contains() {
  local file=$1
  local expected=$2
  local case_name=$3
  if grep -Fq -- "$expected" "$file"; then
    printf 'FAIL %s: unexpected %s\n' "$case_name" "$expected" >&2
    cat "$file" >&2 || true
    FAILURES=$((FAILURES + 1))
  fi
}

assert_output_redacted() {
  local case_name=$1
  local output_path=$2
  local value

  for value in "$OPERATOR_EMAIL" "$PROJECT_ID" "$ORGANIZATION_ID" "$BILLING_ACCOUNT_ID"; do
    if grep -Fq -- "$value" "$output_path"; then
      printf 'FAIL %s: leaked configured value in script output\n' "$case_name" >&2
      cat "$output_path" >&2 || true
      FAILURES=$((FAILURES + 1))
      return
    fi
  done
}

assert_scoped_gcloud_log() {
  local case_name=$1
  while IFS= read -r line; do
    case "$line" in
      *"--help"*|PREFLIGHT_ENV\ *)
        continue
        ;;
      auth\ list\ *)
        [[ "$line" == *"--account=$OPERATOR_EMAIL"* ]] || {
          printf 'FAIL %s: auth list missing --account\n' "$case_name" >&2
          printf '%s\n' "$line" >&2
          FAILURES=$((FAILURES + 1))
        }
        ;;
      *)
        [[ "$line" == *"--account=$OPERATOR_EMAIL"* ]] || {
          printf 'FAIL %s: gcloud call missing --account\n' "$case_name" >&2
          printf '%s\n' "$line" >&2
          FAILURES=$((FAILURES + 1))
        }
        [[ "$line" == *"--project=$PROJECT_ID"* ]] || {
          printf 'FAIL %s: gcloud call missing --project\n' "$case_name" >&2
          printf '%s\n' "$line" >&2
          FAILURES=$((FAILURES + 1))
        }
        ;;
    esac
  done <"$GCLOUD_LOG"
}

run_case() {
  local case_name=$1
  local expected_exit=$2
  local validator=$3
  local setup_cmd=${4:-:}
  local output_path="$TMP_DIR/$case_name.out"
  local config_path="$TMP_DIR/$case_name.env"
  local protected_path="$TMP_DIR/$case_name.protected"

  CURRENT_CONFIG_PATH=$config_path
  CURRENT_PROTECTED_PATH=$protected_path

  write_valid_config "$config_path"
  write_valid_protected "$protected_path"
  eval "$setup_cmd"
  : >"$GCLOUD_LOG"
  : >"$DOCKER_LOG"
  : >"$DOCKER_ENV_LOG"
  : >"$PYTHON_LOG"

  set +e
  env \
    PATH="$STUB_BIN:$PATH" \
    GCLOUD_LOG="$GCLOUD_LOG" \
    DOCKER_LOG="$DOCKER_LOG" \
    DOCKER_ENV_LOG="$DOCKER_ENV_LOG" \
    GCLOUD_RUN_DEPLOY_HELP="${GCLOUD_RUN_DEPLOY_HELP:-}" \
    GCLOUD_ALPHA_RUN_DEPLOY_HELP="${GCLOUD_ALPHA_RUN_DEPLOY_HELP:-}" \
    GCLOUD_IAP_BINDING_HELP="${GCLOUD_IAP_BINDING_HELP:-}" \
    GCLOUD_ALPHA_IAP_BINDING_HELP="${GCLOUD_ALPHA_IAP_BINDING_HELP:-}" \
    GCLOUD_CAPTURE_PREFLIGHT_ENV_ON_AUTH_LIST="${GCLOUD_CAPTURE_PREFLIGHT_ENV_ON_AUTH_LIST:-0}" \
    GCLOUD_MUTATE_ORIGINAL_FILES_ON_AUTH_LIST="${GCLOUD_MUTATE_ORIGINAL_FILES_ON_AUTH_LIST:-0}" \
    GCLOUD_MUTATE_CONFIG_PATH="${GCLOUD_MUTATE_CONFIG_PATH:-$config_path}" \
    GCLOUD_MUTATE_PROTECTED_PATH="${GCLOUD_MUTATE_PROTECTED_PATH:-$protected_path}" \
    GCLOUD_MUTATE_OPERATOR_EMAIL="${GCLOUD_MUTATE_OPERATOR_EMAIL:-$OPERATOR_EMAIL}" \
    GCLOUD_MUTATE_PROJECT_ID="${GCLOUD_MUTATE_PROJECT_ID:-$MUTATED_PROJECT_ID}" \
    GCLOUD_MUTATE_ORGANIZATION_ID="${GCLOUD_MUTATE_ORGANIZATION_ID:-$ORGANIZATION_ID}" \
    GCLOUD_MUTATE_BILLING_ACCOUNT_ID="${GCLOUD_MUTATE_BILLING_ACCOUNT_ID:-$BILLING_ACCOUNT_ID}" \
    GCLOUD_MUTATE_PROTECTED_BODY="${GCLOUD_MUTATE_PROTECTED_BODY:-$PROJECT_ID}"$'\n' \
    GCLOUD_ACTIVE_ACCOUNT="${GCLOUD_ACTIVE_ACCOUNT:-$OPERATOR_EMAIL}" \
    GCLOUD_ACCESS_TOKEN="${GCLOUD_ACCESS_TOKEN:-ya29.short-lived-token}" \
    GCLOUD_PROJECT_DESCRIBE_ID="${GCLOUD_PROJECT_DESCRIBE_ID:-$PROJECT_ID}" \
    GCLOUD_PROJECT_PARENT_TYPE="${GCLOUD_PROJECT_PARENT_TYPE:-organization}" \
    GCLOUD_PROJECT_PARENT_ID="${GCLOUD_PROJECT_PARENT_ID:-$ORGANIZATION_ID}" \
    GCLOUD_PROJECT_NUMBER="${GCLOUD_PROJECT_NUMBER:-$PROJECT_NUMBER}" \
    GCLOUD_BILLING_ENABLED="${GCLOUD_BILLING_ENABLED:-True}" \
    GCLOUD_BILLING_ACCOUNT_NAME="${GCLOUD_BILLING_ACCOUNT_NAME:-billingAccounts/$BILLING_ACCOUNT_ID}" \
    GCLOUD_ARTIFACT_REPO_STATUS="${GCLOUD_ARTIFACT_REPO_STATUS:-not_found}" \
    GCLOUD_ARTIFACT_REPO_FORMAT="${GCLOUD_ARTIFACT_REPO_FORMAT:-docker}" \
    GCLOUD_ARTIFACT_REPO_LOCATION="${GCLOUD_ARTIFACT_REPO_LOCATION:-asia-northeast3}" \
    GCLOUD_ARTIFACT_REPO_ERROR_TEXT="${GCLOUD_ARTIFACT_REPO_ERROR_TEXT:-PERMISSION_DENIED: repo describe failed}" \
    GCLOUD_ARTIFACT_REPO_ERROR_EXIT="${GCLOUD_ARTIFACT_REPO_ERROR_EXIT:-1}" \
    GCLOUD_RUNTIME_SA_STATUS="${GCLOUD_RUNTIME_SA_STATUS:-not_found}" \
    GCLOUD_RUNTIME_SA_ERROR_TEXT="${GCLOUD_RUNTIME_SA_ERROR_TEXT:-PERMISSION_DENIED: service account describe failed}" \
    GCLOUD_RUNTIME_SA_ERROR_EXIT="${GCLOUD_RUNTIME_SA_ERROR_EXIT:-1}" \
    GCLOUD_PROJECT_IAM_POLICY_OUTPUT="${GCLOUD_PROJECT_IAM_POLICY_OUTPUT-}" \
    GCLOUD_PROJECT_RUN_INVOKERS="${GCLOUD_PROJECT_RUN_INVOKERS-}" \
    GCLOUD_PROJECT_IAP_ACCESSORS="${GCLOUD_PROJECT_IAP_ACCESSORS-}" \
    GCLOUD_RUN_SERVICE_STATUS="${GCLOUD_RUN_SERVICE_STATUS:-not_found}" \
    GCLOUD_RUN_SERVICE_NAME="${GCLOUD_RUN_SERVICE_NAME:-mim-bootstrap}" \
    GCLOUD_RUN_RUNTIME_SERVICE_ACCOUNT="${GCLOUD_RUN_RUNTIME_SERVICE_ACCOUNT:-$EXPECTED_RUNTIME_SERVICE_ACCOUNT}" \
    GCLOUD_RUN_SERVICE_URL="${GCLOUD_RUN_SERVICE_URL:-$EXPECTED_SERVICE_URL}" \
    GCLOUD_RUN_IAM_MEMBERS="${GCLOUD_RUN_IAM_MEMBERS-$EXPECTED_IAP_SERVICE_AGENT}" \
    GCLOUD_IAP_ACCESSORS="${GCLOUD_IAP_ACCESSORS-user:$OPERATOR_EMAIL}" \
    GCLOUD_RUN_SERVICE_ERROR_TEXT="${GCLOUD_RUN_SERVICE_ERROR_TEXT:-PERMISSION_DENIED: service describe failed}" \
    GCLOUD_RUN_SERVICE_ERROR_EXIT="${GCLOUD_RUN_SERVICE_ERROR_EXIT:-1}" \
    GIT_HEAD_SHA="${GIT_HEAD_SHA:-deadbeefcafe}" \
    PYTHON_LOG="$PYTHON_LOG" \
    REAL_PYTHON3="$REAL_PYTHON3" \
    GIT_EXPECTED_REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)" \
    MIM_CONFIG_FILE="$config_path" \
    MIM_PROTECTED_PROJECTS_FILE="$protected_path" \
    bash "$APPLY_SCRIPT" >"$output_path" 2>&1
  local exit_code=$?
  set -e

  if [[ "$exit_code" -ne "$expected_exit" ]]; then
    printf 'FAIL %s: expected exit %s, got %s\n' "$case_name" "$expected_exit" "$exit_code" >&2
    cat "$output_path" >&2 || true
    FAILURES=$((FAILURES + 1))
    return
  fi

  "$validator" "$case_name" "$output_path"
}

validate_stable_case() {
  local case_name=$1
  local output_path=$2
  local docker_config_path

  assert_contains "$GCLOUD_LOG" "run deploy --help" "$case_name"
  assert_contains "$GCLOUD_LOG" "iap web add-iam-policy-binding --help" "$case_name"
  assert_not_contains "$GCLOUD_LOG" "alpha run deploy --help" "$case_name"
  assert_not_contains "$GCLOUD_LOG" "alpha iap web add-iam-policy-binding --help" "$case_name"
  assert_contains "$GCLOUD_LOG" "services enable run.googleapis.com iap.googleapis.com artifactregistry.googleapis.com iam.googleapis.com cloudresourcemanager.googleapis.com --account=$OPERATOR_EMAIL --project=$PROJECT_ID" "$case_name"
  assert_contains "$GCLOUD_LOG" "artifacts repositories create mim-bootstrap --location=asia-northeast3 --repository-format=docker --description=MIM bootstrap container images --account=$OPERATOR_EMAIL --project=$PROJECT_ID" "$case_name"
  assert_contains "$GCLOUD_LOG" "iam service-accounts create mim-bootstrap-runtime --display-name=MIM Bootstrap Runtime --description=MIM managed runtime identity with no project roles --account=$OPERATOR_EMAIL --project=$PROJECT_ID" "$case_name"
  assert_contains "$GCLOUD_LOG" "alpha services identity create --service=iap.googleapis.com --account=$OPERATOR_EMAIL --project=$PROJECT_ID" "$case_name"
  assert_contains "$GCLOUD_LOG" "run deploy mim-bootstrap" "$case_name"
  assert_contains "$GCLOUD_LOG" "--image=asia-northeast3-docker.pkg.dev/$PROJECT_ID/mim-bootstrap/mim-bootstrap:deadbeefcafe" "$case_name"
  assert_contains "$GCLOUD_LOG" "--service-account=$EXPECTED_RUNTIME_SERVICE_ACCOUNT" "$case_name"
  assert_contains "$GCLOUD_LOG" "--set-env-vars=MIM_HOSTNAME=mim.madup.app" "$case_name"
  assert_contains "$GCLOUD_LOG" "run services add-iam-policy-binding mim-bootstrap --member=$EXPECTED_IAP_SERVICE_AGENT --role=roles/run.invoker --region=asia-northeast3 --quiet --account=$OPERATOR_EMAIL --project=$PROJECT_ID" "$case_name"
  assert_contains "$GCLOUD_LOG" "iap web add-iam-policy-binding --member=user:$OPERATOR_EMAIL --role=roles/iap.httpsResourceAccessor --region=asia-northeast3 --resource-type=cloud-run --service=mim-bootstrap --quiet --account=$OPERATOR_EMAIL --project=$PROJECT_ID" "$case_name"
  assert_contains "$GCLOUD_LOG" "run services describe mim-bootstrap --region=asia-northeast3 --format=value(status.url) --account=$OPERATOR_EMAIL --project=$PROJECT_ID" "$case_name"
  assert_contains "$GCLOUD_LOG" "run services get-iam-policy mim-bootstrap --region=asia-northeast3 --flatten=bindings[].members --filter=bindings.role=roles/run.invoker --format=value(bindings.members) --account=$OPERATOR_EMAIL --project=$PROJECT_ID" "$case_name"
  assert_contains "$GCLOUD_LOG" "iap web get-iam-policy --region=asia-northeast3 --resource-type=cloud-run --service=mim-bootstrap --flatten=bindings[].members --filter=bindings.role=roles/iap.httpsResourceAccessor --format=value(bindings.members) --account=$OPERATOR_EMAIL --project=$PROJECT_ID" "$case_name"
  assert_not_contains "$GCLOUD_LOG" "projects add-iam-policy-binding" "$case_name"
  assert_not_contains "$GCLOUD_LOG" "allUsers" "$case_name"
  assert_contains "$DOCKER_LOG" "login --username oauth2accesstoken --password-stdin https://asia-northeast3-docker.pkg.dev" "$case_name"
  assert_contains "$DOCKER_LOG" "build --platform=linux/amd64 --tag asia-northeast3-docker.pkg.dev/$PROJECT_ID/mim-bootstrap/mim-bootstrap:deadbeefcafe" "$case_name"
  assert_contains "$DOCKER_LOG" "push asia-northeast3-docker.pkg.dev/$PROJECT_ID/mim-bootstrap/mim-bootstrap:deadbeefcafe" "$case_name"
  assert_contains "$PYTHON_LOG" "snapshot_private_files.py --snapshot Config file" "$case_name"
  assert_contains "$PYTHON_LOG" "--snapshot Protected project file" "$case_name"
  docker_config_path=$(sed -n '1s/^--config=\([^ ]*\).*/\1/p' "$DOCKER_LOG")
  if [[ -z "$docker_config_path" || -e "$docker_config_path" ]]; then
    printf 'FAIL %s: temporary DOCKER_CONFIG should be removed after apply\n' "$case_name" >&2
    cat "$DOCKER_ENV_LOG" >&2 || true
    FAILURES=$((FAILURES + 1))
  fi
  assert_scoped_gcloud_log "$case_name"
  assert_output_redacted "$case_name" "$output_path"
  assert_contains "$output_path" "Cloud Run bootstrap provisioning commands completed for mim-bootstrap." "$case_name"
  assert_contains "$output_path" "IAP-protected URL: $EXPECTED_SERVICE_URL" "$case_name"
}

validate_alpha_case() {
  local case_name=$1
  assert_contains "$GCLOUD_LOG" "run deploy --help" "$case_name"
  assert_contains "$GCLOUD_LOG" "alpha run deploy --help" "$case_name"
  assert_contains "$GCLOUD_LOG" "iap web add-iam-policy-binding --help" "$case_name"
  assert_contains "$GCLOUD_LOG" "alpha iap web add-iam-policy-binding --help" "$case_name"
  assert_contains "$GCLOUD_LOG" "alpha run deploy mim-bootstrap" "$case_name"
  assert_contains "$GCLOUD_LOG" "alpha iap web add-iam-policy-binding --member=user:$OPERATOR_EMAIL --role=roles/iap.httpsResourceAccessor --region=asia-northeast3 --resource-type=cloud-run --service=mim-bootstrap --quiet --account=$OPERATOR_EMAIL --project=$PROJECT_ID" "$case_name"
  if grep -Eq '^run deploy mim-bootstrap ' "$GCLOUD_LOG"; then
    printf 'FAIL %s: stable deploy should not be used when only alpha supports IAP\n' "$case_name" >&2
    FAILURES=$((FAILURES + 1))
  fi
  if grep -Eq '^iap web add-iam-policy-binding --member=' "$GCLOUD_LOG"; then
    printf 'FAIL %s: stable IAP binding should not be used when only alpha supports Cloud Run IAM\n' "$case_name" >&2
    FAILURES=$((FAILURES + 1))
  fi
  assert_scoped_gcloud_log "$case_name"
}

validate_snapshot_case() {
  local case_name=$1
  local output_path=$2
  local env_line

  env_line=$(grep '^PREFLIGHT_ENV ' "$GCLOUD_LOG" || true)
  if [[ -z "$env_line" ]]; then
    printf 'FAIL %s: missing preflight snapshot env capture\n' "$case_name" >&2
    FAILURES=$((FAILURES + 1))
    return
  fi
  [[ "$env_line" == *"MIM_CONFIG_FILE="* ]] || {
    printf 'FAIL %s: missing forwarded config snapshot path\n' "$case_name" >&2
    FAILURES=$((FAILURES + 1))
  }
  [[ "$env_line" == *"MIM_PROTECTED_PROJECTS_FILE="* ]] || {
    printf 'FAIL %s: missing forwarded protected snapshot path\n' "$case_name" >&2
    FAILURES=$((FAILURES + 1))
  }
  [[ "$env_line" != *"$CURRENT_CONFIG_PATH"* ]] || {
    printf 'FAIL %s: preflight should receive a snapshot config path\n' "$case_name" >&2
    FAILURES=$((FAILURES + 1))
  }
  [[ "$env_line" != *"$CURRENT_PROTECTED_PATH"* ]] || {
    printf 'FAIL %s: preflight should receive a snapshot protected path\n' "$case_name" >&2
    FAILURES=$((FAILURES + 1))
  }
  assert_not_contains "$GCLOUD_LOG" "--project=$MUTATED_PROJECT_ID" "$case_name"
  assert_output_redacted "$case_name" "$output_path"
  assert_contains "$output_path" "IAP-protected URL: $EXPECTED_SERVICE_URL" "$case_name"
}

validate_repo_mismatch_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Artifact Registry repository mim-bootstrap exists outside asia-northeast3" "$case_name"
  assert_not_contains "$DOCKER_LOG" "build" "$case_name"
}

validate_repo_format_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Artifact Registry repository mim-bootstrap must use docker format" "$case_name"
  assert_not_contains "$DOCKER_LOG" "build" "$case_name"
}

validate_missing_capability_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "gcloud SDK is too old for direct Cloud Run IAP deployment" "$case_name"
  assert_not_contains "$GCLOUD_LOG" "services enable" "$case_name"
}

validate_repo_error_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Unable to inspect Artifact Registry repository mim-bootstrap" "$case_name"
  assert_not_contains "$GCLOUD_LOG" "artifacts repositories create mim-bootstrap" "$case_name"
}

validate_runtime_sa_error_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Unable to inspect service account" "$case_name"
  assert_not_contains "$GCLOUD_LOG" "iam service-accounts create mim-bootstrap-runtime" "$case_name"
}

validate_run_service_error_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Unable to inspect Cloud Run service mim-bootstrap" "$case_name"
  assert_not_contains "$GCLOUD_LOG" "run deploy mim-bootstrap" "$case_name"
}

validate_rejects_broad_roles_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Runtime service account must not hold project role roles/editor" "$case_name"
  assert_not_contains "$GCLOUD_LOG" "run deploy mim-bootstrap" "$case_name"
}

validate_rejects_run_invoker_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Unexpected direct Cloud Run invoker binding" "$case_name"
  assert_not_contains "$DOCKER_LOG" "build" "$case_name"
}

validate_rejects_iap_accessor_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Unexpected Cloud Run IAP access binding" "$case_name"
  assert_not_contains "$DOCKER_LOG" "build" "$case_name"
}

validate_rejects_project_invoker_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Project-wide Cloud Run invoker bindings are forbidden" "$case_name"
  assert_not_contains "$DOCKER_LOG" "build" "$case_name"
}

validate_rejects_project_iap_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Unexpected project-wide IAP access binding" "$case_name"
  assert_not_contains "$DOCKER_LOG" "build" "$case_name"
}

validate_rejects_runtime_identity_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Cloud Run service mim-bootstrap uses an unexpected runtime identity" "$case_name"
  assert_not_contains "$DOCKER_LOG" "build" "$case_name"
}

validate_missing_expected_run_invoker_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "IAP service agent is missing the Cloud Run invoker role" "$case_name"
}

validate_missing_expected_iap_accessor_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Initial operator is missing Cloud Run IAP access" "$case_name"
}

validate_invalid_service_url_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Cloud Run returned an unexpected service URL" "$case_name"
  assert_not_contains "$output_path" "IAP-protected URL:" "$case_name"
}

validate_unsafe_git_sha_case() {
  local case_name=$1
  local output_path=$2
  assert_contains "$output_path" "Unable to determine a safe git image tag" "$case_name"
  assert_not_contains "$DOCKER_LOG" "build" "$case_name"
}

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
run_case uses_stable_run_deploy 0 validate_stable_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n' \
GCLOUD_ALPHA_RUN_DEPLOY_HELP=$'NAME\n  gcloud alpha run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n' \
GCLOUD_ALPHA_IAP_BINDING_HELP=$'NAME\n  gcloud alpha iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_RUNTIME_SA_STATUS=exists \
GCLOUD_RUN_SERVICE_STATUS=exists \
run_case uses_alpha_fallbacks 0 validate_alpha_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_CAPTURE_PREFLIGHT_ENV_ON_AUTH_LIST=1 \
GCLOUD_MUTATE_ORIGINAL_FILES_ON_AUTH_LIST=1 \
run_case uses_private_snapshots_for_preflight_and_apply 0 validate_snapshot_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_ARTIFACT_REPO_LOCATION=us-central1 \
run_case rejects_repo_region_mismatch 1 validate_repo_mismatch_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_ARTIFACT_REPO_FORMAT=maven2 \
run_case rejects_repo_format_mismatch 1 validate_repo_format_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n' \
GCLOUD_ALPHA_RUN_DEPLOY_HELP=$'NAME\n  gcloud alpha run deploy\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n' \
GCLOUD_ALPHA_IAP_BINDING_HELP=$'NAME\n  gcloud alpha iap web add-iam-policy-binding\n' \
run_case rejects_missing_capability 1 validate_missing_capability_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=error \
run_case rejects_repo_describe_error 1 validate_repo_error_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_RUNTIME_SA_STATUS=error \
run_case rejects_runtime_sa_describe_error 1 validate_runtime_sa_error_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_RUNTIME_SA_STATUS=exists \
GCLOUD_RUN_SERVICE_STATUS=error \
run_case rejects_run_service_describe_error 1 validate_run_service_error_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_RUNTIME_SA_STATUS=exists \
GCLOUD_PROJECT_IAM_POLICY_OUTPUT='roles/editor roles/bigquery.dataEditor' \
run_case rejects_broad_runtime_roles 1 validate_rejects_broad_roles_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_RUNTIME_SA_STATUS=exists \
GCLOUD_RUN_SERVICE_STATUS=exists \
GCLOUD_RUN_IAM_MEMBERS=user:unexpected@madup.com \
run_case rejects_unexpected_run_invoker 1 validate_rejects_run_invoker_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_RUNTIME_SA_STATUS=exists \
GCLOUD_RUN_SERVICE_STATUS=exists \
GCLOUD_IAP_ACCESSORS=group:unexpected@madup.com \
run_case rejects_unexpected_iap_accessor 1 validate_rejects_iap_accessor_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_RUNTIME_SA_STATUS=exists \
GCLOUD_RUN_SERVICE_STATUS=exists \
GCLOUD_PROJECT_RUN_INVOKERS=group:unexpected@madup.com \
run_case rejects_project_wide_run_invoker 1 validate_rejects_project_invoker_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_RUNTIME_SA_STATUS=exists \
GCLOUD_RUN_SERVICE_STATUS=exists \
GCLOUD_PROJECT_IAP_ACCESSORS=group:unexpected@madup.com \
run_case rejects_project_wide_iap_accessor 1 validate_rejects_project_iap_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_RUNTIME_SA_STATUS=exists \
GCLOUD_RUN_SERVICE_STATUS=exists \
GCLOUD_RUN_RUNTIME_SERVICE_ACCOUNT=unexpected@$PROJECT_ID.iam.gserviceaccount.com \
run_case rejects_existing_runtime_identity_mismatch 1 validate_rejects_runtime_identity_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_RUNTIME_SA_STATUS=exists \
GCLOUD_RUN_IAM_MEMBERS='' \
run_case rejects_missing_expected_run_invoker_binding 1 validate_missing_expected_run_invoker_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_RUNTIME_SA_STATUS=exists \
GCLOUD_IAP_ACCESSORS='' \
run_case rejects_missing_expected_iap_binding 1 validate_missing_expected_iap_accessor_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GCLOUD_ARTIFACT_REPO_STATUS=exists \
GCLOUD_RUNTIME_SA_STATUS=exists \
GCLOUD_RUN_SERVICE_STATUS=exists \
GCLOUD_RUN_SERVICE_URL="https://Synthetic-bootstrap-12345${RUN_APP_SUFFIX}/path" \
run_case rejects_invalid_reported_service_url 1 validate_invalid_service_url_case

GCLOUD_RUN_DEPLOY_HELP=$'NAME\n  gcloud run deploy\n  --[no-]iap\n' \
GCLOUD_IAP_BINDING_HELP=$'NAME\n  gcloud iap web add-iam-policy-binding\n  --resource-type=cloud-run\n  --service=SERVICE\n' \
GIT_HEAD_SHA=not-safe-tag \
run_case rejects_unsafe_git_sha 1 validate_unsafe_git_sha_case

if [[ "$FAILURES" -ne 0 ]]; then
  exit 1
fi
