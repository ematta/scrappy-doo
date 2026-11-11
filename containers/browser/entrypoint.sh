#!/bin/sh
set -eu
: "${SCRAPPY_PROXY_ADDRESS:?Proxy address required}"
: "${SCRAPPY_PROXY_CA_CERT:?Public proxy certificate required}"
test -r "$SCRAPPY_PROXY_CA_CERT"
if grep -q 'PRIVATE KEY' "$SCRAPPY_PROXY_CA_CERT"; then
    printf '%s\n' 'A public certificate is required' >&2
    exit 1
fi
nss_dir="$HOME/.local/share/pki/nssdb"
mkdir -p "$nss_dir"
certutil -N -d "sql:$nss_dir" --empty-password
certutil -A -d "sql:$nss_dir" -n scrappy-proxy -t 'C,,' -i "$SCRAPPY_PROXY_CA_CERT"
exec xvfb-run -a /opt/scrappy/node_modules/.bin/playwright-mcp --browser chromium --isolated --sandbox --proxy-server "$SCRAPPY_PROXY_ADDRESS"
