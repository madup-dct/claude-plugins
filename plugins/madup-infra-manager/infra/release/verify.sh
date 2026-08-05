#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(git -C "${script_dir}" rev-parse --show-toplevel 2>/dev/null)" || {
  printf 'verify.sh must run inside the repository\n' >&2
  exit 1
}
script_path="${repo_root}/plugins/madup-infra-manager/infra/release/verify.sh"
scanner="${repo_root}/plugins/madup-infra-manager/infra/release/public_release_guard.py"
default_denylist="${repo_root}/plugins/madup-infra-manager/infra/release/denylist.exact"
plugin_root="${repo_root}/plugins/madup-infra-manager"
control_plane_root="${plugin_root}/control-plane"
control_plane_infra="${plugin_root}/infra/control-plane"
smoke_test="${script_dir}/smoke_test.sh"
app_gateway_root="${plugin_root}/app-gateway-go"

require_regular_readable_file() {
  local path="${1}"
  local label="${2}"
  if [[ -L "${path}" ]]; then
    printf '%s must not be a symlink\n' "${label}" >&2
    exit 1
  fi
  if [[ ! -f "${path}" || ! -r "${path}" ]]; then
    printf '%s is unavailable\n' "${label}" >&2
    exit 1
  fi
}

require_regular_readable_file "${script_path}" "release verifier"
require_regular_readable_file "${scanner}" "public release scanner"

if [[ ! -f "${scanner}" || ! -r "${scanner}" ]]; then
  printf 'public release scanner is unavailable\n' >&2
  exit 1
fi

run_contract_tests() {
  python3 -m unittest \
    tests/test_public_release_guard.py \
    tests/test_mim_release_contract.py \
    tests/test_madup_infra_manager_plugin.py \
    tests/test_mim_public_boundary.py \
    -v
}

run_gate() {
  local gate_name="${1}"
  local gate_status
  shift
  set +e
  (
    set -e
    "$@"
  )
  gate_status=$?
  set -e
  if [[ "${gate_status}" -eq 0 ]]; then
    printf '[PASS] %s\n' "${gate_name}"
    return 0
  fi
  printf '[FAIL] %s\n' "${gate_name}" >&2
  return "${gate_status}"
}

verify_mutation_gate() {
  local mutation_value="${MIM_ENABLE_MUTATIONS:-false}"
  case "${mutation_value}" in
    false)
      return 0
      ;;
    true)
      if [[ "${MIM_RELEASE_STAGE:-local}" == staging ]]; then
        [[ "${MIM_STAGING_MUTATION_CANARY:-false}" == true ]] || {
          printf 'staging mutations require MIM_STAGING_MUTATION_CANARY=true\n' >&2
          return 1
        }
      fi
      return 0
      ;;
    *)
      printf 'MIM_ENABLE_MUTATIONS must be exact true or false\n' >&2
      return 1
      ;;
  esac
}

run_plugin_validation() {
  claude plugin validate --strict "${plugin_root}"
}

run_python_lint() {
  (
    cd -- "${control_plane_root}"
    uv run ruff check src tests
  )
}

run_python_typecheck() {
  (
    cd -- "${control_plane_root}"
    uv run mypy
  )
}

run_python_unit() {
  (
    cd -- "${control_plane_root}"
    uv run python -m unittest discover -s tests -p 'test_*.py'
  )
  (
    cd -- "${plugin_root}"
    python3 -m unittest infra/runtime-bootstrap/test_bootstrap_contract.py
  )
  (
    cd -- "${repo_root}"
    run_contract_tests
  )
}

run_python_integration() {
  (
    cd -- "${control_plane_root}"
    uv run python -m unittest discover -s tests/integration -t . -p 'test_*.py'
  )
}

slack_mode_enabled() {
  local slack_value="${MIM_SLACK_ENABLED:-false}"
  case "${slack_value}" in
    true)
      return 0
      ;;
    false)
      return 1
      ;;
    *)
      printf 'MIM_SLACK_ENABLED must be exact true or false\n' >&2
      return 2
      ;;
  esac
}

verify_slack_mode_gate() {
  local slack_value="${MIM_SLACK_ENABLED:-false}"
  case "${slack_value}" in
    true|false)
      return 0
      ;;
    *)
      printf 'MIM_SLACK_ENABLED must be exact true or false\n' >&2
      return 1
      ;;
  esac
}

