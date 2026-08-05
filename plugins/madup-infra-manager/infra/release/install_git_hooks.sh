#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(git -C "${script_dir}" rev-parse --show-toplevel 2>/dev/null)" || {
  printf 'install_git_hooks.sh must run inside the repository\n' >&2
  exit 1
}

hook_rel=".githooks/pre-push"
hook_path="${repo_root}/${hook_rel}"
verify_path="${repo_root}/plugins/madup-infra-manager/infra/release/verify.sh"
scanner_path="${repo_root}/plugins/madup-infra-manager/infra/release/public_release_guard.py"
self_path="${repo_root}/plugins/madup-infra-manager/infra/release/install_git_hooks.sh"

require_regular_file() {
  local path="${1}"
  local label="${2}"
  if [[ -L "${path}" ]]; then
    printf '%s must not be a symlink: %s\n' "${label}" "${path}" >&2
    exit 1
  fi
  if [[ ! -f "${path}" ]]; then
    printf '%s is missing: %s\n' "${label}" "${path}" >&2
    exit 1
  fi
}

require_regular_readable_file() {
  local path="${1}"
  local label="${2}"
  require_regular_file "${path}" "${label}"
  if [[ ! -r "${path}" ]]; then
    printf '%s is unreadable: %s\n' "${label}" "${path}" >&2
    exit 1
  fi
}

require_regular_file "${hook_path}" "tracked pre-push hook"
require_regular_file "${verify_path}" "release verifier"
require_regular_file "${self_path}" "hook installer"
require_regular_readable_file "${scanner_path}" "public release scanner"

chmod 0755 "${hook_path}" "${verify_path}" "${self_path}"

for executable_path in "${hook_path}" "${verify_path}" "${self_path}"; do
  if [[ ! -x "${executable_path}" ]]; then
    printf 'required executable bit is missing: %s\n' "${executable_path}" >&2
    exit 1
  fi
done

git -C "${repo_root}" config --local core.hooksPath ".githooks"
configured_hook_path="$(git -C "${repo_root}" config --get core.hooksPath 2>/dev/null || true)"
if [[ "${configured_hook_path}" != ".githooks" ]]; then
  printf 'core.hooksPath must be exactly .githooks\n' >&2
  exit 1
fi

if [[ ! -x "${hook_path}" ]]; then
  printf 'installed pre-push hook is not executable\n' >&2
  exit 1
fi
