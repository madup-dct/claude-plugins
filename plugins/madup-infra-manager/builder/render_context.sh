#!/bin/sh
set -eu
export LC_ALL=C

deny() {
    printf '%s\n' 'MIM trusted builder denied the source.' >&2
    exit 2
}

[ "$#" -eq 5 ] || deny
kind=$1
runtime=$2
entrypoint=$3
source_root=$4
work_root=$5

[ -d "$source_root" ] || deny
mkdir -p "$work_root/context"

if find "$source_root" \( -type l -o -type b -o -type c -o -type p -o -type s \) \
    | grep -q .; then
    deny
fi

archive="$work_root/context.tar"
tar -C "$source_root" -cf "$archive" \
    --exclude='./.git' \
    --exclude='./.github' \
    --exclude='./infra' \
    --exclude='./.terraform' \
    --exclude='./node_modules' \
    --exclude='./.next' \
    --exclude='./__pycache__' \
    --exclude='./.env' \
    --exclude='*.env' \
    --exclude='./.env.*' \
    --exclude='*.env.*' \
    --exclude='./.envrc' \
    --exclude='*.envrc' \
    --exclude='./prod.env' \
    --exclude='./.dockerignore' \
    --exclude='./.npmrc' \
    --exclude='./.pypirc' \
    --exclude='./.netrc' \
    --exclude='./Dockerfile' \
    --exclude='./Dockerfile.*' \
    --exclude='./docker-compose*' \
    --exclude='./cloudbuild*.yaml' \
    --exclude='./cloudbuild*.yml' \
    --exclude='./cloudbuild*.json' \
    --exclude='*.tf' \
    --exclude='*.tfvars' \
    --exclude='*.tf.json' \
    --exclude='*.tfvars.json' \
    --exclude='*.pem' \
    --exclude='*.key' \
    --exclude='*.crt' \
    --exclude='*.cer' \
    --exclude='*.cert' \
    --exclude='*.jwk' \
    --exclude='*.jks' \
    --exclude='*.keystore' \
    --exclude='*.p12' \
    --exclude='*.pkcs12' \
    --exclude='*.pfx' \
    --exclude='*.p8' \
    --exclude='./id_rsa' \
    --exclude='./id_dsa' \
    --exclude='./id_ecdsa' \
    --exclude='./id_ed25519' \
    --exclude='*.credentials.json' \
    --exclude='*credentials*.json' \
    --exclude='*creds*.json' \
    --exclude='*credential*.json' \
    --exclude='*service-account*.json' \
    --exclude='*private-key*.json' \
    .
tar -C "$work_root/context" -xf "$archive"
rm -f -- "$archive"

file_count=$(find "$work_root/context" -type f | wc -l | tr -d ' ')
[ "$file_count" -le 128 ] || deny
size_kib=$(du -sk "$work_root/context" | awk '{print $1}')
[ "$size_kib" -le 2048 ] || deny

if find "$work_root/context" -type f \( \
    -name '.env' -o -name '*.env' -o -name '.env.*' -o -name '*.env.*' -o -name '.npmrc' -o \
    -name '.envrc' -o -name '*.envrc' -o -name 'prod.env' -o \
    -name '.pypirc' -o -name '.netrc' -o -name 'Dockerfile' -o \
    -name 'Dockerfile.*' -o -name 'docker-compose*' -o \
    -name 'cloudbuild*.yaml' -o -name 'cloudbuild*.yml' -o -name 'cloudbuild*.json' -o \
    -name '*.tf' -o -name '*.tfvars' -o -name '*.tf.json' -o -name '*.tfvars.json' -o \
    -name '*.pem' -o -name '*.key' -o -name '*.crt' -o -name '*.cer' -o -name '*.cert' -o \
    -name '*.jwk' -o -name '*.jks' -o -name '*.keystore' -o \
    -name '*.p12' -o -name '*.pkcs12' -o -name '*.pfx' -o -name '*.p8' -o \
    -name 'id_rsa' -o -name 'id_dsa' -o -name 'id_ecdsa' -o -name 'id_ed25519' -o \
    -name '*.credentials.json' -o -name '*credentials*.json' -o \
    -name '*creds*.json' -o -name '*credential*.json' -o -name '*service-account*.json' -o \
    -name '*private-key*.json' \
\) | grep -q .; then
    deny
fi

python_base='docker.io/library/python:3.13.7-slim-bookworm@sha256:781449467ffb6f04218f09b1ecdcdc7d22b289ee5da9ec498b024e24ad7a6db7'
node_base='docker.io/library/node:20.19.4-bookworm-slim@sha256:a25e59a5562406b0a4f34ce94ccad6c3902dcf3269b40e1fe12d881090c6f9be'

case "$kind:$runtime:$entrypoint" in
    streamlit:python3.13:app.py)
        [ -f "$work_root/context/app.py" ] || deny
        [ -f "$work_root/context/requirements.txt" ] || deny
        cat >"$work_root/Dockerfile" <<EOF
FROM $python_base AS build
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check --prefix=/install -r requirements.txt
COPY . .

FROM $python_base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 app && useradd --no-create-home --uid 10001 --gid 10001 --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /install /usr/local
COPY --from=build --chown=10001:10001 /app /app
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8080", "--server.headless=true"]
EOF
        ;;
    nextjs:node20:app/page.tsx)
        [ -f "$work_root/context/app/page.tsx" ] || deny
        [ -f "$work_root/context/package.json" ] || deny
        [ -f "$work_root/context/package-lock.json" ] || deny
        cat >"$work_root/Dockerfile" <<EOF
FROM $node_base AS build
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY . .
RUN ./node_modules/.bin/next build

FROM $node_base
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN groupadd --gid 10001 app && useradd --no-create-home --uid 10001 --gid 10001 --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build --chown=10001:10001 /app /app
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["./node_modules/.bin/next", "start", "--hostname", "0.0.0.0", "--port", "8080"]
EOF
        ;;
    scheduled_script:python3.13:*.py)
        case "$entrypoint" in */*|.*|-*|*[!A-Za-z0-9_.-]*) deny ;; esac
        [ -f "$work_root/context/$entrypoint" ] || deny
        [ -f "$work_root/context/mim.yaml" ] || deny
        cat >"$work_root/Dockerfile" <<EOF
FROM $python_base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 app && useradd --no-create-home --uid 10001 --gid 10001 --shell /usr/sbin/nologin app
WORKDIR /app
COPY --chown=10001:10001 . /app
USER 10001:10001
ENTRYPOINT ["python", "$entrypoint"]
EOF
        ;;
    *) deny ;;
esac

chmod 0444 "$work_root/Dockerfile"
