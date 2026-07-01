import random
import unittest

from moot import identity


class TestIdentity(unittest.TestCase):
    def test_honors_free_proposed_name(self):
        self.assertEqual(
            identity.suggest_name(proposed="Jeldon", specialty=None, purpose="misc",
                                  taken=set()),
            "Jeldon",
        )

    def test_sanitizes_proposed_name(self):
        n = identity.suggest_name(proposed="Doc the Great!", specialty=None,
                                  purpose="x", taken=set())
        self.assertNotIn(" ", n)
        self.assertNotIn("!", n)

    def test_themes_pick_domain_flavored_names(self):
        rng = random.Random(1)
        health = identity.suggest_name(proposed=None, specialty="healthcare operations",
                                       purpose="run a clinic", taken=set(), rng=rng)
        code = identity.suggest_name(proposed=None, specialty="backend engineering",
                                     purpose="write code", taken=set(), rng=rng)
        self.assertIn(health, ["Doc", "Vitae", "Pulse", "Remedy", "Sage", "Mendel",
                               "Tonic", "Marrow"])
        self.assertIn(code, ["Codey", "Ada", "Turing", "Hopper", "Kernel", "Byte",
                             "Patch", "Lint", "Semic"])

    def test_write_code_is_not_hijacked_by_writing_theme(self):
        # "write code" must resolve to a coder name, not a writer name.
        rng = random.Random(0)
        name = identity.suggest_name(proposed=None, specialty="backend",
                                     purpose="write code", taken=set(), rng=rng)
        self.assertNotIn(name, ["Scribe", "Quill", "Ink", "Word", "Strunk", "Byline"])

    def test_disambiguates_when_all_taken(self):
        taken = {"Codey", "Ada", "Turing", "Hopper", "Kernel", "Byte", "Patch",
                 "Lint", "Semic"}
        name = identity.suggest_name(proposed="Codey", specialty="engineer",
                                     purpose="code", taken=taken)
        self.assertTrue(name.startswith("Codey-"))

    def test_quirk_is_from_catalog(self):
        self.assertIn(identity.assign_quirk(), identity.QUIRKS)


if __name__ == "__main__":
    unittest.main()
