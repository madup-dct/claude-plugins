#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLUGIN_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
REPO_ROOT=$(cd "$PLUGIN_ROOT/../.." && pwd)
PREFLIGHT_SCRIPT="$SCRIPT_DIR/preflight.sh"
CONFIG_FILE="${MIM_CONFIG_FILE:-$SCRIPT_DIR/config.env}"
PROTECTED_PROJECTS_FILE="${MIM_PROTECTED_PROJECTS_FILE:-$SCRIPT_DIR/protected-projects.exact}"
BOOTSTRAP_DIR="$PLUGIN_ROOT/control-plane/bootstrap"
SNAPSHOT_HELPER="$SCRIPT_DIR/snapshot_private_files.py"

# shellcheck source=config_lib.sh
. "$SCRIPT_DIR/config_lib.sh"

SERVICE_NAME=mim-bootstrap
ARTIFACT_REPOSITORY=mim-bootstrap
RUNTIME_SERVICE_ACCOUNT_NAME=mim-bootstrap-runtime
DOCKER_CONFIG_DIR=
SNAPSHOT_DIR=
SNAPSHOT_CONFIG_FILE=
SNAPSHOT_PROTECTED_PROJECTS_FILE=
INSPECT_STATE=
INSPECT_OUTPUT=
RUN_SERVICE_EXISTS=0

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

cleanup() {
  if [[ -n "${DOCKER_CONFIG_DIR:-}" && -d "$DOCKER_CONFIG_DIR" ]]; then
    rm -rf -- "$DOCKER_CONFIG_DIR"
  fi
  if [[ -n "${SNAPSHOT_DIR:-}" && -d "$SNAPSHOT_DIR" ]]; then
    rm -rf -- "$SNAPSHOT_DIR"
  fi
}
trap cleanup EXIT

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 is required"
}

run_gcloud() {
  gcloud "$@" --account="$MIM_OPERATOR_EMAIL" --project="$MIM_PROJECT_ID"
}

capture_gcloud() {
  local description=$1
  shift

  local output
  if ! output=$(run_gcloud "$@" 2>/dev/null); then
    fail "$description"
  fi
  printf '%s' "$output"
}

# Sets INSPECT_STATE to exists or missing and stores successful output in
# INSPECT_OUTPUT. Successful stderr diagnostics are kept separate from the
# formatted stdout value. Only an explicit, reviewed missing marker means the
# resource is absent; permission, auth, API, and network failures stop apply.
inspect_gcloud_resource() {
  local description=$1
  local missing_marker=$2
  shift 2

  local status
  local error_file
  local error_output
  error_file=$(mktemp)
  set +e
  INSPECT_OUTPUT=$(run_gcloud "$@" 2>"$error_file")
  status=$?
  set -e
  error_output=$(<"$error_file")
  rm -f -- "$error_file"

  if [[ "$status" -eq 0 ]]; then
    INSPECT_STATE=exists
    return
  fi

  if [[ "$error_output" == *"NOT_FOUND:"* || \
    ( -n "$missing_marker" && "$error_output" == *"$missing_marker"* ) ]]; then
    INSPECT_STATE=missing
    INSPECT_OUTPUT=
    return
  fi

  fail "$description"
}

create_private_snapshots() {
  local source_config_file=$1
  local source_protected_projects_file=$2

  SNAPSHOT_DIR=$(mktemp -d)
  chmod 700 "$SNAPSHOT_DIR"

  SNAPSHOT_CONFIG_FILE="$SNAPSHOT_DIR/config.env"
  SNAPSHOT_PROTECTED_PROJECTS_FILE="$SNAPSHOT_DIR/protected-projects.exact"

  python3 "$SNAPSHOT_HELPER" \
    --snapshot "Config file" "$source_config_file" "$SNAPSHOT_CONFIG_FILE" 65536 \
    --snapshot "Protected project file" "$source_protected_projects_file" "$SNAPSHOT_PROTECTED_PROJECTS_FILE" 1048576
}

run_deploy_capability() {
  if gcloud run deploy --help 2>/dev/null | grep -Fq -- '--[no-]iap'; then
    printf 'stable'
    return
  fi

  if gcloud alpha run deploy --help 2>/dev/null | grep -Fq -- '--[no-]iap'; then
    printf 'alpha'
    return
  fi

  fail "gcloud SDK is too old for direct Cloud Run IAP deployment"
}

iap_binding_capability() {
  local help_output

  if help_output=$(gcloud iap web add-iam-policy-binding --help 2>/dev/null) && \
    [[ "$help_output" == *"--resource-type"* && "$help_output" == *"cloud-run"* ]]; then
    printf 'stable'
    return
  fi

  if help_output=$(gcloud alpha iap web add-iam-policy-binding --help 2>/dev/null) && \
    [[ "$help_output" == *"--resource-type"* && "$help_output" == *"cloud-run"* ]]; then
    printf 'alpha'
    return
  fi

  fail "gcloud SDK is too old for Cloud Run IAP access bindings"
}

