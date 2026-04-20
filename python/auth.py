"""All-in-one OAuth 2.1 server + JWT middleware for the MCP server.

Implements the full MCP auth spec (2025-11-25) in a single file with zero
external dependencies beyond the app itself — no Dex, no Caddy, no separate
containers. Everything runs inside the App Lab container on port 7000.

The flow:
  1. Client discovers endpoints via /.well-known/oauth-protected-resource
  2. Client self-registers via POST /oauth/register (DCR shim)
  3. Client redirects user to GET /oauth/authorize (login form)
  4. User enters email + password → POST /oauth/authorize
  5. App validates credentials, generates auth code, redirects to client
  6. Client exchanges code at POST /oauth/token → gets JWT
  7. Client calls /blink with Bearer token → middleware validates JWT

Credentials are stored as AUTH_EMAIL + AUTH_PASSWORD_HASH (bcrypt) in .env.
"""

from __future__ import annotations

import hashlib
import html
import os
import secrets
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlencode, urlparse, parse_qs

import bcrypt
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
STATIC_CLIENT_ID = os.environ["STATIC_CLIENT_ID"]
STATIC_CLIENT_SECRET = os.environ["STATIC_CLIENT_SECRET"]
AUTH_EMAIL = os.environ["AUTH_EMAIL"]
AUTH_PASSWORD_HASH = os.environ["AUTH_PASSWORD_HASH"]

MCP_PATH = os.environ.get("MCP_PATH", "/blink")
MCP_PATH_SEGMENT = MCP_PATH.strip("/")
PROTECTED_RESOURCE = f"{PUBLIC_URL}{MCP_PATH}"
ISSUER = PUBLIC_URL

CORS_ORIGINS_ALLOWED: set[str] = {
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "").split(",")
    if o.strip()
}

CORS_HEADERS_COMMON = {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS, DELETE",
    "Access-Control-Allow-Headers": (
        "Authorization, Content-Type, Mcp-Session-Id, MCP-Protocol-Version, "
        "Last-Event-ID, Accept"
    ),
    "Access-Control-Expose-Headers": "WWW-Authenticate, Mcp-Session-Id",
    "Access-Control-Max-Age": "86400",
}

WWW_AUTHENTICATE = (
    f'Bearer resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource"'
)

# --- RSA signing key ----------------------------------------------------------

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_key = _private_key.public_key()
_kid = f"mcp-{uuid.uuid4().hex[:8]}"


def _jwk_public() -> dict:
    from jwt.algorithms import RSAAlgorithm
    jwk_dict = RSAAlgorithm.to_jwk(_public_key, as_dict=True)
    jwk_dict["kid"] = _kid
    jwk_dict["use"] = "sig"
    jwk_dict["alg"] = "RS256"
    return jwk_dict


# --- Authorization codes (in-memory, short-lived) -----------------------------

_codes_lock = threading.Lock()
_codes: dict[str, dict] = {}
_CODE_TTL = 300


def _store_code(code: str, data: dict) -> None:
    with _codes_lock:
        # Prune expired codes
        now = time.time()
        expired = [k for k, v in _codes.items() if now - v["created_at"] > _CODE_TTL]
        for k in expired:
            del _codes[k]
        data["created_at"] = now
        _codes[code] = data


def _consume_code(code: str) -> dict | None:
    with _codes_lock:
        data = _codes.pop(code, None)
        if data and (time.time() - data["created_at"]) > _CODE_TTL:
            return None
        return data


# --- Token verification -------------------------------------------------------

def verify_bearer(token: str) -> dict[str, Any]:
    return jwt.decode(
        token, _public_key, algorithms=["RS256"],
        issuer=ISSUER,
        audience=PROTECTED_RESOURCE,
        options={"require": ["iss", "exp", "aud"]},
    )


# --- Middleware ---------------------------------------------------------------

class McpAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin")
        allow_origin = origin if origin in CORS_ORIGINS_ALLOWED else None

        if request.method == "OPTIONS":
            headers = dict(CORS_HEADERS_COMMON)
            if allow_origin:
                headers["Access-Control-Allow-Origin"] = allow_origin
                headers["Access-Control-Allow-Credentials"] = "true"
            elif origin:
                headers["Access-Control-Allow-Origin"] = "*"
            return Response(status_code=204, headers=headers)

        path = request.url.path
        if path == MCP_PATH or path.startswith(MCP_PATH + "/"):
            auth_header = request.headers.get("authorization", "")
            if not auth_header.lower().startswith("bearer "):
                return _unauthorized(allow_origin)
            token = auth_header.split(" ", 1)[1].strip()
            try:
                request.state.claims = verify_bearer(token)
            except Exception:
                return _unauthorized(allow_origin)

        response = await call_next(request)
        if allow_origin:
            response.headers["Access-Control-Allow-Origin"] = allow_origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        return response


