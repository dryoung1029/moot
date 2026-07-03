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


_MD_BEHAVIOR = r"""
const cases = [
  ["**bold** and *ital* and `code`", ["<b>bold</b>", "<i>ital</i>", '<code class="mdcode">code</code>']],
  ["- one\n- two\n1. first", ["<ul><li>one</li><li>two</li></ul>", "<ol><li>first</li></ol>"]],
  ["```\nlet x = 1 < 2;\n```", ['<pre class="mdpre"><code>let x = 1 &lt; 2;</code></pre>']],
  ["see [docs](https://x.io/a) or https://y.io", ['href="https://x.io/a"', 'href="https://y.io"']],
  ["<img src=x onerror=alert(1)>", ["&lt;img src=x onerror=alert(1)&gt;"]],
  ["> quoted line", ["<blockquote>quoted line</blockquote>"]],
  ["## Heading", ['<div class="mdh">Heading</div>']],
  ["ping @Codey!", ['<span class="mention">@Codey</span>']],
  ["line1\nline2", ["line1<br/>line2"]],
];
let fail = 0;
for(const [input, wants] of cases){
  const out = md(input);
  for(const w of wants) if(!out.includes(w)){ console.error("FAIL: "+JSON.stringify(input)+" -> "+out); fail++; }
}
if(md("[bad](javascript:alert(1))").includes("<a ")){ console.error("FAIL: javascript: url got linked"); fail++; }
if(md("<script>alert(1)</scr"+"ipt>").includes("<script")){ console.error("FAIL: script injection"); fail++; }
process.exit(fail ? 1 : 0);
"""


class TestDashboardJs(unittest.TestCase):
    def test_markdown_renderer_safe_and_correct(self):
        """Exercise the dashboard's md() in a real JS runtime: formatting works
        and — critically — agent-written text can never become markup."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        js = _scripts()
        esc_part = re.search(r"(function esc.*?)\nfunction when", js, re.S)
        md_part = re.search(r"(/\* Minimal safe Markdown.*?)\n\nasync function api",
                            js, re.S)
        self.assertTrue(esc_part and md_part, "esc()/md() not found in dashboard JS")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(esc_part.group(1) + "\n" + md_part.group(1) + _MD_BEHAVIOR)
            path = f.name
        r = subprocess.run([node, path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"md() behavior failures:\n{r.stderr}")

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