run_staging_contracts() {
  (
    cd -- "${control_plane_root}"
    run_gate direct-origin-denial \
      uv run python -m unittest tests.staging.test_cloudflare_origin_canary -v
    run_gate runtime-iam-canary \
      uv run python -m unittest tests.staging.test_runtime_iam_canary -v
    run_gate sensitive-project-denial \
      uv run python -m unittest tests.staging.test_sensitive_project_denials -v
    if slack_mode_enabled; then
      run_gate slack-oauth-canary \
        uv run python -m unittest tests.staging.test_slack_oauth_canary -v
    fi
    run_gate staging-canary-contract \
      uv run python -m unittest tests.test_staging_canary_contract -v
  )
}

run_shell_suites() {
  local test_script
  local test_scripts=(
    "${plugin_root}/infra/domain/test_preflight.sh"
    "${plugin_root}/infra/domain/test_apply_cloud_run.sh"
    "${control_plane_infra}/test_prepare_config.sh"
    "${control_plane_infra}/test_preflight.sh"
    "${control_plane_infra}/test_apply.sh"
    "${plugin_root}/infra/billing/test_plan.sh"
    "${plugin_root}/infra/billing/test_apply.sh"
    "${plugin_root}/infra/github/test_preflight.sh"
    "${plugin_root}/infra/github/test_plan_connection.sh"
    "${plugin_root}/infra/github/test_apply_connection.sh"
    "${plugin_root}/infra/runtime-bootstrap/test_prepare_input.sh"
    "${plugin_root}/infra/runtime-bootstrap/test_plan.sh"
    "${plugin_root}/infra/runtime-bootstrap/test_apply.sh"
    "${script_dir}/test_task18_lib.sh"
    "${script_dir}/test_plan.sh"
    "${script_dir}/test_apply.sh"
    "${plugin_root}/builder/test_builder.sh"
  )
  for test_script in "${test_scripts[@]}"; do
    [[ -f "${test_script}" && ! -L "${test_script}" ]] || return 1
    bash "${test_script}"
  done
}

run_edge_tests() {
  bash "${plugin_root}/infra/edge/test_plan.sh"
  bash "${plugin_root}/infra/edge/test_apply.sh"
  npm --prefix "${plugin_root}/edge/worker" test
}

run_go_gateway_checks() {
  (
    cd -- "${app_gateway_root}"
    go test ./...
    go vet ./...
    go test -race ./...
    CGO_ENABLED=0 go build ./cmd/mim-app-gateway
  )
}

run_container_builds() {
  docker build --platform=linux/amd64 \
    --tag mim-control-plane:release-check \
    "${control_plane_root}"
  docker build --platform=linux/amd64 \
    --tag mim-app-gateway:release-check \
    "${app_gateway_root}"
  docker build --platform=linux/amd64 \
    --tag mim-builder:release-check \
    "${plugin_root}/builder"
}

run_secret_scan() {
  local temp_denylist
  temp_denylist="$(mktemp "${TMPDIR:-/tmp}/mim-release-local-denylist.XXXXXX")"
  cleanup_secret_scan() {
    rm -f -- "${temp_denylist}"
  }
  trap cleanup_secret_scan RETURN
  chmod 0600 "${temp_denylist}"
  MIM_PUBLIC_RELEASE_DENYLIST_FILE="${temp_denylist}" \
    python3 "${scanner}" verify --local
  cleanup_secret_scan
  trap - RETURN
}

run_local_matrix() {
  run_gate slack-mode verify_slack_mode_gate
  run_gate mutation-gate verify_mutation_gate
  run_gate plugin-validation run_plugin_validation
  run_gate python-lint run_python_lint
  run_gate python-typecheck run_python_typecheck
  run_gate python-unit run_python_unit
  run_gate python-integration run_python_integration
  run_gate staging-contracts run_staging_contracts
  run_gate shell-suites run_shell_suites
  run_gate edge-tests run_edge_tests
  run_gate go-app-gateway run_go_gateway_checks
  run_gate container-build run_container_builds
  run_gate secret-scan run_secret_scan
}