def _unauthorized(allow_origin: str | None) -> Response:
    headers = {"WWW-Authenticate": WWW_AUTHENTICATE, **CORS_HEADERS_COMMON}
    if allow_origin:
        headers["Access-Control-Allow-Origin"] = allow_origin
        headers["Access-Control-Allow-Credentials"] = "true"
    return JSONResponse({"error": "unauthorized"}, status_code=401, headers=headers)


# --- Routes -------------------------------------------------------------------

router = APIRouter()

# -- Discovery --

@router.get("/.well-known/oauth-protected-resource")
@router.get(f"/.well-known/oauth-protected-resource/{MCP_PATH_SEGMENT}")
async def protected_resource_metadata() -> JSONResponse:
    return JSONResponse({
        "resource": PROTECTED_RESOURCE,
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["openid", "profile", "email", "offline_access"],
    })


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata() -> JSONResponse:
    return JSONResponse({
        "issuer": ISSUER,
        "authorization_endpoint": f"{PUBLIC_URL}/oauth/authorize",
        "token_endpoint": f"{PUBLIC_URL}/oauth/token",
        "jwks_uri": f"{PUBLIC_URL}/oauth/keys",
        "registration_endpoint": f"{PUBLIC_URL}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post", "client_secret_basic"
        ],
        "scopes_supported": ["openid", "profile", "email", "offline_access"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "authorization_response_iss_parameter_supported": True,
    })


# -- DCR shim --

@router.post("/oauth/register")
async def register(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}

    now = int(time.time())
    redirect_uris = body.get("redirect_uris") or [
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    ]
    return JSONResponse(status_code=201, content={
        "client_id": STATIC_CLIENT_ID,
        "client_secret": STATIC_CLIENT_SECRET,
        "client_id_issued_at": now,
        "client_secret_expires_at": 0,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": body.get(
            "token_endpoint_auth_method", "client_secret_post"
        ),
        "grant_types": body.get("grant_types", ["authorization_code", "refresh_token"]),
        "response_types": ["code"],
        "client_name": body.get("client_name", "MCP Client"),
        "scope": "openid profile email offline_access",
    })


# -- JWKS --

@router.get("/oauth/keys")
async def jwks() -> JSONResponse:
    return JSONResponse({"keys": [_jwk_public()]})


# -- Authorization --

