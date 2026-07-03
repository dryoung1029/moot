"""Live end-to-end test: boot the real hub as a subprocess and drive it over
MCP (streamable HTTP) plus the dashboard API. Skips cleanly if the server can't
start in this environment.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _txt(res):
    for c in res.content:
        t = getattr(c, "text", None)
        if t is not None:
            try:
                return json.loads(t)
            except Exception:
                return t
    return res.structuredContent


class TestLiveHub(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.data = tempfile.mkdtemp(prefix="moothttp-")
        env = dict(os.environ,
                   MOOT_DATA_DIR=cls.data, MOOT_ADMIN_KEY="testkey",
                   MOOT_HOST="127.0.0.1", MOOT_PORT=str(cls.port))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "moot.server"], env=env, cwd=root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Wait for the hub to answer its open health probe.
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

    def _call(self, headers, tool, **args):
        async def go():
            async with streamablehttp_client(self.base + "/mcp", headers=headers) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    res = await s.call_tool(tool, args)
                    return _txt(res), res.isError
        return anyio.run(go)

    def test_full_flow(self):
        reg, err = self._call({}, "moot_register", purpose="write code",
                              specialty="backend", proposed_name="Codey")
        self.assertFalse(err)
        token = reg["token"]
        h = {"Authorization": "Bearer " + token}

        # bad token rejected
        _, berr = self._call({"Authorization": "Bearer bad"}, "moot_whoami")
        self.assertTrue(berr)

        who, _ = self._call(h, "moot_whoami")
        self.assertEqual(who["identity"]["aid"], "Codey")

        post, _ = self._call(h, "moot_post", channel="skunkworks", body="hello moot")
        self.assertIn("post_id", post)

        ci, _ = self._call(h, "moot_checkin", since_post=0)
        self.assertIn("check_in_policy", ci)

        # dashboard reads now require the admin key
        with self.assertRaises(urllib.error.HTTPError) as cm0:
            urllib.request.urlopen(self.base + "/api/overview")
        self.assertEqual(cm0.exception.code, 401)
        req_ov = urllib.request.Request(self.base + "/api/overview",
                                        headers={"X-Moot-Admin": "testkey"})
        ov = json.loads(urllib.request.urlopen(req_ov).read())
        self.assertIn("Codey", [a["aid"] for a in ov["roster"]])
        # the HTML shell and health probe stay open
        html = urllib.request.urlopen(self.base + "/").read().decode()
        self.assertIn("The Moot", html)
        health = json.loads(urllib.request.urlopen(self.base + "/healthz").read())
        self.assertEqual(health["status"], "ok")

        # admin write works; unauth blocked
        req = urllib.request.Request(
            self.base + "/api/act", method="POST",
            headers={"Content-Type": "application/json", "X-Moot-Admin": "testkey"},
            data=json.dumps({"action": "broadcast", "body": "Prime here"}).encode())
        self.assertTrue(json.loads(urllib.request.urlopen(req).read())["ok"])

        # Prime can create a task from the dashboard, and it shows in overview
        req = urllib.request.Request(
            self.base + "/api/act", method="POST",
            headers={"Content-Type": "application/json", "X-Moot-Admin": "testkey"},
            data=json.dumps({"action": "task_add", "title": "kick off portal",
                             "assignee": "Codey", "channel": "proj-training"}).encode())
        tid = json.loads(urllib.request.urlopen(req).read())["result"]["task_id"]
        req_ov2 = urllib.request.Request(self.base + "/api/overview",
                                         headers={"X-Moot-Admin": "testkey"})
        ov2 = json.loads(urllib.request.urlopen(req_ov2).read())
        self.assertTrue(any(t["id"] == tid and t["assignee"] == "Codey"
                            for t in ov2["tasks"]))

        with self.assertRaises(urllib.error.HTTPError) as cm:
            bad = urllib.request.Request(
                self.base + "/api/act", method="POST",
                headers={"Content-Type": "application/json"},
                data=json.dumps({"action": "broadcast", "body": "x"}).encode())
            urllib.request.urlopen(bad)
        self.assertEqual(cm.exception.code, 401)

    def test_rest_bridge(self):
        def post_json(path, payload, tok=None):
            headers = {"Content-Type": "application/json"}
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
            req = urllib.request.Request(self.base + path, method="POST",
                                         headers=headers,
                                         data=json.dumps(payload).encode())
            return json.loads(urllib.request.urlopen(req).read())

        def get_json(path, tok=None):
            headers = {"Authorization": f"Bearer {tok}"} if tok else {}
            req = urllib.request.Request(self.base + path, headers=headers)
            return json.loads(urllib.request.urlopen(req).read())

        # spec + help + skill files are open
        spec = get_json("/v1/openapi.json")
        self.assertEqual(spec["info"]["title"], "The Moot")
        skill = urllib.request.urlopen(self.base + "/skill.md").read().decode()
        self.assertIn("moot", skill.lower())
        self.assertIn("/heartbeat.md", skill)
        hb = urllib.request.urlopen(self.base + "/heartbeat.md").read().decode()
        self.assertIn("Check in", hb)
        # register a REST-only agent
        reg = post_json("/v1/register", {"purpose": "automate flows",
                                         "proposed_name": "Restly",
                                         "specialty": "integrations"})
        tok = reg["token"]
        self.assertEqual(reg["aid"], "Restly")
        # unauthenticated call rejected
        with self.assertRaises(urllib.error.HTTPError) as cm:
            get_json("/v1/checkin")
        self.assertEqual(cm.exception.code, 401)
        # first checkin carries orientation + suggested actions
        ci = get_json("/v1/checkin", tok)
        self.assertIn("orientation", ci)
        self.assertIn("suggested_actions", ci)
        # post, task, search round-trip
        p = post_json("/v1/post", {"channel": "proj-rest", "body": "hello from REST"},
                      tok)
        self.assertIn("post_id", p)
        t = post_json("/v1/tasks", {"title": "wire the webhook",
                                    "channel": "proj-rest"}, tok)
        self.assertIn("task_id", t)
        hits = get_json("/v1/search?q=webhook", tok)["results"]
        self.assertTrue(hits)


if __name__ == "__main__":
    unittest.main()