require_staging_configuration() {
  [[ -n "${MIM_CONFIG_FILE:-}" ]] || {
    printf 'MIM_CONFIG_FILE is required for staging verification\n' >&2
    return 1
  }
  require_regular_readable_file "${MIM_CONFIG_FILE}" "staging operator config"
  [[ "${MIM_STAGING_BASE_URL:-}" == https://mim.madup.app ]] || {
    printf 'MIM_STAGING_BASE_URL must be the exact MIM origin\n' >&2
    return 1
  }
}

run_iam_policy_diff() {
  MIM_CONFIG_FILE="${MIM_CONFIG_FILE}" bash "${control_plane_infra}/audit_iam.sh"
}

run_authenticated_readonly_smoke() {
  require_regular_readable_file "${smoke_test}" "authenticated read-only smoke"
  MIM_CONFIG_FILE="${MIM_CONFIG_FILE}" \
    MIM_STAGING_BASE_URL="${MIM_STAGING_BASE_URL}" \
    bash "${smoke_test}" --read-only
}

run_public_app_live() {
  local required_envs=(
    MIM_STAGING_APP_GATEWAY_RUN_APP_URL
    MIM_STAGING_PRIVATE_WORKLOAD_RUN_APP_URL
    MIM_STAGING_APP_HOST_URL
    MIM_STAGING_APP_OWNER_CF_AUTHORIZATION_FILE
    MIM_STAGING_APP_ADMIN_CF_AUTHORIZATION_FILE
    MIM_STAGING_APP_OTHER_CF_AUTHORIZATION_FILE
  )
  local missing=()
  local name
  for name in "${required_envs[@]}"; do
    if [[ -z "${!name:-}" ]]; then
      missing+=("${name}")
    fi
  done
  if [[ "${#missing[@]}" -ne 0 ]]; then
    printf 'public app live canary prerequisites missing: %s\n' "${missing[*]}" >&2
    return 1
  fi
  require_regular_readable_file "${smoke_test}" "public app smoke"
  bash "${smoke_test}" --public-app
}

run_staging_matrix() {
  export MIM_RELEASE_STAGE=staging
  export MIM_REQUIRE_STAGING_CANARIES=true
  require_staging_configuration
  run_local_matrix
  run_gate iam-policy-diff run_iam_policy_diff
  run_gate authenticated-readonly-smoke run_authenticated_readonly_smoke
  run_gate public-app-live run_public_app_live
}

if [[ $# -eq 1 && "${1}" == "--ci" ]]; then
  printf 'CI note: verify.sh --ci cannot approve the first public push because the ignored exact denylist is unavailable in CI.\n'
  (
    cd -- "${repo_root}"
    temp_denylist="$(mktemp "${TMPDIR:-/tmp}/mim-public-release-ci-denylist.XXXXXX")"
    cleanup() {
      rm -f -- "${temp_denylist}"
    }
    trap cleanup EXIT HUP INT TERM
    chmod 0600 "${temp_denylist}"
    MIM_PUBLIC_RELEASE_DENYLIST_FILE="${temp_denylist}" python3 "${scanner}" verify --local
    unset MIM_PUBLIC_RELEASE_DENYLIST_FILE
    run_contract_tests
  )
  exit 0
fi

if [[ $# -eq 2 && "${1}" == "--release" ]]; then
  base_ref="${2}"
  if [[ -z "${base_ref}" || "${base_ref}" == -* ]]; then
    printf 'release base ref must be one safe non-option ref\n' >&2
    exit 2
  fi
  (
    cd -- "${repo_root}"
    MIM_PUBLIC_RELEASE_DENYLIST_FILE="${default_denylist}" python3 "${scanner}" verify --local --base-ref "${base_ref}" --require-exact-values
    MIM_PUBLIC_RELEASE_DENYLIST_FILE="${default_denylist}" python3 "${scanner}" verify --range "${base_ref}..HEAD"
    unset MIM_PUBLIC_RELEASE_DENYLIST_FILE
    run_contract_tests
  )
  exit 0
fi

if [[ $# -eq 1 && "${1}" == "--local" ]]; then
  (
    cd -- "${repo_root}"
    run_local_matrix
  )
  exit 0
fi

if [[ $# -eq 1 && "${1}" == "--staging" ]]; then
  (
    cd -- "${repo_root}"
    run_staging_matrix
  )
  exit 0
fi

printf 'usage: %s --ci | --release BASE_REF | --local | --staging\n' "plugins/madup-infra-manager/infra/release/verify.sh" >&2
exit 2
