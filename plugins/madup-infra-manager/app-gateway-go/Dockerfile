FROM docker.io/library/golang:1.26.5-alpine@sha256:0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2 AS build

WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY gateway ./gateway

RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags='-s -w' -o /out/mim-app-gateway ./cmd/mim-app-gateway

FROM gcr.io/distroless/static-debian12:nonroot@sha256:f5b485ea962d9bd1186b2f6b3a061191539b905b82ec395de78cbfae51f20e35

ENV PORT=8080
EXPOSE 8080

COPY --from=build /out/mim-app-gateway /mim-app-gateway

USER nonroot:nonroot
ENTRYPOINT ["/mim-app-gateway"]
