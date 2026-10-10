FROM node:22-alpine AS build
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
COPY frontend/ .
ARG BASE_PATH=/unifi
ENV BASE_PATH=${BASE_PATH}
RUN npm run build

FROM nginx:1.29-alpine
ARG VERSION=1.0.0
ARG REVISION=local
LABEL org.opencontainers.image.title="Unifi-NOC Web" \
	org.opencontainers.image.description="Noncommercial UniFi NOC dashboard with topology, telemetry and alerts" \
	org.opencontainers.image.source="https://github.com/marinfrankovic/Unifi-NOC" \
	org.opencontainers.image.documentation="https://github.com/marinfrankovic/Unifi-NOC/blob/main/docs/deployment.md" \
	org.opencontainers.image.url="https://marinfrankovic.github.io/Unifi-NOC/" \
	org.opencontainers.image.licenses="LicenseRef-PolyForm-Noncommercial-1.0.0" \
	org.opencontainers.image.version="${VERSION}" \
	org.opencontainers.image.revision="${REVISION}"
RUN apk add --no-cache openssl \
 && rm -f /etc/nginx/conf.d/default.conf \
 && mkdir -p /etc/nginx/certs
COPY frontend/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY frontend/10-selfsigned-cert.sh /docker-entrypoint.d/10-selfsigned-cert.sh
COPY LICENSE NOTICE /usr/share/licenses/unifi-noc/
ARG BASE_PATH=/unifi
COPY --from=build /src/dist /usr/share/nginx/html${BASE_PATH}
RUN chmod 755 /docker-entrypoint.d/10-selfsigned-cert.sh
ENV NGINX_ENVSUBST_FILTER="^(BASE_PATH|TLS_CN|HTTPS_PORT)$"
EXPOSE 8080 8443
