#!/usr/bin/env bash
set -euo pipefail

readonly exact_origin="https://mim.madup.app"
readonly fixed_region="asia-northeast3"

usage() {
  printf 'usage: smoke_test.sh --read-only | --public-app\n' >&2
}

stat_mode() {
  local path="$1"
  if stat -f '%Lp' "$path" >/dev/null 2>&1; then
    stat -f '%Lp' "$path"
    return
  fi
  stat -c '%a' "$path"
}

require_exact_env() {
  local name="$1"
  local expected="$2"
  local value="${!name:-}"
  [[ -n "${value}" ]] || {
    printf '%s is required\n' "${name}" >&2
    exit 1
  }
  [[ "${value}" == "${expected}" ]] || {
    printf '%s must match the exact approved value\n' "${name}" >&2
    exit 1
  }
}

require_env() {
  local name="$1"
  [[ -n "${!name:-}" ]] || {
    printf '%s is required\n' "${name}" >&2
    exit 1
  }
}

reviewed_project_number() {
  require_env MIM_STAGING_PROJECT_NUMBER
  local project_number="${MIM_STAGING_PROJECT_NUMBER}"
  [[ "${project_number}" =~ ^[1-9][0-9]{11}$ ]] || {
    printf 'MIM_STAGING_PROJECT_NUMBER must be an exact reviewed project number\n' >&2
    exit 1
  }
  printf '%s' "${project_number}"
}

deterministic_run_app_origin() {
  local service_name="$1"
  local project_number="$2"
  printf 'https://%s-%s.%s.run.app' \
    "${service_name}" \
    "${project_number}" \
    "${fixed_region}"
}

require_private_workload_origin() {
  local project_number="$1"
  require_env MIM_STAGING_PRIVATE_WORKLOAD_RUN_APP_URL
  local expected_pattern="^https://mim-svc-[0-9a-f]{12}-${project_number}\\.${fixed_region}\\.run\\.app$"
  [[ "${MIM_STAGING_PRIVATE_WORKLOAD_RUN_APP_URL}" =~ ${expected_pattern} ]] || {
    printf 'MIM_STAGING_PRIVATE_WORKLOAD_RUN_APP_URL must be an exact reviewed MIM workload origin\n' >&2
    exit 1
  }
}

