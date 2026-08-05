#!/bin/sh
set -eu
export LC_ALL=C

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
tmp=$(mktemp -d /tmp/mim-builder-test.XXXXXX)
cleanup() {
    rm -rf -- "$tmp"
}
trap cleanup EXIT HUP INT TERM

grep -F 'USER root' "$here/Dockerfile" >/dev/null
if grep -F 'USER 10001:10001' "$here/Dockerfile" >/dev/null; then
    exit 1
fi

mkdir -p "$tmp/source/app" "$tmp/source/.github/workflows" "$tmp/source/infra"
printf '%s\n' 'import streamlit' >"$tmp/source/app.py"
printf '%s\n' 'streamlit==1.40.0' >"$tmp/source/requirements.txt"
printf '%s\n' 'FROM untrusted' >"$tmp/source/Dockerfile"
printf '%s\n' 'steps: []' >"$tmp/source/cloudbuild.yaml"
printf '%s\n' '{"steps":[]}' >"$tmp/source/cloudbuild.json"
printf '%s\n' 'terraform {}' >"$tmp/source/infra/main.tf"
printf '%s\n' 'secret=value' >"$tmp/source/.env"
printf '%s\n' 'secret=value' >"$tmp/source/prod.env"
printf '%s\n' 'secret=value' >"$tmp/source/staging.env"
printf '%s\n' 'layout = "shell"' >"$tmp/source/.envrc"
printf '%s\n' 'terraform {}' >"$tmp/source/policy.tf.json"
printf '%s\n' 'vars' >"$tmp/source/terraform.tfvars.json"
printf '%s%s\n' '-----BEGIN PRIVATE ' 'KEY-----' >"$tmp/source/id_rsa"
printf '%s\n' '{"type":"service_account"}' >"$tmp/source/service-account.credentials.json"
printf '%s\n' '-----BEGIN CERTIFICATE-----' >"$tmp/source/server.crt"
mkdir -p "$tmp/source/nested"
printf '%s\n' 'secret=value' >"$tmp/source/nested/release.env"
printf '%s\n' '*' >"$tmp/source/.dockerignore"

"$here/render_context.sh" \
    streamlit python3.13 app.py "$tmp/source" "$tmp/rendered"

test -f "$tmp/rendered/Dockerfile"
test -f "$tmp/rendered/context/app.py"
test ! -e "$tmp/rendered/context/Dockerfile"
test ! -e "$tmp/rendered/context/cloudbuild.yaml"
test ! -e "$tmp/rendered/context/cloudbuild.json"
test ! -e "$tmp/rendered/context/infra/main.tf"
test ! -e "$tmp/rendered/context/.env"
test ! -e "$tmp/rendered/context/prod.env"
test ! -e "$tmp/rendered/context/staging.env"
test ! -e "$tmp/rendered/context/.envrc"
test ! -e "$tmp/rendered/context/nested/release.env"
test ! -e "$tmp/rendered/context/policy.tf.json"
test ! -e "$tmp/rendered/context/terraform.tfvars.json"
test ! -e "$tmp/rendered/context/id_rsa"
test ! -e "$tmp/rendered/context/service-account.credentials.json"
test ! -e "$tmp/rendered/context/server.crt"
test ! -e "$tmp/rendered/context/.dockerignore"
grep -F 'USER 10001:10001' "$tmp/rendered/Dockerfile" >/dev/null
grep -F '@sha256:781449467ffb6f04218f09b1ecdcdc7d22b289ee5da9ec498b024e24ad7a6db7' "$tmp/rendered/Dockerfile" >/dev/null
if grep -E '(^|[[:space:]])(ARG|ENV)[[:space:]].*(TOKEN|SECRET|KEY|PASSWORD)' "$tmp/rendered/Dockerfile" >/dev/null; then
    exit 1
fi

mkdir -p "$tmp/next/app"
printf '%s\n' 'export default function Page() { return null }' >"$tmp/next/app/page.tsx"
printf '%s\n' '{"dependencies":{"next":"20.0.0"}}' >"$tmp/next/package.json"
printf '%s\n' '{"lockfileVersion":3}' >"$tmp/next/package-lock.json"
"$here/render_context.sh" \
    nextjs node20 app/page.tsx "$tmp/next" "$tmp/next-rendered"
grep -F 'npm ci --ignore-scripts --no-audit --no-fund' "$tmp/next-rendered/Dockerfile" >/dev/null
grep -F '@sha256:a25e59a5562406b0a4f34ce94ccad6c3902dcf3269b40e1fe12d881090c6f9be' "$tmp/next-rendered/Dockerfile" >/dev/null
grep -F 'USER 10001:10001' "$tmp/next-rendered/Dockerfile" >/dev/null

mkdir -p "$tmp/job"
printf '%s\n' 'print("ok")' >"$tmp/job/main.py"
printf '%s\n' 'kind: scheduled_script' 'entrypoint: main.py' 'schedule: hourly' >"$tmp/job/mim.yaml"
"$here/render_context.sh" \
    scheduled_script python3.13 main.py "$tmp/job" "$tmp/job-rendered"
