"""Tests for examples/export_rep.py credential resolution — the caller-side
'export your live MOOT_REP to a file' helper. We test the .mcp.json parsing in
isolation (no network): the script must find the token and hub from a local moot
MCP config so a warden/cron can run it with zero arguments."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "export_rep",
    Path(__file__).resolve().parent.parent / "examples" / "export_rep.py")
export_rep = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(export_rep)


class TestExportRepCreds(unittest.TestCase):
    def _write(self, obj) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / ".mcp.json"
        p.write_text(json.dumps(obj))
        return p

    def test_reads_token_and_hub_from_mcp(self):
        p = self._write({"mcpServers": {"moot": {
            "url": "https://moot.fly.dev/mcp",
            "headers": {"Authorization": "Bearer sk-abc123"}}}})
        token, base = export_rep._from_mcp(p)
        self.assertEqual(token, "sk-abc123")
        self.assertEqual(base, "https://moot.fly.dev")  # /mcp stripped

    def test_missing_file_is_graceful(self):
        token, base = export_rep._from_mcp(Path("/nonexistent/.mcp.json"))
        self.assertIsNone(token)
        self.assertIsNone(base)

    def test_config_without_moot_server(self):
        p = self._write({"mcpServers": {"other": {"url": "x"}}})
        token, base = export_rep._from_mcp(p)
        self.assertIsNone(token)
        self.assertIsNone(base)


if __name__ == "__main__":
    unittest.main()
