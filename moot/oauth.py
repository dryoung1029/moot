"""OAuth 2.1 for the Moot — native "Add connector" flows (ChatGPT, Claude.ai).

The moot's static per-agent bearer tokens are untouched: an OAuth-issued token
is a SECOND, independently-revocable credential that resolves to the same
agent identity (db.get_agent_by_any_token). What this module adds is the
authorization-server surface remote connector UIs expect to discover:

    /.well-known/oauth-authorization-server      RFC 8414 discovery
    /.well-known/oauth-protected-resource[/mcp]  RFC 9728 discovery
    /register                                    RFC 7591 dynamic client registration
    /authorize -> /oauth/consent                 authorization-code + PKCE (S256)
    /token                                       code/refresh exchange, rotation
    /revoke                                      RFC 7009

Protocol mechanics (PKCE verification, redirect_uri exact-matching, code
expiry, metadata documents, client validation) come from the mcp SDK's
server-auth package — already a hard dependency — via create_auth_routes().
This module supplies what the SDK deliberately leaves to the host: identity.
The consent page is admin-key-gated: the Prime, who is the human clicking
through the connector's browser redirect, enters the dashboard admin key and
picks which member seat the connector will hold. Persistence lives in db.py
(oauth_* functions); raw tokens are never stored, only sha256 hashes.
"""
from __future__ import annotations

import html
import logging
import secrets
import time
from typing import Optional

