#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# shellcheck source=config_lib.sh
. "$SCRIPT_DIR/config_lib.sh"

CONFIG_FILE="${MIM_CONFIG_FILE:-$(mim_default_config_file "$SCRIPT_DIR")}"

mim_require_no_args "$@"
mim_load_config "$CONFIG_FILE"
mim_print_redacted_config_summary
printf 'Configuration review passed.\n'
