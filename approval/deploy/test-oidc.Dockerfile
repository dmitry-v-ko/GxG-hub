# Test OIDC issuer for the compose `test` profile only (never production). AC-M9-12 stand.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN --mount=type=secret,id=ca-bundle,required=false \
    if [ -f /run/secrets/ca-bundle ]; then export PIP_CERT=/run/secrets/ca-bundle; fi \
    && pip install --no-cache-dir oidc-provider-mock==0.4.8
USER nobody
EXPOSE 9400
CMD ["oidc-provider-mock", "--host", "0.0.0.0", "--port", "9400"]