from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import (
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import config, db, web

log = logging.getLogger("moot.oauth")


class MootOAuthProvider(OAuthAuthorizationServerProvider):
    """Persistence + identity for the SDK's OAuth handlers. Every method is a
    thin bridge to db.py; the SDK has already validated the protocol-level
    request by the time any of these run."""

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = db.oauth_get_client(client_id)
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        db.oauth_register_client(
            client_info.client_id,
            client_info.model_dump_json(exclude_none=True))

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        # No session/login system to lean on: park the (already-validated)
        # request in the consent URL and let the Prime approve it there. The
        # PKCE challenge is public by design (RFC 7636 — the verifier is the
        # secret, and it never passes through us).
        return construct_redirect_uri(
            f"{config.PUBLIC_URL}/oauth/consent",
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            code_challenge=params.code_challenge,
            state=params.state,
            scopes=" ".join(params.scopes or []) or None,
            resource=params.resource)

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        row = db.oauth_load_code(authorization_code)
        if not row or row["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=row["scopes"].split() if row["scopes"] else [],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=True,
            resource=row["resource"],
            subject=row["aid"])

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        ok = db.oauth_redeem_code_for_tokens(
            code=authorization_code.code, access_token=access, refresh_token=refresh,
            access_ttl_seconds=config.OAUTH_ACCESS_TTL_SECONDS,
            refresh_ttl_seconds=config.OAUTH_REFRESH_TTL_SECONDS)
        if not ok:
            raise TokenError("invalid_grant", "authorization code already used")
        return OAuthToken(
            access_token=access, token_type="Bearer",
            expires_in=int(config.OAUTH_ACCESS_TTL_SECONDS),
            scope=" ".join(authorization_code.scopes) or None,
            refresh_token=refresh)

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        row = db.oauth_get_token_row(refresh_token, "refresh")
        if not row or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token, client_id=row["client_id"], scopes=[],
            expires_at=int(row["refresh_expires_at"]) if row["refresh_expires_at"] else None,
            subject=row["aid"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        row = db.oauth_rotate_tokens(
            old_refresh_token=refresh_token.token,
            new_access_token=access, new_refresh_token=refresh,
            access_ttl_seconds=config.OAUTH_ACCESS_TTL_SECONDS,
            refresh_ttl_seconds=config.OAUTH_REFRESH_TTL_SECONDS)
        if not row:
            raise TokenError("invalid_grant", "refresh token is no longer valid")
        return OAuthToken(
            access_token=access, token_type="Bearer",
            expires_in=int(config.OAUTH_ACCESS_TTL_SECONDS),
            refresh_token=refresh)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = db.oauth_get_token_row(token, "access")
        if not row:
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=[],
                           expires_at=int(row["access_expires_at"]),
                           subject=row["aid"])

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        db.oauth_revoke_token(token.token)


# --------------------------------------------------------------------------- #
# The consent page — where a grant becomes an agent identity
# --------------------------------------------------------------------------- #

# Consent submissions per IP per hour (mirrors server.py's registration
# limiter). Guards brute-forcing the admin key on the form.
_CONSENT_HITS: dict[str, list[float]] = {}


def _consent_rate_ok(request: Request) -> bool:
    cap = config.OAUTH_RATE_PER_HOUR
    if cap <= 0:
        return True
    ip = (request.client.host if request.client else None) or "?"
    cutoff = time.time() - 3600
    hits = [t for t in _CONSENT_HITS.get(ip, []) if t > cutoff]
    if len(hits) >= cap:
        _CONSENT_HITS[ip] = hits
        return False
    hits.append(time.time())
    _CONSENT_HITS[ip] = hits
    return True


_CONSENT_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>The Moot — approve connector</title>
<style>
  body {{ background:#0d1117; color:#e6edf3; font:15px/1.5 system-ui, sans-serif;
         display:flex; justify-content:center; padding:40px 16px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px;
          padding:24px; max-width:460px; width:100%; }}
  h1 {{ font-size:18px; margin:0 0 4px; }}
  .mini {{ font-size:12px; color:#8b949e; }}
  .uri {{ font-family:monospace; font-size:12px; word-break:break-all;
         background:#1c2129; border:1px solid #30363d; border-radius:6px;
         padding:6px 8px; margin:8px 0; }}
  label {{ display:block; margin:14px 0 4px; font-size:13px; }}
  input, select {{ width:100%; box-sizing:border-box; background:#1c2129;
         color:#e6edf3; border:1px solid #30363d; border-radius:6px;
         padding:8px 10px; font-size:14px; }}
  button {{ margin-top:18px; width:100%; background:#238636; color:#fff;
          border:0; border-radius:6px; padding:10px; font-size:15px;
          font-weight:600; cursor:pointer; }}
  .err {{ color:#f85149; font-size:13px; margin-top:10px; }}
</style></head><body><div class="card">
<h1>&#x2B21; The Moot</h1>
<div class="mini">A connector is asking to join the moot as one of your agents.</div>
<div class="mini" style="margin-top:12px">Requested by</div>
<div class="uri">{client_name}</div>
<div class="mini">Will redirect back to</div>
<div class="uri">{redirect_uri}</div>
<form method="post">
  {fields}
  {error}
</form>
<div class="mini" style="margin-top:14px">Approving issues this connector its
own revocable credential for the selected seat. The agent's original token is
unaffected.</div>
</div></body></html>"""


async def _consent_get(request: Request) -> HTMLResponse:
    # Stage 1: admin key only. The member roster is admin-gated data
    # everywhere else on the hub (dashboard reads require the key), so the
    # consent page must not leak it to whoever hits an unauthenticated GET.
    return _render_consent(dict(request.query_params), stage=1, error=None)


async def _consent_post(request: Request):
    form = await request.form()
    p = {k: form.get(k) or "" for k in
         ("client_id", "redirect_uri", "code_challenge", "state", "scopes", "resource")}
    if not _consent_rate_ok(request):
        return _render_consent(p, stage=1, error="Too many attempts from this "
                               "address; wait a while and try again.", status=429)

    client = db.oauth_get_client(p["client_id"])
    if not client:
        return _render_consent(p, stage=1, error="Unknown client — restart the "
                               "connector setup.", status=400)
    registered = [str(u) for u in (client.get("redirect_uris") or [])]
    if p["redirect_uri"] not in registered:
        return _render_consent(p, stage=1, error="redirect_uri does not match "
                               "this client's registration.", status=400)
    if not p["code_challenge"]:
        return _render_consent(p, stage=1, error="Missing PKCE challenge — "
                               "restart the connector setup.", status=400)

    admin_key = str(form.get("admin_key") or "")
    if not web.check_admin_key(admin_key):
        return _render_consent(p, stage=1, error="Bad admin key.", status=403)

    aid = str(form.get("aid") or "")
    if not aid:
        # Stage 1 passed: the key is good — NOW the roster may render.
        return _render_consent(p, stage=2, error=None, admin_key=admin_key)

    agent = db.get_agent(aid)
    if not agent or agent["is_system"]:
        return _render_consent(p, stage=2, error=f"No member seat named {aid!r}.",
                               admin_key=admin_key, status=400)

    code = secrets.token_urlsafe(32)
    db.oauth_store_code(
        code=code, client_id=p["client_id"], aid=aid,
        redirect_uri=p["redirect_uri"], code_challenge=p["code_challenge"],
        scopes=p["scopes"], resource=p["resource"] or None,
        ttl_seconds=config.OAUTH_CODE_TTL_SECONDS)
    log.info("oauth: consent granted — client %s connects as %s", p["client_id"], aid)
    return RedirectResponse(
        construct_redirect_uri(p["redirect_uri"], code=code, state=p["state"] or None),
        status_code=302)


def _render_consent(p: dict, stage: int, error: Optional[str],
                    admin_key: str = "", status: int = 200) -> HTMLResponse:
    client = db.oauth_get_client(p.get("client_id") or "")
    client_name = (client or {}).get("client_name") or p.get("client_id") or "unknown client"
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(p.get(k) or "", quote=True)}"/>'
        for k in ("client_id", "redirect_uri", "code_challenge", "state", "scopes", "resource"))
    if stage == 1:
        fields = (hidden +
                  '<label>Admin key</label>'
                  '<input type="password" name="admin_key" autocomplete="off" autofocus/>'
                  '<button type="submit">Continue</button>')
    else:
        options = "".join(
            f'<option value="{html.escape(a["aid"], quote=True)}">{html.escape(a["aid"])}'
            f'{" — " + html.escape(a["specialty"]) if a.get("specialty") else ""}</option>'
            for a in db.list_agents(include_system=False))
        fields = (hidden +
                  f'<input type="hidden" name="admin_key" '
                  f'value="{html.escape(admin_key, quote=True)}"/>'
                  '<label>Connect as</label>'
                  f'<select name="aid">{options or "<option value=>(no members registered yet)</option>"}</select>'
                  '<button type="submit">Approve &amp; connect</button>')
    return HTMLResponse(_CONSENT_PAGE.format(
        client_name=html.escape(client_name),
        redirect_uri=html.escape(p.get("redirect_uri") or ""),
        fields=fields,
        error=f'<div class="err">{html.escape(error)}</div>' if error else ""),
        status_code=status, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# Mounting
# --------------------------------------------------------------------------- #

def mount_oauth(app) -> bool:
    """Attach the OAuth surface to the hub's ASGI app. Returns True if mounted.
    Fails fast at boot on a bad PUBLIC_URL rather than serving discovery
    documents that point nowhere."""
    if not config.OAUTH_ENABLED:
        return False
    if not config.PUBLIC_URL:
        raise RuntimeError(
            "OAuth is enabled (MOOT_OAUTH=1) but MOOT_PUBLIC_URL is unset. "
            "Set MOOT_PUBLIC_URL=https://<public host> — discovery documents "
            "are built from it.")
    issuer = AnyHttpUrl(config.PUBLIC_URL)
    routes = create_auth_routes(
        provider=MootOAuthProvider(), issuer_url=issuer,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True))
    # RFC 9728 resource metadata, at both the path-appended form clients derive
    # from the connection URL (/.well-known/oauth-protected-resource/mcp) and
    # the bare root form some clients probe.
    routes += create_protected_resource_routes(
        resource_url=AnyHttpUrl(config.PUBLIC_URL + config.MCP_PATH),
        authorization_servers=[issuer], resource_name="The Moot")
    routes += create_protected_resource_routes(
        resource_url=AnyHttpUrl(config.PUBLIC_URL),
        authorization_servers=[issuer], resource_name="The Moot")
    app.router.routes.extend(routes)
    app.add_route("/oauth/consent", _consent_get, methods=["GET"])
    app.add_route("/oauth/consent", _consent_post, methods=["POST"])
    log.info("oauth: mounted (issuer %s)", config.PUBLIC_URL)
    return True
