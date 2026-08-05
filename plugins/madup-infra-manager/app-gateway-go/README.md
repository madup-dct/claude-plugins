# MIM App Gateway

Stdlib Go 1.24 reverse proxy for `*.madup.app` workloads behind the MIM Worker.

## Responsibilities

- Validate strict runtime configuration.
- Enforce a bounded whole-request read timeout for the public server before any body can be consumed indefinitely.
- Verify Worker proof v2 locally for `destination_class=app-gateway`.
- Verify the Cloudflare Access JWT against JWKS with stdlib RSA/JWT handling.
- Use dedicated short-timeout control clients for JWKS, metadata ID tokens, and `/v1/apps/authorize`, separate from the streaming proxy transport.
- Call the private `/v1/apps/authorize` endpoint with a metadata-server Google ID token.
- Validate that the returned upstream exactly matches the reviewed Cloud Run naming contract and the hashed workload id.
- Proxy HTTP streaming and WebSocket upgrades while stripping browser/edge credentials, rewriting unsafe cookies and redirects, and returning generic errors.

## Required environment

- `MIM_LISTEN_ADDR` optional, defaults to `:8080`
- `MIM_PUBLIC_SUFFIX`
- `MIM_PROJECT_ID`
- `MIM_PROJECT_NUMBER`
- `MIM_REGION`
- `MIM_CLOUDFLARE_ACCESS_ISSUER`
- `MIM_CLOUDFLARE_ACCESS_AUDIENCE`
- `MIM_APP_GATEWAY_SERVICE_ACCOUNT_EMAIL`
- `MIM_APP_GATEWAY_ORIGIN`
- `MIM_APP_AUTHORIZATION_URL`
- `MIM_APP_AUTHORIZATION_AUDIENCE`
- `MIM_APP_PROOF_CURRENT_KEY_ID`
- `MIM_APP_PROOF_CURRENT_SECRET`
- `MIM_APP_PROOF_PREVIOUS_KEY_ID` optional only when paired with `MIM_APP_PROOF_PREVIOUS_SECRET`
- `MIM_APP_PROOF_PREVIOUS_SECRET` optional only when paired with `MIM_APP_PROOF_PREVIOUS_KEY_ID`

## Verification

```bash
go test ./...
go test -race ./...
go vet ./...
CGO_ENABLED=0 go build ./cmd/mim-app-gateway
docker build -t mim-app-gateway:local .
```
