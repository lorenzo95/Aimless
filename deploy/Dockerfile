FROM golang:1.27-alpine AS build
WORKDIR /src
COPY daemon/go.mod daemon/go.sum ./
RUN go mod download
COPY daemon/*.go ./
RUN go build -trimpath -ldflags "-s -w" -o /aimlessd .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates && adduser -D -u 1000 aimless
COPY --from=build /aimlessd /usr/local/bin/aimlessd
USER aimless
WORKDIR /data
VOLUME /data
ENTRYPOINT ["/usr/local/bin/aimlessd", "-datadir", "/data"]
