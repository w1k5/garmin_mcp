#!/usr/bin/env bash
set -euo pipefail
sudo systemctl is-active garmin-mcp caddy
sudo sh -c 'set -a; . /etc/garmin-mcp.env; set +a; curl --fail --silent --show-error "$MCP_PUBLIC_URL/healthz"'
echo
