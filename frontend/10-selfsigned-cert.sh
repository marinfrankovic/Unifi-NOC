#!/bin/sh
# Generates a self-signed certificate on first start unless one is mounted at /etc/nginx/certs.
set -eu
CERT=/etc/nginx/certs/tls.crt
KEY=/etc/nginx/certs/tls.key
if [ ! -s "$CERT" ] || [ ! -s "$KEY" ]; then
  echo "unifi-noc: generating self-signed certificate for ${TLS_CN:-localhost}"
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=${TLS_CN:-localhost}" \
    -addext "subjectAltName=DNS:${TLS_CN:-localhost},DNS:localhost,IP:127.0.0.1" -keyout "$KEY" -out "$CERT" 2>/dev/null
  chmod 600 "$KEY"
fi
