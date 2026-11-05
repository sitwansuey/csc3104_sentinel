ARG RUNTIME_IMAGE
FROM golang:1.27.3-alpine@sha256:b8c9b82affbbb82a020bbcf998dad2ba9516944ac7e883524642a481a9505e9c AS build
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
COPY shipping/ ./
# Exact dependency changes, preserving upstream application behavior.
RUN go get golang.org/x/net@v0.60.0 && go mod tidy \
    && go test ./... \
    && go build -trimpath -ldflags="-s -w" -o /out/shippingservice .

FROM ${RUNTIME_IMAGE}
ARG UPSTREAM_COMMIT
LABEL org.opencontainers.image.title="Sentinel reviewed Boutique shipping candidate" \
      org.opencontainers.image.version="0.10.7-security.1" \
      org.opencontainers.image.revision=${UPSTREAM_COMMIT}
COPY --from=build /out/shippingservice /src/shippingservice
COPY LICENSE /usr/share/licenses/boutique-LICENSE
USER 1000:1000
ENV PORT=50051 GOTRACEBACK=single
ENTRYPOINT ["/src/shippingservice"]
