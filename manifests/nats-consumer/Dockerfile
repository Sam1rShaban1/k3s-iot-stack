FROM --platform=linux/arm64 golang:1.24-alpine AS builder
WORKDIR /app
RUN GOTOOLCHAIN=auto go version
COPY go.mod go.sum ./
COPY consumer.go ./
RUN GOTOOLCHAIN=auto CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -ldflags="-s -w" -o consumer .

FROM scratch
COPY --from=builder /app/consumer /consumer
ENTRYPOINT ["/consumer"]