require_app_host_origin() {
  require_env MIM_STAGING_APP_HOST_URL
  [[ "${MIM_STAGING_APP_HOST_URL}" =~ ^https://[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.madup\\.app/?$ ]] || {
    printf 'MIM_STAGING_APP_HOST_URL must be an exact reviewed madup.app host\n' >&2
    exit 1
  }
}

require_private_file() {
  local path="$1"
  local label="$2"
  [[ -f "${path}" ]] || {
    printf '%s must be a regular file\n' "${label}" >&2
    exit 1
  }
  [[ ! -L "${path}" ]] || {
    printf '%s must not be a symlink\n' "${label}" >&2
    exit 1
  }
  [[ "$(stat_mode "${path}")" == "600" ]] || {
    printf '%s must use mode 0600\n' "${label}" >&2
    exit 1
  }
}

read_cookie_header() {
  local path="$1"
  local value
  value="$(<"${path}")"
  value="${value//$'\r'/}"
  value="${value//$'\n'/}"
  [[ -n "${value}" ]] || {
    printf 'Cloudflare authorization cookie file must not be empty\n' >&2
    exit 1
  }
  if [[ "${value}" == CF_Authorization=* ]]; then
    printf 'Cookie: %s' "${value}"
    return
  fi
  printf 'Cookie: CF_Authorization=%s' "${value}"
}

read_bearer_header() {
  local path="$1"
  local value
  value="$(<"${path}")"
  value="${value//$'\r'/}"
  value="${value//$'\n'/}"
  [[ -n "${value}" ]] || {
    printf 'ID token file must not be empty\n' >&2
    exit 1
  }
  if [[ "${value}" == Bearer\ * ]]; then
    printf 'Authorization: %s' "${value}"
    return
  fi
  printf 'Authorization: Bearer %s' "${value}"
}

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/mim-smoke.XXXXXX")"
cleanup() {
  rm -rf -- "${tmp_dir}"
}
trap cleanup EXIT
chmod 0700 "${tmp_dir}"

request_headers="${tmp_dir}/request-headers.txt"
response_headers="${tmp_dir}/response-headers.txt"
response_body="${tmp_dir}/response-body.txt"
payload_file="${tmp_dir}/payload.json"
last_status=""
touch "${request_headers}" "${response_headers}" "${response_body}" "${payload_file}"
chmod 0600 "${request_headers}" "${response_headers}" "${response_body}" "${payload_file}"

run_request() {
  local method="$1"
  local url="$2"
  local expected_csv="$3"
  local body="${4:-}"
  local header_line="${5:-}"
  local status

  : > "${request_headers}"
  : > "${response_headers}"
  : > "${response_body}"
  printf 'Accept: application/json\n' > "${request_headers}"
  if [[ -n "${header_line}" ]]; then
    printf '%s\n' "${header_line}" >> "${request_headers}"
  fi

  if [[ -n "${body}" ]]; then
    printf '%s' "${body}" > "${payload_file}"
    printf 'Content-Type: application/json\n' >> "${request_headers}"
    status="$(
      curl -sS \
        -X "${method}" \
        -D "${response_headers}" \
        -o "${response_body}" \
        -w '%{http_code}' \
        -H @"${request_headers}" \
        --data @"${payload_file}" \
        -- "${url}"
    )"
  else
    status="$(
      curl -sS \
        -X "${method}" \
        -D "${response_headers}" \
        -o "${response_body}" \
        -w '%{http_code}' \
        -H @"${request_headers}" \
        -- "${url}"
    )"
  fi

  case ",${expected_csv}," in
    *,"${status}",*)
      last_status="${status}"
      return 0
      ;;
    *)
      printf 'Unexpected status for %s %s: %s\n' "${method}" "${url}" "${status}" >&2
      exit 1
      ;;
  esac
}

require_cloud_run_iam_denial() {
  grep -Eiq '^server:[[:space:]]*Google Frontend\r?$' "${response_headers}" || {
    printf 'private workload denial must come from the Google Frontend IAM boundary\n' >&2
    exit 1
  }
  case "${last_status}" in
    401)
      grep -Eiq '^www-authenticate:[[:space:]]*Bearer' "${response_headers}" || {
        printf 'Cloud Run 401 denial must include a Bearer challenge\n' >&2
        exit 1
      }
      ;;
    403)
      grep -Eiq \
        'Your client does not have permission|The request was not authenticated' \
        "${response_body}" || {
        printf 'Cloud Run 403 denial body did not match the IAM boundary\n' >&2
        exit 1
      }
      ;;
    *)
      printf 'private workload denial status was not an IAM denial\n' >&2
      exit 1
      ;;
  esac
}

run_read_only_smoke() {
  local project_number expected_control_origin
  project_number="$(reviewed_project_number)"
  expected_control_origin="$(
    deterministic_run_app_origin "mim-control-plane" "${project_number}"
  )"
  require_exact_env MIM_STAGING_BASE_URL "${exact_origin}"
  require_env MIM_STAGING_CF_AUTHORIZATION_FILE
  require_exact_env MIM_STAGING_CONTROL_PLANE_RUN_APP_URL "${expected_control_origin}"
  require_private_file "${MIM_STAGING_CF_AUTHORIZATION_FILE}" "Cloudflare authorization file"

  local cookie_header
  cookie_header="$(read_cookie_header "${MIM_STAGING_CF_AUTHORIZATION_FILE}")"

  run_request GET "${MIM_STAGING_BASE_URL}/healthz" "302,303,307,308,401,403"
  printf '[PASS] anonymous-healthz-denied\n'

  run_request GET "${MIM_STAGING_BASE_URL}/healthz" "200" "" "${cookie_header}"
  printf '[PASS] authenticated-healthz\n'

  run_request GET "${MIM_STAGING_BASE_URL}/readyz" "200" "" "${cookie_header}"
  printf '[PASS] authenticated-readyz\n'

  run_request \
    POST \
    "${MIM_STAGING_BASE_URL}/mcp" \
    "200" \
    '{"jsonrpc":"2.0","id":"init","method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"mim-staging-smoke","version":"1.0"}}}' \
    "${cookie_header}"
  printf '[PASS] authenticated-mcp-initialize\n'

  run_request \
    POST \
    "${MIM_STAGING_BASE_URL}/mcp" \
    "200" \
    '{"jsonrpc":"2.0","id":"tools","method":"tools/list","params":{}}' \
    "${cookie_header}"
  printf '[PASS] authenticated-mcp-tools-list\n'

  run_request GET "${MIM_STAGING_CONTROL_PLANE_RUN_APP_URL}/healthz" "401,403"
  printf '[PASS] direct-origin-without-worker-hmac-denied\n'

  run_request GET "${MIM_STAGING_CONTROL_PLANE_RUN_APP_URL}/readyz" "401,403"
  printf '[PASS] direct-origin-readyz-without-worker-hmac-denied\n'

  run_request \
    POST \
    "${MIM_STAGING_CONTROL_PLANE_RUN_APP_URL}/mcp" \
    "401,403" \
    '{"jsonrpc":"2.0","id":"tools","method":"tools/list","params":{}}'
  printf '[PASS] direct-origin-mcp-without-worker-hmac-denied\n'
}