ensure_artifact_repository() {
  inspect_gcloud_resource \
    "Unable to inspect Artifact Registry repository $ARTIFACT_REPOSITORY" \
    '' \
    artifacts repositories describe "$ARTIFACT_REPOSITORY" \
    --location="$MIM_FIXED_REGION" \
    '--format=value(format)'

  if [[ "$INSPECT_STATE" == missing ]]; then
    printf 'Creating Artifact Registry repository %s in %s.\n' "$ARTIFACT_REPOSITORY" "$MIM_FIXED_REGION" >&2
    run_gcloud artifacts repositories create "$ARTIFACT_REPOSITORY" \
      --location="$MIM_FIXED_REGION" \
      --repository-format=docker \
      '--description=MIM bootstrap container images' >/dev/null
    return
  fi

  [[ "$INSPECT_OUTPUT" == docker || "$INSPECT_OUTPUT" == DOCKER ]] || \
    fail "Artifact Registry repository $ARTIFACT_REPOSITORY must use docker format"

  inspect_gcloud_resource \
    "Unable to inspect Artifact Registry repository location for $ARTIFACT_REPOSITORY" \
    '' \
    artifacts repositories describe "$ARTIFACT_REPOSITORY" \
    --location="$MIM_FIXED_REGION" \
    '--format=value(name)'
  [[ "$INSPECT_STATE" == exists ]] || fail "Artifact Registry repository $ARTIFACT_REPOSITORY disappeared during inspection"
  [[ "$INSPECT_OUTPUT" == "projects/$MIM_PROJECT_ID/locations/$MIM_FIXED_REGION/repositories/$ARTIFACT_REPOSITORY" ]] || \
    fail "Artifact Registry repository $ARTIFACT_REPOSITORY exists outside $MIM_FIXED_REGION"
}

ensure_runtime_service_account() {
  local email="${RUNTIME_SERVICE_ACCOUNT_NAME}@${MIM_PROJECT_ID}.iam.gserviceaccount.com"

  inspect_gcloud_resource \
    "Unable to inspect service account" \
    '' \
    iam service-accounts describe "$email" \
    '--format=value(email)'

  if [[ "$INSPECT_STATE" == missing ]]; then
    printf 'Creating bootstrap runtime service account.\n' >&2
    run_gcloud iam service-accounts create "$RUNTIME_SERVICE_ACCOUNT_NAME" \
      '--display-name=MIM Bootstrap Runtime' \
      '--description=MIM managed runtime identity with no project roles' >/dev/null
  else
    [[ -z "$INSPECT_OUTPUT" || "$INSPECT_OUTPUT" == "$email" ]] || fail "Runtime service account identity mismatch"
  fi

  local roles
  roles=$(capture_gcloud \
    "Unable to inspect runtime service account project roles" \
    projects get-iam-policy "$MIM_PROJECT_ID" \
    '--flatten=bindings[].members' \
    "--filter=bindings.members:serviceAccount:$email" \
    '--format=value(bindings.role)')

  local role
  for role in $roles; do
    [[ -z "$role" ]] || fail "Runtime service account must not hold project role $role"
  done

  printf '%s' "$email"
}

inspect_existing_run_service() {
  local runtime_service_account=$1

  inspect_gcloud_resource \
    "Unable to inspect Cloud Run service $SERVICE_NAME" \
    "Cannot find service [$SERVICE_NAME]" \
    run services describe "$SERVICE_NAME" \
    --region="$MIM_FIXED_REGION" \
    '--format=value(metadata.name)'

  if [[ "$INSPECT_STATE" == missing ]]; then
    RUN_SERVICE_EXISTS=0
    printf 'Creating Cloud Run service %s.\n' "$SERVICE_NAME" >&2
    return
  fi

  RUN_SERVICE_EXISTS=1
  [[ "$INSPECT_OUTPUT" == "$SERVICE_NAME" ]] || fail "Cloud Run service name mismatch for $SERVICE_NAME"

  inspect_gcloud_resource \
    "Unable to inspect Cloud Run runtime identity for $SERVICE_NAME" \
    '' \
    run services describe "$SERVICE_NAME" \
    --region="$MIM_FIXED_REGION" \
    '--format=value(spec.template.spec.serviceAccountName)'
  [[ "$INSPECT_STATE" == exists ]] || fail "Cloud Run service $SERVICE_NAME disappeared during inspection"
  [[ "$INSPECT_OUTPUT" == "$runtime_service_account" ]] || \
    fail "Cloud Run service $SERVICE_NAME uses an unexpected runtime identity"

  printf 'Updating Cloud Run service %s.\n' "$SERVICE_NAME" >&2
}

