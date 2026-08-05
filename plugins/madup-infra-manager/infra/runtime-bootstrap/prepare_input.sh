#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TEMPLATE_FILE="$SCRIPT_DIR/bootstrap-input.template.json"

usage() {
  printf 'Usage: %s --output <private-json-file>\n' "$0" >&2
  exit 1
}

OUTPUT_FILE=
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --output)
      [[ "$#" -ge 2 ]] || {
        printf 'ERROR: Missing value for --output\n' >&2
        exit 1
      }
      OUTPUT_FILE=$2
      shift 2
      ;;
    --*)
      printf 'ERROR: Unknown argument: %s\n' "$1" >&2
      exit 1
      ;;
    *)
      printf 'ERROR: Positional arguments are not supported\n' >&2
      exit 1
      ;;
  esac
done

[[ -n "$OUTPUT_FILE" ]] || usage
[[ -f "$TEMPLATE_FILE" ]] || {
  printf 'ERROR: Bootstrap template is missing\n' >&2
  exit 1
}
[[ ! -e "$OUTPUT_FILE" ]] || {
  printf 'ERROR: Refusing to overwrite existing file\n' >&2
  exit 1
}

install -m 600 "$TEMPLATE_FILE" "$OUTPUT_FILE"
printf 'Wrote bootstrap template to %s\n' "$OUTPUT_FILE"
