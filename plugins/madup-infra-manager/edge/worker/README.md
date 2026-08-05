# Madup Infra Manager Edge Worker

Fail-closed Cloudflare Worker for `mim.madup.app` and reviewed `*.madup.app` application hosts.

## Purpose

- Depend on Cloudflare Access Managed OAuth for MCP authentication and preserve any downstream `401` + `WWW-Authenticate` discovery responses.
- Route only the exact control host `mim.madup.app` to the control-plane origin and only one-label, non-reserved `*.madup.app` hosts to the app-gateway origin.
- Require a single non-empty `Cf-Access-Jwt-Assertion` for every app request and every control request except the exact GitHub webhook bypass.
- Forward only explicit control-plane method/path pairs while allowing reviewed app methods across wildcard hosts.
- Strip caller-supplied `Authorization`, `Proxy-Authorization`, `X-MIM-*`, `Forwarded`, `X-Forwarded-*`, and extra `Cf-Access-*` headers before minting fresh origin-proof headers.
- Sign the v2 proof `destination class + method + signed public host + canonical path + raw-body SHA-256 + timestamp + request ID + key ID` with per-destination HMAC secrets that stay in Worker secret bindings.
- Reject control-plane and GitHub-bound request bodies larger than 16 KiB before origin forwarding, while allowing app-gateway request bodies up to exactly 1 MiB and denying 1 MiB plus one byte before origin fetch.
- Preserve streamed request/response bodies and WebSocket upgrade headers while proxying.
- Preserve host-local app cookies while stripping Cloudflare Access and MIM credential cookies, and rewrite origin `Set-Cookie` domains to host-only cookies.
- Keep the GitHub webhook bypass isolated to the exact `POST /v1/webhooks/github` route.

## Configuration

Plain variables in [wrangler.jsonc](./wrangler.jsonc):

- `MIM_CONTROL_PUBLIC_HOSTNAME`
- `MIM_APP_HOST_SUFFIX`
- `MIM_PROJECT_NUMBER` (the reviewed 12-digit target project number)
- `MIM_CONTROL_ORIGIN`
- `MIM_APP_GATEWAY_ORIGIN`
- `MIM_CONTROL_ALLOWED_ROUTES`
- `MIM_CONTROL_ORIGIN_HMAC_KEY_ID`
- `MIM_APP_GATEWAY_ORIGIN_HMAC_KEY_ID`

Secret binding, not checked into Git:

- `MIM_CONTROL_ORIGIN_HMAC_SECRET`
- `MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET`

The Worker derives both permitted Cloud Run origins from the reviewed project
number, fixed Seoul region, and fixed `mim-control-plane` / `mim-app-gateway`
service identities. The two origin variables must equal those derived URLs
exactly; alternate projects, services, ports, credentials, paths, queries, and
fragments fail closed before any origin fetch.

The tracked Wrangler values are deliberately invalid placeholders. The reviewed
edge plan/apply flow must inject the exact discovered project number and the two
derived origins; deploying the tracked defaults stays fail closed.

## Local verification

```bash
npm test
npx wrangler deploy --dry-run
```

This directory does not claim a live deployment. Operator-only Cloudflare account, route, secret, and origin values stay outside tracked files.
