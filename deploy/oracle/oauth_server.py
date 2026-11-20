"""Single-owner OAuth adapter using the locked MCP SDK's protocol handlers.

The owner approves access with the existing server key. This module never
accepts Garmin passwords, imports Garmin tokens, or executes shell commands.
"""

import base64
import hashlib
import hmac
import html
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route
from starlette.applications import Starlette

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, cors_middleware, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


SCOPE = "garmin.personal"
SCOPES = [SCOPE, "offline_access"]
# Stable callback is supported by RFC 9207 issuer identification below.
CHATGPT_CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
COOKIE = "__Host-garmin-oauth"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}
CONSENT_CSP = "default-src 'none'; form-action 'self' https://chatgpt.com; frame-ancestors 'none'; base-uri 'none'"
LOGGER = logging.getLogger("garmin.oauth")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Database:
    """Commit/rollback and close without mutating SDK exception tracebacks."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.connection = sqlite3.connect(self.path, timeout=10)
        return self.connection

    def __exit__(self, kind, error, traceback):
        try:
            return self.connection.__exit__(kind, error, traceback)
        finally:
            self.connection.close()


class PersonalOAuth:
    """Persistent single-owner authorization server, not a multi-user Garmin service."""

    def __init__(self, base_url, api_key, store_path):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("MCP_PUBLIC_URL must be a public HTTPS origin without a path")
        self.issuer = base_url.rstrip("/")
        self.resource = self.issuer + "/mcp"
        self.api_key = api_key.encode()
        key = hashlib.sha256(b"garmin-personal-oauth-storage-v1\0" + self.api_key).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(key))
        self.path = Path(store_path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise ValueError("OAuth storage must not be a symlink")
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, key TEXT, value BLOB, expires INTEGER, family TEXT, PRIMARY KEY(kind,key))")
            db.execute("CREATE INDEX IF NOT EXISTS family_idx ON records(family)")
            db.execute("CREATE TABLE IF NOT EXISTS limits (bucket TEXT PRIMARY KEY, start INTEGER, count INTEGER)")
        self.registration = ClientRegistrationOptions(enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES)
        authn = ClientAuthenticator(self)
        self.authorization_handler = AuthorizationHandler(self)
        self.registration_handler = RegistrationHandler(self, self.registration)
        self.token_handler = TokenHandler(self, authn)
        self.revocation_handler = RevocationHandler(self, authn)
        routes = [
            Route("/.well-known/oauth-authorization-server", cors_middleware(self.metadata, ["GET", "OPTIONS"]), methods=["GET", "OPTIONS"]),
            Route("/authorize", self.authorize_http, methods=["GET", "POST"]),
            Route("/register", cors_middleware(self.register_http, ["POST", "OPTIONS"]), methods=["POST", "OPTIONS"]),
            Route("/token", cors_middleware(self.token_http, ["POST", "OPTIONS"]), methods=["POST", "OPTIONS"]),
            Route("/revoke", cors_middleware(self.revocation_handler.handle, ["POST", "OPTIONS"]), methods=["POST", "OPTIONS"]),
            Route("/oauth/consent", self.consent, methods=["GET", "POST"]),
        ]
        routes.extend(create_protected_resource_routes(AnyHttpUrl(self.resource), [AnyHttpUrl(self.issuer)], SCOPES, "Garmin Connect Personal"))
        self.public_routes = {r.path: set(r.methods) for r in routes}
        self.app = RequestBodyLimitMiddleware(Starlette(routes=routes), 16384)

    def db(self):
        return Database(self.path)

    def pack(self, value):
        return self.cipher.encrypt(json.dumps(value).encode())

    def get(self, kind, key):
        with self.db() as db:
            row = db.execute("SELECT value, expires FROM records WHERE kind=? AND key=?", (kind, key)).fetchone()
        if row is None or (row[1] and row[1] <= int(time.time())):
            return None
        try:
            return json.loads(self.cipher.decrypt(row[0]))
        except InvalidToken:
            # A changed owner key invalidates old OAuth credentials, fail closed.
            return None

    def put(self, db, kind, key, value, expires=0, family=None):
        db.execute("INSERT INTO records VALUES (?,?,?,?,?)", (kind, key, self.pack(value), expires, family))

    def allowed(self, bucket, maximum, window=300):
        now = int(time.time())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT start,count FROM limits WHERE bucket=?", (bucket,)).fetchone()
            start, count = row if row and row[0] > now-window else (now, 0)
            db.execute("INSERT OR REPLACE INTO limits VALUES (?,?,?)", (bucket, start, count+1))
            db.execute("DELETE FROM records WHERE expires > 0 AND expires <= ?", (now,))
        return count < maximum

    async def get_client(self, client_id):
        data = self.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        if [str(x) for x in client_info.redirect_uris or []] != [CHATGPT_CALLBACK]:
            raise RegistrationError("invalid_redirect_uri", "Only the ChatGPT callback is allowed")
        if client_info.token_endpoint_auth_method not in ("client_secret_post", "client_secret_basic", "none"):
            raise RegistrationError("invalid_client_metadata", "Unsupported client authentication method")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM records WHERE kind='client'").fetchone()[0] >= 1024:
                raise RegistrationError("invalid_client_metadata", "Registration capacity reached")
            self.put(db, "client", client_info.client_id, client_info.model_dump(mode="json"))

    async def authorize(self, client, params: AuthorizationParams):
        scopes = params.scopes if params.scopes is not None else (client.scope or "").split()
        if params.resource != self.resource:
            raise AuthorizeError("invalid_request", "Resource does not match this MCP server")
        if not params.state or len(params.state) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "State and an S256 PKCE challenge are required")
        if SCOPE not in scopes or not set(scopes) <= set(SCOPES):
            raise AuthorizeError("invalid_scope", "Personal Garmin scope is required")
        if not self.allowed("authorize", 60):
            raise AuthorizeError("temporarily_unavailable", "Try again later")
        ticket = secrets.token_urlsafe(32)
        data = params.model_dump(mode="json") | {"client_id":client.client_id, "scopes":scopes}
        with self.db() as db:
            self.put(db, "pending", digest(ticket), data, int(time.time())+300)
        return self.issuer + "/oauth/consent?request=" + ticket

    async def load_authorization_code(self, client, authorization_code):
        data = self.get("code", digest(authorization_code))
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate(data | {"code":authorization_code})

    def issue(self, db, client_id, scopes, family, family_expiry):
        now = int(time.time())
        access = secrets.token_urlsafe(48)
        refresh = secrets.token_urlsafe(48)
        expiry = min(now+3600, family_expiry)
        shared = {"client_id":client_id, "scopes":scopes, "resource":self.resource, "subject":"owner"}
        self.put(db, "access", digest(access), shared | {"expires_at":expiry}, expiry, family)
        if "offline_access" in scopes:
            self.put(db, "refresh", digest(refresh), shared | {"expires_at":family_expiry}, family_expiry, family)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=expiry-now, refresh_token=refresh if "offline_access" in scopes else None, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute("DELETE FROM records WHERE kind='code' AND key=? AND expires>?", (digest(authorization_code.code), int(time.time()))).rowcount
            if used != 1:
                raise TokenError("invalid_grant", "Code expired or already used")
            return self.issue(db, client.client_id, authorization_code.scopes, secrets.token_urlsafe(24), int(time.time())+2592000)

    async def load_refresh_token(self, client, refresh_token):
        hashed = digest(refresh_token)
        data = self.get("refresh", hashed)
        if data and data["client_id"] == client.client_id:
            return RefreshToken.model_validate(data | {"token":refresh_token})
        # Reuse of an already-rotated token invalidates the whole token family.
        with self.db() as db:
            row = db.execute("SELECT family FROM records WHERE kind='used_refresh' AND key=? AND expires>?", (hashed, int(time.time()))).fetchone()
            if row:
                db.execute("DELETE FROM records WHERE family=? AND kind IN ('access','refresh')", (row[0],))
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        now = int(time.time())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT family,expires FROM records WHERE kind='refresh' AND key=? AND expires>?", (digest(refresh_token.token), now)).fetchone()
            if not row:
                raise TokenError("invalid_grant", "Refresh token expired or already used")
            if SCOPE not in scopes or not set(scopes) <= set(refresh_token.scopes):
                raise TokenError("invalid_scope", "Cannot expand the granted scopes")
            family, expiry = row
            db.execute("DELETE FROM records WHERE family=? AND kind IN ('access','refresh')", (family,))
            self.put(db, "used_refresh", digest(refresh_token.token), {}, expiry, family)
            return self.issue(db, client.client_id, scopes, family, expiry)

    async def load_access_token(self, token):
        data = self.get("access", digest(token))
        return AccessToken.model_validate(data | {"token":token}) if data else None

    async def revoke_token(self, token):
        with self.db() as db:
            row = db.execute("SELECT family FROM records WHERE key=? AND kind IN ('access','refresh')", (digest(token.token),)).fetchone()
            if row:
                db.execute("DELETE FROM records WHERE family=?", (row[0],))

    async def metadata(self, request):
        data = build_metadata(AnyHttpUrl(self.issuer), None, self.registration, RevocationOptions(enabled=True)).model_dump(mode="json", exclude_none=True)
        data["issuer"] = self.issuer
        data["authorization_response_iss_parameter_supported"] = True
        data["token_endpoint_auth_methods_supported"] = ["client_secret_post", "client_secret_basic", "none"]
        return JSONResponse(data, headers=SECURITY_HEADERS)

    async def authorize_http(self, request):
        fields = request.query_params if request.method == "GET" else await request.form()
        if len(fields.multi_items()) != len(fields):
            return JSONResponse({"error":"invalid_request"}, status_code=400, headers=SECURITY_HEADERS)
        response = await self.authorization_handler.handle(request)
        location = response.headers.get("location", "")
        if location.split("?", 1)[0] == CHATGPT_CALLBACK:
            # Include issuer on both success and error callback responses, RFC 9207.
            response.headers["location"] = construct_redirect_uri(location, iss=self.issuer)
        return response

    async def register_http(self, request):
        if not self.allowed("register", 20):
            return JSONResponse({"error":"temporarily_unavailable"}, status_code=429)
        try:
            await request.json()
        except ValueError:
            return JSONResponse({"error":"invalid_client_metadata"}, status_code=400)
        return await self.registration_handler.handle(request)

    async def token_http(self, request):
        form = await request.form()
        LOGGER.warning("Token request received: grant=%s resource_present=%s verifier_present=%s", form.get("grant_type"), bool(form.get("resource")), bool(form.get("code_verifier")))
        # The resource is bound to the authorization code and refresh token.
        # OAuth clients may omit the resource indicator at token exchange.
        if len(form.multi_items()) != len(form) or (form.get("resource") and form.get("resource") != self.resource):
            return JSONResponse({"error":"invalid_request", "error_description":"Resource must match when supplied"}, status_code=400, headers=SECURITY_HEADERS)
        if form.get("grant_type") == "authorization_code" and not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", str(form.get("code_verifier", ""))):
            return JSONResponse({"error":"invalid_request", "error_description":"Invalid PKCE verifier"}, status_code=400, headers=SECURITY_HEADERS)
        response = await self.token_handler.handle(request)
        LOGGER.warning("Token response status=%s", response.status_code)
        return response

    async def consent(self, request: Request):
        if request.method == "GET":
            ticket = request.query_params.get("request", "")
        else:
            form = await request.form()
            ticket = form.get("request", "")
            if not isinstance(ticket, str) or len(form.multi_items()) != len(form):
                return HTMLResponse("Invalid approval request", status_code=400, headers=SECURITY_HEADERS)
        if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
            return HTMLResponse("Approval request expired. Start again from ChatGPT.", status_code=400, headers=SECURITY_HEADERS)
        data = self.get("pending", digest(ticket))
        if not data:
            return HTMLResponse("Approval request expired. Start again from ChatGPT.", status_code=400, headers=SECURITY_HEADERS)
        if request.method == "GET":
            csrf = secrets.token_urlsafe(32)
            data["csrf"] = digest(csrf)
            with self.db() as db:
                db.execute("UPDATE records SET value=? WHERE kind='pending' AND key=?", (self.pack(data), digest(ticket)))
            duration = "This connection can stay signed in for up to 30 days." if "offline_access" in data["scopes"] else "This connection expires after one hour."
            body = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Connect Garmin to ChatGPT</title>
<h1>Connect Garmin Connect Personal</h1>
<p>Approve ChatGPT access to your personal Garmin server. The original tools include reading, changing, and deleting Garmin data. ChatGPT's tool permissions still apply.</p>
<p>{duration}</p>
<p>This authorizes the MCP connection. Garmin account authentication is managed separately.</p>
<form method="post" action="/oauth/consent">
<input type="hidden" name="request" value="{html.escape(ticket, quote=True)}">
<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">
<label>Server access key <input name="access_key" type="password" autocomplete="current-password" required maxlength="1024"></label>
<button type="submit">Approve ChatGPT connection</button></form></html>'''
            # Keep same-origin form posts identifiable without leaking referrers
            # to ChatGPT or any other cross-origin destination.
            response = HTMLResponse(body, headers=SECURITY_HEADERS | {"Referrer-Policy": "same-origin", "Content-Security-Policy": CONSENT_CSP})
            response.set_cookie(COOKIE, csrf, max_age=300, secure=True, httponly=True, samesite="lax", path="/")
            return response
        csrf = form.get("csrf", "")
        cookie = request.cookies.get(COOKIE, "")
        access_key = form.get("access_key", "")
        valid_csrf = isinstance(csrf, str) and isinstance(cookie, str) and csrf and hmac.compare_digest(csrf.encode(), cookie.encode()) and hmac.compare_digest(digest(csrf), data.get("csrf", ""))
        if request.headers.get("origin") != self.issuer or not valid_csrf:
            return HTMLResponse("Invalid approval request", status_code=403, headers=SECURITY_HEADERS)
        if not self.allowed("owner_approval", 10) or not isinstance(access_key, str) or not hmac.compare_digest(access_key.encode(), self.api_key):
            return HTMLResponse("Approval failed. Start again from ChatGPT.", status_code=403, headers=SECURITY_HEADERS)
        code = secrets.token_urlsafe(48)
        payload = {k:data[k] for k in ("scopes", "client_id", "code_challenge", "redirect_uri", "redirect_uri_provided_explicitly", "resource")}
        payload.update(expires_at=int(time.time())+120, subject="owner")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute("DELETE FROM records WHERE kind='pending' AND key=? AND expires>?", (digest(ticket), int(time.time()))).rowcount
            if used != 1:
                return HTMLResponse("Approval request expired", status_code=400, headers=SECURITY_HEADERS)
            self.put(db, "code", digest(code), payload, payload["expires_at"])
        response = RedirectResponse(construct_redirect_uri(data["redirect_uri"], code=code, state=data["state"], iss=self.issuer), status_code=302, headers=SECURITY_HEADERS | {"Content-Security-Policy": CONSENT_CSP})
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    def is_public(self, scope):
        return scope.get("method") in self.public_routes.get(scope.get("path"), set())

    async def __call__(self, scope, receive, send):
        async def security_send(message):
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [(k.lower().encode(), v.encode()) for k,v in SECURITY_HEADERS.items() if k.lower().encode() not in existing]
            await send(message)
        await self.app(scope, receive, security_send)


class OAuthRouter:
    def __init__(self, app, oauth):
        self.app, self.oauth = app, oauth

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") in self.oauth.public_routes:
            return await self.oauth(scope, receive, send)
        return await self.app(scope, receive, send)
