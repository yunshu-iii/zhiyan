FROM golang:1.24.8-bookworm AS builder
ENV GOMAXPROCS=2 GOMEMLIMIT=1200MiB CGO_ENABLED=0 GOTOOLCHAIN=local
WORKDIR /src
COPY minio-source.tar.gz /tmp/minio-source.tar.gz
RUN tar -xzf /tmp/minio-source.tar.gz --strip-components=1 -C /src
RUN go build -p 2 -trimpath -ldflags="-s -w" -o /out/minio .

FROM alpine:3.22.2
COPY --from=builder /out/minio /usr/local/bin/minio
COPY --from=builder /src/LICENSE /usr/share/licenses/minio/LICENSE
ENV MINIO_UPDATE=off
EXPOSE 9000 9001
CMD ["minio", "server", "/minio_data", "--console-address", ":9001"]
