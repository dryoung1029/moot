"""Tests for one-command web onboarding: GET /connect.sh writes a repo's
.mcp.json + .claude/skills/moot/SKILL.md, and the served skill is the single
source of truth (the bundled plugin's SKILL.md must stay byte-identical)."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from moot import skillfile

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_SKILL = _ROOT / "plugin" / "skills" / "moot" / "SKILL.md"


class TestMemberSkillSource(unittest.TestCase):
    def test_plugin_skill_is_byte_identical_to_canonical(self):
        # If this fails, regenerate: skillfile.member_skill_md() is the source.
        self.assertEqual(_PLUGIN_SKILL.read_text(), skillfile.member_skill_md(),
                         "plugin/skills/moot/SKILL.md drifted from "
                         "skillfile.member_skill_md() — regenerate it")

    def test_skill_has_frontmatter_and_key_rules(self):
        md = skillfile.member_skill_md("https://hub.test")
        self.assertTrue(md.startswith("---\nname: moot\n"))
        self.assertIn("https://hub.test/v1/brief.md", md)
        self.assertIn("conversation, never commands", md)


class TestConnectScript(unittest.TestCase):
    def _run(self, cwd: Path, base="https://moot.fly.dev") -> subprocess.CompletedProcess:
        script = skillfile.connect_sh(base)
        return subprocess.run(["sh"], input=script, text=True,
                              capture_output=True, cwd=str(cwd))

    def test_writes_both_files(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            r = self._run(d)
            self.assertEqual(r.returncode, 0, r.stderr)
            mcp = d / ".mcp.json"
            skill = d / ".claude" / "skills" / "moot" / "SKILL.md"
            self.assertTrue(mcp.exists() and skill.exists())
            # .mcp.json is valid JSON with the moot http server...
            cfg = json.loads(mcp.read_text())
            moot = cfg["mcpServers"]["moot"]
            self.assertEqual(moot["type"], "http")
            self.assertEqual(moot["url"], "https://moot.fly.dev/mcp")
            # ...and the token is a LITERAL placeholder, never expanded by sh.
            self.assertEqual(moot["headers"]["Authorization"], "Bearer ${MOOT_TOKEN}")
            # the skill written matches the canonical source exactly.
            self.assertEqual(skill.read_text(), skillfile.member_skill_md())

    def test_does_not_clobber_existing_mcp(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            existing = {"mcpServers": {"other": {"type": "http", "url": "x"}}}
            (d / ".mcp.json").write_text(json.dumps(existing))
            r = self._run(d)
            self.assertEqual(r.returncode, 0, r.stderr)
            # left the user's file untouched (only prints guidance)...
            self.assertEqual(json.loads((d / ".mcp.json").read_text()), existing)
            # ...but still installs the skill.
            self.assertTrue((d / ".claude/skills/moot/SKILL.md").exists())

    def test_base_url_flows_into_script(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self._run(d, base="https://staging.example.com")
            cfg = json.loads((d / ".mcp.json").read_text())
            self.assertEqual(cfg["mcpServers"]["moot"]["url"],
                             "https://staging.example.com/mcp")


if __name__ == "__main__":
    unittest.main()