_LOGIN_HTML = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCP Login</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 24rem; margin: 3rem auto; padding: 0 1rem; color: #222 }}
h1 {{ font-weight: 600; font-size: 1.3rem }}
label {{ display: block; margin: .75rem 0 .2rem; font-size: .9rem }}
input {{ padding: .5rem; border: 1px solid #d4d4d8; border-radius: .25rem; width: 100%; box-sizing: border-box }}
button {{ padding: .6rem 1.2rem; border: none; border-radius: .25rem; background: #2563eb; color: #fff; cursor: pointer; margin-top: 1rem; font-size: .95rem }}
button:hover {{ background: #1d4ed8 }}
.err {{ color: #b91c1c; margin: .5rem 0; font-size: .9rem }}
.muted {{ color: #6b7280; font-size: .85rem }}
</style>
<h1>Authorize MCP access</h1>
<p class="muted">Sign in to grant access to your Arduino UNO Q.</p>
{error}
<form method="post">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
  <input type="hidden" name="scope" value="{scope}">
  <label>Email<input type="email" name="email" required autocomplete="username"></label>
  <label>Password<input type="password" name="password" required autocomplete="current-password"></label>
  <button type="submit">Sign in</button>
</form>"""


@router.get("/oauth/authorize")
async def authorize_get(request: Request) -> HTMLResponse:
    params = request.query_params
    return HTMLResponse(_LOGIN_HTML.format(
        state=html.escape(params.get("state", "")),
        redirect_uri=html.escape(params.get("redirect_uri", "")),
        client_id=html.escape(params.get("client_id", "")),
        code_challenge=html.escape(params.get("code_challenge", "")),
        code_challenge_method=html.escape(params.get("code_challenge_method", "S256")),
        scope=html.escape(params.get("scope", "openid")),
        error="",
    ))


@router.post("/oauth/authorize")
async def authorize_post(request: Request) -> Response:
    form = await request.form()
    email = form.get("email", "")
    password = form.get("password", "")
    state = form.get("state", "")
    redirect_uri = str(form.get("redirect_uri", ""))
    client_id = form.get("client_id", "")
    code_challenge = form.get("code_challenge", "")
    code_challenge_method = form.get("code_challenge_method", "S256")
    scope = form.get("scope", "openid")

    # Validate credentials
    valid = (
        email == AUTH_EMAIL
        and bcrypt.checkpw(password.encode(), AUTH_PASSWORD_HASH.encode())
    )
    if not valid:
        return HTMLResponse(_LOGIN_HTML.format(
            state=html.escape(state),
            redirect_uri=html.escape(redirect_uri),
            client_id=html.escape(client_id),
            code_challenge=html.escape(code_challenge),
            code_challenge_method=html.escape(code_challenge_method),
            scope=html.escape(scope),
            error='<p class="err">Invalid email or password.</p>',
        ), status_code=401)

    # Generate authorization code
    code = secrets.token_urlsafe(32)
    _store_code(code, {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "scope": scope,
        "email": email,
    })

    # Redirect back to client
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}{urlencode({'code': code, 'state': state, 'iss': ISSUER})}"
    return RedirectResponse(location, status_code=302)


# -- Token exchange --

@router.post("/oauth/token")
async def token_exchange(request: Request) -> JSONResponse:
    body = await request.body()
    params = dict(parse_qs(body.decode(), keep_blank_values=True))
    # parse_qs returns lists; flatten single values
    p = {k: v[0] if len(v) == 1 else v for k, v in params.items()}

    grant_type = p.get("grant_type")

    if grant_type == "authorization_code":
        code = p.get("code", "")
        code_verifier = p.get("code_verifier", "")
        client_id = p.get("client_id", "")

        data = _consume_code(code)
        if not data:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid or expired code"},
                status_code=400,
            )

        # Verify PKCE
        if data.get("code_challenge"):
            expected = data["code_challenge"]
            if data.get("code_challenge_method") == "S256":
                digest = hashlib.sha256(code_verifier.encode()).digest()
                import base64
                computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            else:
                computed = code_verifier
            if computed != expected:
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                    status_code=400,
                )

        now = int(time.time())
        access_claims = {
            "iss": ISSUER,
            "sub": data["email"],
            "aud": PROTECTED_RESOURCE,
            "exp": now + 3600,
            "iat": now,
            "email": data["email"],
            "scope": data.get("scope", "openid"),
        }
        access_token = jwt.encode(
            access_claims, _private_key, algorithm="RS256", headers={"kid": _kid}
        )

        id_claims = {
            "iss": ISSUER,
            "sub": data["email"],
            "aud": client_id,
            "exp": now + 3600,
            "iat": now,
            "email": data["email"],
        }
        id_token = jwt.encode(
            id_claims, _private_key, algorithm="RS256", headers={"kid": _kid}
        )

        # Issue a refresh token (opaque, stored in-memory)
        refresh = secrets.token_urlsafe(32)
        _store_code(f"refresh:{refresh}", {
            "client_id": client_id,
            "email": data["email"],
            "scope": data.get("scope", "openid"),
        })

        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "id_token": id_token,
            "refresh_token": refresh,
            "scope": data.get("scope", "openid"),
        })

    elif grant_type == "refresh_token":
        refresh = p.get("refresh_token", "")
        data = _consume_code(f"refresh:{refresh}")
        if not data:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid refresh token"},
                status_code=400,
            )

        now = int(time.time())
        access_claims = {
            "iss": ISSUER,
            "sub": data["email"],
            "aud": PROTECTED_RESOURCE,
            "exp": now + 3600,
            "iat": now,
            "email": data["email"],
            "scope": data.get("scope", "openid"),
        }
        access_token = jwt.encode(
            access_claims, _private_key, algorithm="RS256", headers={"kid": _kid}
        )

        new_refresh = secrets.token_urlsafe(32)
        _store_code(f"refresh:{new_refresh}", data)

        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": new_refresh,
            "scope": data.get("scope", "openid"),
        })

    return JSONResponse(
        {"error": "unsupported_grant_type"}, status_code=400
    )


def install(app) -> None:
    app.add_middleware(McpAuthMiddleware)
    app.include_router(router)
