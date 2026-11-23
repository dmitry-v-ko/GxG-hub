# Railway image of ContextForge: the pinned upstream image (SEC-22, same digest as compose.yaml) plus
#   - the gxg_identity plugin (ADR-0001 variant C);
#   - entrypoint.sh (variables to files) and tailscale (userspace networking, `tailscale serve`; ADR-0003).
# Build context: the repository root. CF_START is the upstream start command; it is not recorded in the repository, read
# it once with `docker image inspect <image> --format '{{json .Config.Entrypoint}} {{json .Config.Cmd}}'` and set the build
# variable CF_START in Railway (services.yaml: build_args). The build fails without it.
ARG TAILSCALE_IMAGE=tailscale/tailscale:v1.84.2
FROM ${TAILSCALE_IMAGE} AS tailscale

FROM ghcr.io/ibm/mcp-context-forge:v1.1.1@sha256:16c7a8985c1a3528ba49fcab32951e06c9a41a171fc5c41d24b085283c4556a5
ARG CF_START
RUN test -n "$CF_START" || { echo "set the build variable CF_START (see the header of this file)" >&2; exit 1; }
ENV CF_START=$CF_START
COPY gateway/contextforge/gxg_identity /app/plugins/gxg_identity
COPY deploy/railway/entrypoint.sh /opt/gxg/entrypoint.sh
COPY --from=tailscale /usr/local/bin/tailscaled /usr/local/bin/tailscale /usr/local/bin/
ENTRYPOINT ["/opt/gxg/entrypoint.sh"]
CMD ["sh", "-c", "exec $CF_START"]
