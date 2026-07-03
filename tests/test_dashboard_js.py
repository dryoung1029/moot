"""Guard: the dashboard's inline JavaScript must be syntactically valid.

A syntax error in the dashboard script (e.g. a duplicate `const`) is fatal — it
kills the entire page, including the unlock flow — but is invisible to the Python
test suite because the script is just a string. This test extracts it and checks
it with a real JS parser (node) when available, and with a scoped-duplicate-
declaration heuristic otherwise.
"""
import re
import shutil
import subprocess
import tempfile
import unittest

from moot.web import _HTML


def _scripts() -> str:
    html = _HTML.replace("__MOOT_VERSION__", "0.0.0")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "dashboard has no <script> block"
    return "\n".join(blocks)


class TestDashboardJs(unittest.TestCase):
    def test_script_parses(self):
        js = _scripts()
        node = shutil.which("node")
        if node:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(js)
                path = f.name
            r = subprocess.run([node, "--check", path],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0,
                             f"dashboard JS failed node --check:\n{r.stderr}")
        else:
            # Heuristic fallback: no top-level (function-body) name declared
            # twice with const/let. Catches the exact `dms` collision class.
            import collections
            names = re.findall(r"\b(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=", js)
            dupes = {n: c for n, c in collections.Counter(names).items() if c > 1}
            self.assertEqual(dupes, {},
                             f"duplicate const/let declarations: {dupes}")


if __name__ == "__main__":
    unittest.main()
