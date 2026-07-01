"""Tests for the persona system: three-axis assignment, distinctness across a
fleet, the drift log, and in-place migration of a pre-persona database."""
import unittest

from moot import actions, db, identity
from tests._util import fresh_store


def _reg(name, specialty="generalist", purpose="work"):
    return actions.register(purpose=purpose, specialty=specialty,
                            proposed_name=name, history=None, origin="test")


class TestPersona(unittest.TestCase):
    def setUp(self):
        fresh_store()

    def test_registration_rolls_all_three_axes(self):
        a = _reg("Codey")["agent"]
        self.assertIn(a["quirk"], identity.QUIRKS)
        self.assertIn(a["temperament"], identity.TEMPERAMENTS)
        self.assertIn(a["muse"], identity.MUSES)

    def test_fleet_gets_distinct_personas_on_every_axis(self):
        names = ["Codey", "Doc", "Carol", "Jeldon", "Nova"]
        agents = [_reg(n)["agent"] for n in names]
        for axis in ("quirk", "temperament", "muse"):
            values = [a[axis] for a in agents]
            self.assertEqual(len(set(values)), len(names),
                             f"{axis} values collided: {values}")

    def test_drift_log_roundtrip(self):
        _reg("Codey")
        db.add_drift("Codey", "started favoring worked examples")
        db.add_drift("Codey", "developed a taste for terse commit messages")
        log = db.list_drift("Codey")
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0]["note"], "developed a taste for terse commit messages")

    def test_persona_is_searchable(self):
        a = _reg("Codey")["agent"]
        # A distinctive word from the temperament line should find the agent.
        word = a["temperament"].split("—")[0].replace("The", "").strip()
        hits = db.search(word, kinds=["agent"])
        self.assertTrue(any(h["ref_id"] == "Codey" for h in hits))

    def test_migration_backfills_pre_persona_agents(self):
        # Simulate an agent registered under v0.1.0: no temperament/muse.
        _reg("Codey")
        with db.tx() as conn:
            conn.execute(
                "UPDATE agents SET temperament=NULL, muse=NULL WHERE aid='Codey'")
        db.init_db()  # boot-time migration path
        a = db.get_agent("Codey")
        self.assertIn(a["temperament"], identity.TEMPERAMENTS)
        self.assertIn(a["muse"], identity.MUSES)
        # Quirk from the original registration is preserved.
        self.assertIn(a["quirk"], identity.QUIRKS)


if __name__ == "__main__":
    unittest.main()
