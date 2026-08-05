# MIM trusted workload builder

This image is the only workload build step accepted by the MIM control plane.
It renders platform-owned, digest-pinned runtime Dockerfiles for Streamlit,
Next.js, and the approved hourly Python job. Repository Dockerfiles,
Cloud Build files, Terraform, local environment files, private keys, links,
and device files never enter the build context.

The Cloud Build adapter supplies only the reviewed kind, runtime, entrypoint,
immutable source SHA, and exact
`asia-northeast3-docker.pkg.dev/mim-prod-123456/mim/workloads:<tag>`
Artifact Registry destination. The tag must embed the same 12-hex source SHA
prefix that the control plane admitted. The builder accepts no secret argument
or secret environment binding. The trusted builder step itself stays `root`
because Cloud Build's Docker builder contract needs daemon access, while every
rendered workload runtime image stays on uid/gid `10001`. The entrypoint
resolves `../libexec/mim-render-context` relative to its installed binary path;
there is no production environment override for arbitrary helper executables.

Run the local contract checks with:

```sh
sh test_builder.sh
docker build --platform=linux/amd64 -t mim-builder:test .
```

`test_builder.sh` copies the entrypoint into a temporary `bin/../libexec`
layout with mocked `docker` and render-context binaries, so it proves argument
binding without requiring a local Docker daemon or network access.
