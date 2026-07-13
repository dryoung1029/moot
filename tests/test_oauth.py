"""Tests for OAuth 2.1 connector auth: the db-layer grant lifecycle (codes,
token pairs, rotation, revocation, agent-cascade), the unified any-token
resolver both auth surfaces now use, and — when the mcp SDK + httpx are
available — a live end-to-end authorize -> consent -> token -> API flow
against a real hub subprocess, including refresh rotation and revocation."""
import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from moot import actions, config, db
from tests._util import fresh_store

try:
    import httpx  # ships with the mcp SDK; absent only in minimal sandboxes
    _HAVE_HTTPX = True
except ImportError:
    _HAVE_HTTPX = False


def _reg(name):
    return actions.register(purpose="work", specialty="x",
                            proposed_name=name, history=None, origin="test")


def _client(client_id="c-1", redirect="https://chatgpt.com/connector/oauth/cb"):
    db.oauth_register_client(client_id, json.dumps({
        "client_id": client_id, "client_name": "Test Connector",
        "redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"}))
    return client_id, redirect


class TestOAuthGrantLifecycle(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codex")
        self.cid, self.redirect = _client()

    def _code(self, code="rawcode", ttl=300.0):
        db.oauth_store_code(code=code, client_id=self.cid, aid="Codex",
                            redirect_uri=self.redirect, code_challenge="chal",
                            scopes="", resource=None, ttl_seconds=ttl)
        return code

    def test_client_roundtrip(self):
        info = db.oauth_get_client(self.cid)
        self.assertEqual(info["client_name"], "Test Connector")
        self.assertEqual(info["redirect_uris"], [self.redirect])
        self.assertIsNone(db.oauth_get_client("nope"))

    def test_code_loads_then_single_use_redemption(self):
        code = self._code()
        row = db.oauth_load_code(code)
        self.assertEqual((row["aid"], row["client_id"]), ("Codex", self.cid))
        ok = db.oauth_redeem_code_for_tokens(
            code=code, access_token="A1", refresh_token="R1",
            access_ttl_seconds=3600, refresh_ttl_seconds=None)
        self.assertTrue(ok)
        # replay: the code is consumed — no second pair, no load
        self.assertFalse(db.oauth_redeem_code_for_tokens(
            code=code, access_token="A2", refresh_token="R2",
            access_ttl_seconds=3600, refresh_ttl_seconds=None))
        self.assertIsNone(db.oauth_load_code(code))
        self.assertIsNone(db.oauth_get_token_row("A2", "access"))

    def test_expired_code_still_loads_for_handler_level_rejection(self):
        code = self._code(ttl=-1)
        row = db.oauth_load_code(code)
        self.assertLess(row["expires_at"], time.time())

    def test_minted_tokens_resolve_and_expire(self):
        code = self._code()
        db.oauth_redeem_code_for_tokens(code=code, access_token="A1",
                                        refresh_token="R1",
                                        access_ttl_seconds=-1,  # born expired
                                        refresh_ttl_seconds=None)
        self.assertIsNone(db.oauth_get_token_row("A1", "access"))
        self.assertIsNotNone(db.oauth_get_token_row("R1", "refresh"))  # no expiry

    def test_rotation_kills_old_pair_and_mints_new(self):
        db.oauth_redeem_code_for_tokens(code=self._code(), access_token="A1",
                                        refresh_token="R1",
                                        access_ttl_seconds=3600, refresh_ttl_seconds=None)
        row = db.oauth_rotate_tokens(old_refresh_token="R1", new_access_token="A2",
                                     new_refresh_token="R2",
                                     access_ttl_seconds=3600, refresh_ttl_seconds=None)
        self.assertEqual(row["aid"], "Codex")
        self.assertIsNone(db.oauth_get_token_row("A1", "access"))
        self.assertIsNone(db.oauth_get_token_row("R1", "refresh"))
        self.assertIsNotNone(db.oauth_get_token_row("A2", "access"))
        # old refresh can't rotate twice
        self.assertIsNone(db.oauth_rotate_tokens(
            old_refresh_token="R1", new_access_token="A3", new_refresh_token="R3",
            access_ttl_seconds=3600, refresh_ttl_seconds=None))

    def test_revoke_by_either_half_kills_the_pair(self):
        db.oauth_redeem_code_for_tokens(code=self._code(), access_token="A1",
                                        refresh_token="R1",
                                        access_ttl_seconds=3600, refresh_ttl_seconds=None)
        self.assertTrue(db.oauth_revoke_token("R1"))
        self.assertIsNone(db.oauth_get_token_row("A1", "access"))
        self.assertFalse(db.oauth_revoke_token("R1"))  # already revoked

    def test_revoke_all_for_agent(self):
        for i in range(2):
            db.oauth_redeem_code_for_tokens(code=self._code(f"code{i}"),
                                            access_token=f"A{i}", refresh_token=f"R{i}",
                                            access_ttl_seconds=3600, refresh_ttl_seconds=None)
        self.assertEqual(db.oauth_revoke_all_for_agent("Codex"), 2)
        self.assertEqual(db.oauth_grants_for_agent("Codex"), [])

    def test_agent_revocation_cascades_grants_away(self):
        db.oauth_redeem_code_for_tokens(code=self._code(), access_token="A1",
                                        refresh_token="R1",
                                        access_ttl_seconds=3600, refresh_ttl_seconds=None)
        db.revoke_agent("Codex")
        self.assertIsNone(db.oauth_get_token_row("A1", "access"))

    def test_static_token_reissue_leaves_grants_alone(self):
        # Deliberate: the two credential families are independent — rotating a
        # leaked static token must not silently break a working connector.
        db.oauth_redeem_code_for_tokens(code=self._code(), access_token="A1",
                                        refresh_token="R1",
                                        access_ttl_seconds=3600, refresh_ttl_seconds=None)
        db.reissue_token("Codex", "brand-new-static-token")
        self.assertIsNotNone(db.oauth_get_token_row("A1", "access"))


class TestAnyTokenResolver(unittest.TestCase):
    def setUp(self):
        fresh_store()
        self.static_token = _reg("Codex")["token"]
        cid, redirect = _client()
        db.oauth_store_code(code="c", client_id=cid, aid="Codex",
                            redirect_uri=redirect, code_challenge="ch",
                            scopes="", resource=None, ttl_seconds=300)
        db.oauth_redeem_code_for_tokens(code="c", access_token="OA1",
                                        refresh_token="OR1",
                                        access_ttl_seconds=3600, refresh_ttl_seconds=None)

    def test_static_token_still_resolves(self):
        agent = db.get_agent_by_any_token(self.static_token)
        self.assertEqual(agent["aid"], "Codex")

    def test_oauth_token_resolves_to_same_agent_shape(self):
        via_static = db.get_agent_by_any_token(self.static_token)
        via_oauth = db.get_agent_by_any_token("OA1")
        self.assertEqual(via_oauth["aid"], "Codex")
        self.assertEqual(set(via_static.keys()), set(via_oauth.keys()))

    def test_oauth_resolution_stamps_last_used(self):
        db.get_agent_by_any_token("OA1")
        grants = db.oauth_grants_for_agent("Codex")
        self.assertIsNotNone(grants[0]["last_used_at"])

    def test_garbage_and_refresh_tokens_do_not_authenticate(self):
        self.assertIsNone(db.get_agent_by_any_token("garbage"))
        # a refresh token is for /token only, never a live API credential
        self.assertIsNone(db.get_agent_by_any_token("OR1"))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(_HAVE_HTTPX, "httpx (mcp SDK dependency) not installed")
class TestLiveOAuthFlow(unittest.TestCase):
    """Full connector handshake against a real hub subprocess: discovery ->
    DCR -> authorize -> consent (admin key + seat picker) -> PKCE token
    exchange -> authenticated API call -> refresh rotation -> revocation."""

    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.data = tempfile.mkdtemp(prefix="mootoauth-")
        env = dict(os.environ,
                   MOOT_DATA_DIR=cls.data, MOOT_ADMIN_KEY="testkey",
                   MOOT_HOST="127.0.0.1", MOOT_PORT=str(cls.port),
                   MOOT_PUBLIC_URL=cls.base)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "moot.server"], env=env, cwd=root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.base + "/healthz", timeout=1).read()
                return
            except Exception:
                if cls.proc.poll() is not None:
                    raise unittest.SkipTest("hub process exited during startup")
                time.sleep(0.2)
        raise unittest.SkipTest("hub did not come up in time")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "proc", None):
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except Exception:
                cls.proc.kill()

    def test_full_connector_handshake(self):
        c = httpx.Client(follow_redirects=False, timeout=10)

        # An agent exists to connect as (registered via REST, static path).
        reg = c.post(self.base + "/v1/register",
                     json={"purpose": "agentic coding", "proposed_name": "Codex"}).json()
        self.assertEqual(reg["aid"], "Codex")

        # 1. Discovery: both documents advertise the endpoints.
        meta = c.get(self.base + "/.well-known/oauth-authorization-server").json()
        for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            self.assertIn(k, meta)
        prm = c.get(self.base + "/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(prm["authorization_servers"], [self.base + "/"])

        # 2. Dynamic client registration (public client, like ChatGPT).
        redirect_uri = "https://chatgpt.com/connector/oauth/testcb"
        dcr = c.post(meta["registration_endpoint"], json={
            "client_name": "ChatGPT Test", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"}).json()
        client_id = dcr["client_id"]

        # 3. /authorize with PKCE -> redirect into the consent page.
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        r = c.get(meta["authorization_endpoint"], params={
            "response_type": "code", "client_id": client_id,
            "redirect_uri": redirect_uri, "state": "st4te",
            "code_challenge": challenge, "code_challenge_method": "S256"})
        self.assertIn(r.status_code, (302, 307))
        consent_url = r.headers["location"]
        self.assertIn("/oauth/consent", consent_url)
        self.assertEqual(c.get(consent_url).status_code, 200)

        # 4. Consent: wrong admin key refused; right key mints the code.
        from urllib.parse import parse_qs, urlparse
        q = {k: v[0] for k, v in parse_qs(urlparse(consent_url).query).items()}
        bad = c.post(self.base + "/oauth/consent",
                     data={**q, "aid": "Codex", "admin_key": "wrong"})
        self.assertEqual(bad.status_code, 403)
        ok = c.post(self.base + "/oauth/consent",
                    data={**q, "aid": "Codex", "admin_key": "testkey"})
        self.assertEqual(ok.status_code, 302)
        loc = urlparse(ok.headers["location"])
        self.assertTrue(ok.headers["location"].startswith(redirect_uri))
        cbq = {k: v[0] for k, v in parse_qs(loc.query).items()}
        self.assertEqual(cbq["state"], "st4te")
        code = cbq["code"]

        # 5. Token exchange: wrong verifier rejected, right one mints tokens.
        badtok = c.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": client_id,
            "code_verifier": "not-the-verifier"})
        self.assertEqual(badtok.status_code, 400)
        tok = c.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": client_id,
            "code_verifier": verifier}).json()
        self.assertEqual(tok["token_type"], "Bearer")
        access, refresh = tok["access_token"], tok["refresh_token"]

        # replayed code is dead
        replay = c.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": client_id,
            "code_verifier": verifier})
        self.assertEqual(replay.status_code, 400)

        # 6. The OAuth token authenticates real API calls as Codex.
        me = c.get(self.base + "/v1/checkin",
                   headers={"Authorization": f"Bearer {access}"}).json()
        self.assertEqual(me["aid"], "Codex")

        # 7. Refresh rotation: new pair works, old pair is dead.
        tok2 = c.post(meta["token_endpoint"], data={
            "grant_type": "refresh_token", "refresh_token": refresh,
            "client_id": client_id}).json()
        self.assertNotEqual(tok2["access_token"], access)
        dead = c.get(self.base + "/v1/checkin",
                     headers={"Authorization": f"Bearer {access}"})
        self.assertEqual(dead.status_code, 401)
        live = c.get(self.base + "/v1/checkin",
                     headers={"Authorization": f"Bearer {tok2['access_token']}"})
        self.assertEqual(live.status_code, 200)

        # 8. Revocation kills the connector without touching the static token.
        rv = c.post(self.base + "/revoke", data={
            "token": tok2["access_token"], "client_id": client_id,
            "client_secret": ""})
        self.assertEqual(rv.status_code, 200)
        gone = c.get(self.base + "/v1/checkin",
                     headers={"Authorization": f"Bearer {tok2['access_token']}"})
        self.assertEqual(gone.status_code, 401)
        still = c.get(self.base + "/v1/checkin",
                      headers={"Authorization": f"Bearer {reg['token']}"})
        self.assertEqual(still.status_code, 200)


if __name__ == "__main__":
    unittest.main()