build_and_push_image() {
  local image_uri=$1
  local registry="${MIM_FIXED_REGION}-docker.pkg.dev"

  DOCKER_CONFIG_DIR=$(mktemp -d)

  printf 'Building bootstrap image.\n' >&2
  run_gcloud auth print-access-token | \
    docker --config="$DOCKER_CONFIG_DIR" login \
      --username oauth2accesstoken \
      --password-stdin \
      "https://$registry" >/dev/null
  docker build --platform=linux/amd64 --tag "$image_uri" "$BOOTSTRAP_DIR"
  docker --config="$DOCKER_CONFIG_DIR" push "$image_uri"
}

deploy_run_service() {
  local deploy_surface=$1
  local image_uri=$2
  local runtime_service_account=$3
  local -a command=(gcloud run deploy)

  if [[ "$deploy_surface" == alpha ]]; then
    command=(gcloud alpha run deploy)
  fi

  "${command[@]}" "$SERVICE_NAME" \
    --image="$image_uri" \
    --region="$MIM_FIXED_REGION" \
    --no-allow-unauthenticated \
    --iap \
    --cpu-throttling \
    --no-cpu-boost \
    --min=0 \
    --max=1 \
    --max-instances=1 \
    --cpu=1 \
    --memory=512Mi \
    --concurrency=20 \
    --ingress=all \
    --service-account="$runtime_service_account" \
    --set-env-vars="MIM_HOSTNAME=$MIM_FIXED_HOSTNAME" \
    --quiet \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"
}

grant_iap_user_access() {
  local binding_surface=$1
  local -a command=(gcloud iap web add-iam-policy-binding)

  if [[ "$binding_surface" == alpha ]]; then
    command=(gcloud alpha iap web add-iam-policy-binding)
  fi

  "${command[@]}" \
    --member="$MIM_IAP_MEMBER" \
    --role=roles/iap.httpsResourceAccessor \
    --region="$MIM_FIXED_REGION" \
    --resource-type=cloud-run \
    --service="$SERVICE_NAME" \
    --quiet \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" >/dev/null
}

verify_project_access_boundary() {
  local members

  members=$(capture_gcloud \
    "Unable to inspect project-wide Cloud Run invoker bindings" \
    projects get-iam-policy "$MIM_PROJECT_ID" \
    '--flatten=bindings[].members' \
    '--filter=bindings.role=roles/run.invoker' \
    '--format=value(bindings.members)')
  [[ -z "$members" ]] || fail "Project-wide Cloud Run invoker bindings are forbidden"

  members=$(capture_gcloud \
    "Unable to inspect project-wide IAP access bindings" \
    projects get-iam-policy "$MIM_PROJECT_ID" \
    '--flatten=bindings[].members' \
    '--filter=bindings.role=roles/iap.httpsResourceAccessor' \
    '--format=value(bindings.members)')

  local member
  while IFS= read -r member; do
    [[ -z "$member" ]] && continue
    [[ "$member" == "$MIM_IAP_MEMBER" ]] || \
      fail "Unexpected project-wide IAP access binding"
  done <<<"$members"
}

verify_run_invoker_boundary() {
  local expected_member=$1
  local require_expected=$2
  local members
  members=$(capture_gcloud \
    "Unable to inspect Cloud Run invoker bindings" \
    run services get-iam-policy "$SERVICE_NAME" \
    --region="$MIM_FIXED_REGION" \
    '--flatten=bindings[].members' \
    '--filter=bindings.role=roles/run.invoker' \
    '--format=value(bindings.members)')

  local found_expected=0
  local member
  while IFS= read -r member; do
    [[ -z "$member" ]] && continue
    [[ "$member" == "$expected_member" ]] || fail "Unexpected direct Cloud Run invoker binding"
    found_expected=1
  done <<<"$members"

  if [[ "$require_expected" == true && "$found_expected" -ne 1 ]]; then
    fail "IAP service agent is missing the Cloud Run invoker role"
  fi
}