grep -F 'ENTRYPOINT ["python", "main.py"]' "$tmp/job-rendered/Dockerfile" >/dev/null
grep -F 'USER 10001:10001' "$tmp/job-rendered/Dockerfile" >/dev/null

if "$here/render_context.sh" \
    scheduled_script python3.13 '../escape.py' "$tmp/job" "$tmp/bad" 2>/dev/null; then
    exit 1
fi

mkdir -p "$tmp/link-source"
printf '%s\n' 'print("ok")' >"$tmp/link-source/app.py"
printf '%s\n' 'streamlit==1.40.0' >"$tmp/link-source/requirements.txt"
ln -s app.py "$tmp/link-source/app-link.py"
if "$here/render_context.sh" \
    streamlit python3.13 app.py "$tmp/link-source" "$tmp/link-rendered" 2>/dev/null; then
    exit 1
fi

mkdir -p "$tmp/fifo-source"
printf '%s\n' 'print("ok")' >"$tmp/fifo-source/app.py"
printf '%s\n' 'streamlit==1.40.0' >"$tmp/fifo-source/requirements.txt"
mkfifo "$tmp/fifo-source/runtime.pipe"
if "$here/render_context.sh" \
    streamlit python3.13 app.py "$tmp/fifo-source" "$tmp/fifo-rendered" 2>/dev/null; then
    exit 1
fi

mkdir -p "$tmp/many-files"
printf '%s\n' 'print("ok")' >"$tmp/many-files/app.py"
printf '%s\n' 'streamlit==1.40.0' >"$tmp/many-files/requirements.txt"
i=0
while [ "$i" -lt 127 ]; do
    printf '%s\n' "$i" >"$tmp/many-files/file-$i.txt"
    i=$((i + 1))
done
if "$here/render_context.sh" \
    streamlit python3.13 app.py "$tmp/many-files" "$tmp/many-files-rendered" 2>/dev/null; then
    exit 1
fi

mkdir -p "$tmp/large-source"
printf '%s\n' 'print("ok")' >"$tmp/large-source/app.py"
printf '%s\n' 'streamlit==1.40.0' >"$tmp/large-source/requirements.txt"
dd if=/dev/zero of="$tmp/large-source/payload.bin" bs=1024 count=2050 >/dev/null 2>&1
if "$here/render_context.sh" \
    streamlit python3.13 app.py "$tmp/large-source" "$tmp/large-rendered" 2>/dev/null; then
    exit 1
fi

mkdir -p "$tmp/mocks/bin" "$tmp/mocks/libexec" "$tmp/workspace"
mkdir -p "$tmp/runtime/bin" "$tmp/runtime/libexec"
cat >"$tmp/runtime/libexec/mim-render-context" <<'EOF'
#!/bin/sh
set -eu
mkdir -p "$5/context"
printf '%s\n' 'FROM scratch' >"$5/Dockerfile"
EOF
chmod 0555 "$tmp/runtime/libexec/mim-render-context"
cp "$here/entrypoint.sh" "$tmp/runtime/bin/mim-builder"
chmod 0555 "$tmp/runtime/bin/mim-builder"
cat >"$tmp/runtime/bin/docker" <<'EOF'
#!/bin/sh
set -eu
printf '%s\n' "$*" >>"$MIM_DOCKER_LOG"
EOF
chmod 0555 "$tmp/runtime/bin/docker"

source_sha='1234567890abcdef1234567890abcdef12345678'
source_prefix='1234567890ab'
destination_tag="w-aaaaaaaaaaaa-sha-$source_prefix-op-bbbbbbbbbbbb"
destination="asia-northeast3-docker.pkg.dev/mim-prod-123456/mim/workloads:$destination_tag"
docker_log="$tmp/docker.log"
PATH="$tmp/runtime/bin:$PATH" \
MIM_DOCKER_LOG="$docker_log" \
"$tmp/runtime/bin/mim-builder" \
    --kind streamlit \
    --runtime python3.13 \
    --entrypoint app.py \
    --source-sha "$source_sha" \
    --destination "$destination"
grep -F "build --file " "$docker_log" >/dev/null
grep -F -- "--tag $destination --pull" "$docker_log" >/dev/null
grep -F "push $destination" "$docker_log" >/dev/null

if PATH="$tmp/runtime/bin:$PATH" \
    MIM_DOCKER_LOG="$docker_log.bad" \
    "$tmp/runtime/bin/mim-builder" \
        --kind streamlit \
        --runtime python3.13 \
        --entrypoint app.py \
        --source-sha "$source_sha" \
        --destination "asia-northeast3-docker.pkg.dev/mim-prod-123456/mim/workloads:w-aaaaaaaaaaaa-sha-deadbeefcafe-op-bbbbbbbbbbbb" \
        2>/dev/null; then
    exit 1
fi

if PATH="$tmp/runtime/bin:$PATH" \
    MIM_DOCKER_LOG="$docker_log.project" \
    "$tmp/runtime/bin/mim-builder" \
        --kind streamlit \
        --runtime python3.13 \
        --entrypoint app.py \
        --source-sha "$source_sha" \
        --destination "asia-northeast3-docker.pkg.dev/other-project/mim/workloads:$destination_tag" \
        2>/dev/null; then
    exit 1
fi

printf '%s\n' 'builder contract tests passed'
