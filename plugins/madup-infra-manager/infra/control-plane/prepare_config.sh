#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
EXAMPLE_FILE="$SCRIPT_DIR/config.example.env"

# shellcheck source=config_lib.sh
. "$SCRIPT_DIR/config_lib.sh"

usage() {
  printf 'Usage: %s --output <exact-file>\n' "$0" >&2
  exit 1
}

OUTPUT_FILE=
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --output)
      [[ "$#" -ge 2 ]] || mim_fail "Missing value for --output"
      OUTPUT_FILE=$2
      shift 2
      ;;
    --*)
      mim_fail "Unknown argument: $1"
      ;;
    *)
      mim_fail "Positional arguments are not supported"
      ;;
  esac
done

[[ -n "$OUTPUT_FILE" ]] || usage
[[ -f "$EXAMPLE_FILE" ]] || mim_fail "Config example is missing"
[[ ! -e "$OUTPUT_FILE" ]] || mim_fail "Refusing to overwrite existing file"

install -m 600 "$EXAMPLE_FILE" "$OUTPUT_FILE"
printf 'Wrote config template to %s\n' "$OUTPUT_FILE"
