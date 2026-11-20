"""Authenticated HTTP launcher for the unmodified Taxuspt/garmin_mcp server."""

import hmac
import json
import os


class BearerAuth:
    """Protect every HTTP route except the public liveness probe."""

    def __init__(self, app, api_key, oauth=None):
        self.app = app
        self.api_key = api_key.encode("utf-8")
        self.oauth = oauth

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        public_probe = scope["path"] == "/healthz" and scope["method"] in ("GET", "HEAD")
        headers = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
        authorized = False
        if len(headers) == 1:
            scheme, separator, credential = headers[0].partition(b" ")
            authorized = bool(separator) and scheme.lower() == b"bearer" and hmac.compare_digest(credential, self.api_key)
            if not authorized and separator and scheme.lower() == b"bearer" and self.oauth:
                try:
                    token = await self.oauth.load_access_token(credential.decode("ascii"))
                except UnicodeDecodeError:
                    token = None
                authorized = token is not None and token.resource == self.oauth.resource and "garmin.personal" in token.scopes
        if public_probe or authorized or (self.oauth and self.oauth.is_public(scope)):
            return await self.app(scope, receive, send)
        challenge = b'Bearer realm="garmin-mcp"'
        if self.oauth:
            challenge += (', resource_metadata="' + self.oauth.issuer + '/.well-known/oauth-protected-resource/mcp"').encode()
        body = json.dumps({"error": "Authentication required"}).encode()
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"content-type", b"application/json"),
            (b"www-authenticate", challenge),
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})


def main():
    # Restrict any application-created credential copies from their first write.
    os.umask(0o077)
    api_key = os.environ.get("MCP_API_KEY", "")
    if len(api_key) < 32:
        raise SystemExit("Set MCP_API_KEY to a random secret of at least 32 characters in the host's secret settings.")

    os.environ["GARMIN_MCP_TRANSPORT"] = "streamable-http"
    os.environ["GARMIN_MCP_HOST"] = "0.0.0.0"
    os.environ["GARMIN_MCP_PORT"] = os.environ.get("PORT", "8000")
    os.environ["GARMIN_MCP_STATELESS_HTTP"] = "true"
    os.environ.setdefault("GARMINTOKENS", "/data/garmin-tokens")
    os.environ.setdefault("GARMIN_FIT_CONFIG", "/data/fit-config.json")
    os.environ.setdefault("GARMIN_FIT_DOWNLOAD_DIR", "/data/activity-files")

    import garmin_mcp
    import uvicorn
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    class HostedFastMCP(FastMCP):
        def run(self, transport="streamable-http", mount_path=None):
            if transport != "streamable-http":
                raise ValueError("The hosted launcher supports only streamable-http")
            # The outer bearer gate authenticates public hostnames, including
            # Railway's generated domain. No cookie/session credential is used.
            self.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
            app = self.streamable_http_app()
            oauth = None
            public_url = os.environ.get("MCP_PUBLIC_URL")
            if public_url:
                from oauth_server import PersonalOAuth, OAuthRouter
                oauth = PersonalOAuth(public_url, api_key, os.environ.get("MCP_OAUTH_STORE", "/data/garmin-oauth/state.sqlite3"))
                app = OAuthRouter(app, oauth)
            app = BearerAuth(app, api_key, oauth)
            uvicorn.run(app, host=self.settings.host, port=self.settings.port,
                        proxy_headers=False, access_log=False, log_level="warning")

    # Preserve upstream's client, registration, annotations, resources and tools.
    # Only replace its final HTTP serving layer; the vendored source is unchanged.
    garmin_mcp.FastMCP = HostedFastMCP
    garmin_mcp.main()


if __name__ == "__main__":
    main()
