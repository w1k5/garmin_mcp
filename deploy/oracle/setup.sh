#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 PUBLIC_HOSTNAME" >&2
  exit 2
fi
host=$1
if [[ ! $host =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "PUBLIC_HOSTNAME must be a DNS hostname" >&2
  exit 2
fi
repo=$(cd "$(dirname "$0")/../.." && pwd)
if [[ $repo != /opt/garmin-mcp/app ]]; then
  echo "Clone this repository to /opt/garmin-mcp/app before running setup." >&2
  exit 2
fi
command -v uv >/dev/null || { echo "Install uv first." >&2; exit 1; }
caddy_bin=$(command -v caddy) || { echo "Install Caddy first." >&2; exit 1; }
command -v openssl >/dev/null || { echo "Install OpenSSL first." >&2; exit 1; }

cd "$repo"
uv sync --frozen --python 3.12

sudo install -d -m 0700 -o ubuntu -g ubuntu /srv/garmin-mcp/data
sudo install -d -m 0700 -o ubuntu -g ubuntu /srv/garmin-mcp/data/garmin-tokens
sudo install -d -m 0700 -o ubuntu -g ubuntu /srv/garmin-mcp/data/garmin-oauth
sudo install -d -m 0755 /etc/caddy
if ! id caddy >/dev/null 2>&1; then
  sudo useradd --system --home-dir /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy
fi
sudo install -d -m 0700 -o caddy -g caddy /var/lib/caddy/data /var/lib/caddy/config

if [[ ! -f /etc/garmin-mcp.env ]]; then
  key=$(openssl rand -hex 32)
  printf 'MCP_API_KEY=%s\nMCP_PUBLIC_URL=https://%s\nGARMINTOKENS=/srv/garmin-mcp/data/garmin-tokens\nGARMIN_FIT_CONFIG=/srv/garmin-mcp/data/fit-config.json\nGARMIN_FIT_DOWNLOAD_DIR=/srv/garmin-mcp/data/activity-files\nMCP_OAUTH_STORE=/srv/garmin-mcp/data/garmin-oauth/state.sqlite3\nPORT=8000\n' "$key" "$host" | sudo sh -c 'umask 077; cat > /etc/garmin-mcp.env'
  unset key
  sudo chmod 0600 /etc/garmin-mcp.env
else
  echo "Keeping existing /etc/garmin-mcp.env and its access key."
fi

printf '%s {\n    reverse_proxy 127.0.0.1:8000\n}\n' "$host" | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo install -m 0644 "$repo/deploy/oracle/garmin-mcp.service" /etc/systemd/system/garmin-mcp.service
sed "s|@CADDY_BIN@|$caddy_bin|g" "$repo/deploy/oracle/caddy.service.template" | sudo tee /etc/systemd/system/caddy.service >/dev/null
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable garmin-mcp.service caddy.service
sudo systemctl restart garmin-mcp.service caddy.service
echo "Services started. Complete Garmin sign-in, then check https://$host/healthz."