run_public_app_smoke() {
  local project_number expected_app_gateway_origin
  project_number="$(reviewed_project_number)"
  expected_app_gateway_origin="$(
    deterministic_run_app_origin "mim-app-gateway" "${project_number}"
  )"
  require_exact_env MIM_STAGING_APP_GATEWAY_RUN_APP_URL "${expected_app_gateway_origin}"
  require_private_workload_origin "${project_number}"
  require_app_host_origin
  require_env MIM_STAGING_APP_OWNER_CF_AUTHORIZATION_FILE
  require_env MIM_STAGING_APP_ADMIN_CF_AUTHORIZATION_FILE
  require_env MIM_STAGING_APP_OTHER_CF_AUTHORIZATION_FILE
  require_private_file "${MIM_STAGING_APP_OWNER_CF_AUTHORIZATION_FILE}" "App owner authorization file"
  require_private_file "${MIM_STAGING_APP_ADMIN_CF_AUTHORIZATION_FILE}" "App admin authorization file"
  require_private_file "${MIM_STAGING_APP_OTHER_CF_AUTHORIZATION_FILE}" "App other-user authorization file"

  local owner_header admin_header other_header
  owner_header="$(read_cookie_header "${MIM_STAGING_APP_OWNER_CF_AUTHORIZATION_FILE}")"
  admin_header="$(read_cookie_header "${MIM_STAGING_APP_ADMIN_CF_AUTHORIZATION_FILE}")"
  other_header="$(read_cookie_header "${MIM_STAGING_APP_OTHER_CF_AUTHORIZATION_FILE}")"

  run_request GET "${MIM_STAGING_APP_GATEWAY_RUN_APP_URL}" "401,403"
  grep -Fqx 'Request denied.' "${response_body}" || {
    printf 'app gateway direct denial must return the exact application deny contract\n' >&2
    exit 1
  }
  printf '[PASS] app-gateway-direct-anonymous-denied\n'

  run_request GET "${MIM_STAGING_PRIVATE_WORKLOAD_RUN_APP_URL}" "401,403"
  require_cloud_run_iam_denial
  printf '[PASS] private-user-run-app-anonymous-denied\n'

  run_request GET "${MIM_STAGING_APP_HOST_URL}" "200" "" "${owner_header}"
  printf '[PASS] app-host-owner-allowed\n'

  run_request GET "${MIM_STAGING_APP_HOST_URL}" "200" "" "${admin_header}"
  printf '[PASS] app-host-admin-allowed\n'

  run_request GET "${MIM_STAGING_APP_HOST_URL}" "401,403" "" "${other_header}"
  printf '[PASS] app-host-other-denied\n'
}

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

case "${1}" in
  --read-only)
    run_read_only_smoke
    ;;
  --public-app)
    run_public_app_smoke
    ;;
  *)
    usage
    exit 1
    ;;
esac

printf '[PASS] staging smoke completed\n'