verify_iap_access_boundary() {
  local binding_surface=$1
  local require_expected=$2
  local -a command=(gcloud iap web get-iam-policy)
  if [[ "$binding_surface" == alpha ]]; then
    command=(gcloud alpha iap web get-iam-policy)
  fi

  local members
  if ! members=$("${command[@]}" \
    --region="$MIM_FIXED_REGION" \
    --resource-type=cloud-run \
    --service="$SERVICE_NAME" \
    '--flatten=bindings[].members' \
    '--filter=bindings.role=roles/iap.httpsResourceAccessor' \
    '--format=value(bindings.members)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" 2>/dev/null); then
    fail "Unable to inspect Cloud Run IAP access bindings"
  fi

  local found_expected=0
  local member
  while IFS= read -r member; do
    [[ -z "$member" ]] && continue
    [[ "$member" == "$MIM_IAP_MEMBER" ]] || fail "Unexpected Cloud Run IAP access binding"
    found_expected=1
  done <<<"$members"

  if [[ "$require_expected" == true && "$found_expected" -ne 1 ]]; then
    fail "Initial operator is missing Cloud Run IAP access"
  fi
}

main() {
  [[ -f "$PREFLIGHT_SCRIPT" ]] || fail "Preflight script not found: $PREFLIGHT_SCRIPT"
  [[ -f "$SNAPSHOT_HELPER" ]] || fail "Snapshot helper not found: $SNAPSHOT_HELPER"
  [[ -d "$BOOTSTRAP_DIR" ]] || fail "Bootstrap source not found: $BOOTSTRAP_DIR"
  [[ -e "$REPO_ROOT/.git" ]] || fail "Repository root not found: $REPO_ROOT"

  require_command gcloud
  require_command docker
  require_command git
  require_command python3

  create_private_snapshots "$CONFIG_FILE" "$PROTECTED_PROJECTS_FILE"

  MIM_CONFIG_FILE="$SNAPSHOT_CONFIG_FILE" \
    MIM_PROTECTED_PROJECTS_FILE="$SNAPSHOT_PROTECTED_PROJECTS_FILE" \
    bash "$PREFLIGHT_SCRIPT"
  mim_load_config "$SNAPSHOT_CONFIG_FILE"
  mim_assert_project_not_protected "$MIM_PROJECT_ID" "$SNAPSHOT_PROTECTED_PROJECTS_FILE"
  readonly MIM_IAP_MEMBER="$(mim_derive_iap_member)"

  local deploy_surface
  local binding_surface
  deploy_surface=$(run_deploy_capability)
  binding_surface=$(iap_binding_capability)

  run_gcloud services enable \
    run.googleapis.com \
    iap.googleapis.com \
    artifactregistry.googleapis.com \
    iam.googleapis.com \
    cloudresourcemanager.googleapis.com >/dev/null

  local project_number
  project_number=$(capture_gcloud \
    "Unable to determine the project number" \
    projects describe "$MIM_PROJECT_ID" \
    '--format=value(projectNumber)')
  [[ "$project_number" =~ ^[0-9]+$ ]] || fail "Invalid project number returned"

  verify_project_access_boundary
  ensure_artifact_repository

  local runtime_service_account
  runtime_service_account=$(ensure_runtime_service_account)
  inspect_existing_run_service "$runtime_service_account"

  local iap_service_agent="serviceAccount:service-${project_number}@gcp-sa-iap.iam.gserviceaccount.com"
  if [[ "$RUN_SERVICE_EXISTS" -eq 1 ]]; then
    verify_run_invoker_boundary "$iap_service_agent" false
    verify_iap_access_boundary "$binding_surface" false
  fi

  local git_sha
  git_sha=$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)
  [[ "$git_sha" =~ ^[0-9a-f]{12}$ ]] || fail "Unable to determine a safe git image tag"

  local image_uri="${MIM_FIXED_REGION}-docker.pkg.dev/${MIM_PROJECT_ID}/${ARTIFACT_REPOSITORY}/${SERVICE_NAME}:${git_sha}"
  build_and_push_image "$image_uri"

  run_gcloud alpha services identity create --service=iap.googleapis.com >/dev/null
  deploy_run_service "$deploy_surface" "$image_uri" "$runtime_service_account"

  run_gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
    --member="$iap_service_agent" \
    --role=roles/run.invoker \
    --region="$MIM_FIXED_REGION" \
    --quiet >/dev/null

  grant_iap_user_access "$binding_surface"
  verify_run_invoker_boundary "$iap_service_agent" true
  verify_iap_access_boundary "$binding_surface" true

  local reported_service_url
  reported_service_url=$(capture_gcloud \
    "Unable to determine the Cloud Run service URL" \
    run services describe "$SERVICE_NAME" \
    --region="$MIM_FIXED_REGION" \
    '--format=value(status.url)')
  # Ownership comes from the explicit service/project/region describe command
  # above; this validator only rejects malformed or cross-origin URLs.
  mim_is_safe_run_service_url "$reported_service_url" || fail "Cloud Run returned an unexpected service URL"

  printf 'Cloud Run bootstrap provisioning commands completed for %s.\n' "$SERVICE_NAME"
  printf 'IAP-protected URL: %s\n' "$reported_service_url"
}

main "$@"
