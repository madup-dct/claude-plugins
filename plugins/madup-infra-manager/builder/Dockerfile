FROM docker.io/library/docker:29.6.2-cli@sha256:be132a9f282288de4afaf63379dff75711fda0147c6b72a9df44e51841402144

USER root

COPY --chmod=0555 entrypoint.sh /usr/local/bin/mim-builder
COPY --chmod=0555 render_context.sh /usr/local/libexec/mim-render-context

ENTRYPOINT ["/usr/local/bin/mim-builder"]
