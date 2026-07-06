"""Tests for one-command web onboarding: GET /connect.sh writes a repo's
.mcp.json + .claude/skills/moot/SKILL.md, and the served skill is the single
source of truth (the bundled plugin's SKILL.md must stay byte-identical)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from moot import skillfile

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_SKILL = _ROOT / "plugin" / "skills" / "moot" / "SKILL.md"

_GIT_ENV = {**os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), env=_GIT_ENV,
                          capture_output=True, text=True)


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


class TestConnectGit(unittest.TestCase):
    """connect.sh commits + pushes the wiring so a fresh cloud clone has it —
    including the non-fast-forward case (remote moved ahead) that must rebase."""

    def test_commits_rebases_and_pushes_when_remote_is_ahead(self):
        if not shutil.which("git"):
            self.skipTest("git not available")
        base = Path(tempfile.mkdtemp())
        remote = base / "remote.git"
        _git(["init", "--bare", "-b", "main", str(remote)], base)
        # seed main from a first clone
        w1 = base / "w1"
        _git(["clone", str(remote), str(w1)], base)
        (w1 / "README").write_text("x")
        _git(["add", "README"], w1); _git(["commit", "-m", "init"], w1)
        _git(["push", "origin", "main"], w1)
        # a second clone pushes an extra commit -> remote is now AHEAD of w1
        w2 = base / "w2"
        _git(["clone", str(remote), str(w2)], base)
        (w2 / "other.txt").write_text("remote work")
        _git(["add", "other.txt"], w2); _git(["commit", "-m", "remote ahead"], w2)
        _git(["push", "origin", "main"], w2)
        # run connect.sh in the now-behind w1 (this is exactly Jeldon's case)
        r = subprocess.run(["sh"], input=skillfile.connect_sh("https://moot.fly.dev"),
                           text=True, capture_output=True, cwd=str(w1), env=_GIT_ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        # committed the moot wiring...
        self.assertIn("Wire up the Moot", _git(["log", "--oneline"], w1).stdout)
        # ...rebased in the remote's ahead commit (no clobber, no lost work)...
        self.assertTrue((w1 / "other.txt").exists(),
                        "pull --rebase should bring the remote's work in")
        # ...and pushed: a brand-new clone gets the moot files.
        w3 = base / "w3"
        _git(["clone", str(remote), str(w3)], base)
        self.assertTrue((w3 / ".mcp.json").exists(),
                        "push should land .mcp.json on the remote")
        self.assertTrue((w3 / ".claude/skills/moot/SKILL.md").exists())

    def test_skips_git_when_opted_out(self):
        if not shutil.which("git"):
            self.skipTest("git not available")
        base = Path(tempfile.mkdtemp())
        _git(["init", "-b", "main", str(base)], base)
        env = {**_GIT_ENV, "MOOT_NO_GIT": "1"}
        r = subprocess.run(["sh"], input=skillfile.connect_sh("https://moot.fly.dev"),
                           text=True, capture_output=True, cwd=str(base), env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((base / ".mcp.json").exists())          # files still written
        self.assertEqual(_git(["log", "--oneline"], base).stdout.strip(), "",
                         "MOOT_NO_GIT=1 must not create any commit")


if __name__ == "__main__":
    unittest.main()
