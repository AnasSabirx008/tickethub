# syntax=docker/dockerfile:1
# Runs as non-root user nginx (uid 101) and listens on 8080
FROM nginxinc/nginx-unprivileged:1.27-alpine

# Default upstream; override at runtime with -e GATEWAY_URL=...
ENV GATEWAY_URL=http://gateway:8000

# Templates in /etc/nginx/templates/ are rendered with envsubst at startup
# into /etc/nginx/conf.d/ (only existing env vars are replaced, so $host etc. stay intact)
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY public/ /usr/share/nginx/html/

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
