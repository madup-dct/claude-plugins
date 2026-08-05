#!/bin/sh
set -eu
export LC_ALL=C

deny() {
    printf '%s\n' 'MIM trusted builder denied the request.' >&2
    exit 2
}

readonly allowed_region='asia-northeast3'
readonly allowed_project_id='mim-prod-123456'
readonly allowed_repository='mim'
readonly allowed_image='workloads'

kind=''
runtime=''
entrypoint=''
source_sha=''
destination=''

while [ "$#" -gt 0 ]; do
    [ "$#" -ge 2 ] || deny
    case "$1" in
        --kind) kind=$2 ;;
        --runtime) runtime=$2 ;;
        --entrypoint) entrypoint=$2 ;;
        --source-sha) source_sha=$2 ;;
        --destination) destination=$2 ;;
        *) deny ;;
    esac
    shift 2
done

case "$source_sha" in
    ''|*[!0-9a-f]*) deny ;;
esac
[ "${#source_sha}" -eq 40 ] || deny
[ "$source_sha" != '0000000000000000000000000000000000000000' ] || deny
source_prefix=$(printf '%.12s' "$source_sha")

destination_prefix="$allowed_region-docker.pkg.dev/$allowed_project_id/$allowed_repository/$allowed_image:"
destination_tag=${destination#"$destination_prefix"}
 [ "$destination_tag" != "$destination" ] || deny
printf '%s' "$destination_tag" \
    | grep -Eq '^w-[0-9a-f]{12}-sha-[0-9a-f]{12}-op-[0-9a-f]{12}$' || deny
tag_source=$(printf '%s' "$destination_tag" \
    | sed -n 's/^w-[0-9a-f]\{12\}-sha-\([0-9a-f]\{12\}\)-op-[0-9a-f]\{12\}$/\1/p')
[ -n "$tag_source" ] || deny
[ "$tag_source" = "$source_prefix" ] || deny

case "$kind:$runtime:$entrypoint" in
    streamlit:python3.13:app.py) ;;
    nextjs:node20:app/page.tsx) ;;
    scheduled_script:python3.13:*.py)
        case "$entrypoint" in */*|.*|-*|*[!A-Za-z0-9_.-]*) deny ;; esac
        ;;
    *) deny ;;
esac

work_root=$(mktemp -d /tmp/mim-builder.XXXXXX)
cleanup() {
    rm -rf -- "$work_root"
}
trap cleanup EXIT HUP INT TERM

entrypoint_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
render_context_dir=$(CDPATH= cd -- "$entrypoint_dir/../libexec" && pwd)
render_context_bin="$render_context_dir/mim-render-context"
[ -x "$render_context_bin" ] || deny
command -v docker >/dev/null 2>&1 || deny

"$render_context_bin" \
    "$kind" "$runtime" "$entrypoint" /workspace "$work_root"

docker build \
    --file "$work_root/Dockerfile" \
    --tag "$destination" \
    --pull \
    "$work_root/context"
docker push "$destination"
