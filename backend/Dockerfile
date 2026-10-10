FROM python:3.13-slim AS base
ARG VERSION=1.0.0
ARG REVISION=local
LABEL org.opencontainers.image.title="Unifi-NOC Backend" \
	org.opencontainers.image.description="Noncommercial UniFi network monitoring API and collector" \
	org.opencontainers.image.source="https://github.com/marinfrankovic/Unifi-NOC" \
	org.opencontainers.image.documentation="https://github.com/marinfrankovic/Unifi-NOC/blob/main/docs/deployment.md" \
	org.opencontainers.image.url="https://marinfrankovic.github.io/Unifi-NOC/" \
	org.opencontainers.image.licenses="LicenseRef-PolyForm-Noncommercial-1.0.0" \
	org.opencontainers.image.version="${VERSION}" \
	org.opencontainers.image.revision="${REVISION}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt \
 && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin noc \
 && mkdir -p /var/lib/noc && chown noc:noc /var/lib/noc && chmod 700 /var/lib/noc
COPY backend/noc ./noc
COPY LICENSE NOTICE /usr/share/licenses/unifi-noc/
USER noc
# Default: API. The collector service overrides the command.
CMD ["uvicorn", "noc.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
