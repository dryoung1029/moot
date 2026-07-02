"""Tests for the persona system: three-axis assignment, distinctness across a
fleet, the drift log, in-place migration of a pre-persona database, and the
carry-home persona block with its safe-word toggle."""
import unittest

from moot import actions, config, db, identity, persona
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

    def test_persona_block_carries_character_and_safe_word(self):
        a = _reg("Codey")["agent"]
        db.add_drift("Codey", "grew fond of terse commit messages")
        block = persona.render_block(db.get_agent("Codey"))
        self.assertTrue(block.startswith(persona.BEGIN_MARK))
        self.assertTrue(block.endswith(persona.END_MARK))
        self.assertIn(a["temperament"], block)
        self.assertIn(a["muse"], block)
        self.assertIn(config.SAFE_WORD, block)
        self.assertIn(config.WAKE_WORD, block)
        self.assertIn("terse commit messages", block)
        self.assertIn("ON", block)

    def test_persona_mode_toggle_reflected_in_block(self):
        _reg("Codey")
        self.assertEqual(db.persona_mode(), "on")     # default
        self.assertEqual(db.set_persona_mode("off"), "off")
        block = persona.render_block(db.get_agent("Codey"))
        self.assertIn("OFF", block)
        self.assertIn("suspended", block)
        self.assertEqual(db.set_persona_mode("anything-else"), "on")

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
