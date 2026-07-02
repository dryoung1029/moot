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

    def test_reclaim_pre_enrolled_seat(self):
        placeholder = _reg("Doc", "healthcare operations", "placeholder purpose")
        old_token = placeholder["token"]
        # The real Doc arrives, proposing the same name, before any check-in.
        real = actions.register(purpose="run the healthcare business",
                                specialty="musculoskeletal medicine",
                                proposed_name="Doc", history="the real one",
                                origin="claude-code")
        self.assertTrue(real["reclaimed"])
        self.assertEqual(real["agent"]["aid"], "Doc")
        # Persona carried over from the seat; purpose updated; token rotated.
        self.assertEqual(real["agent"]["quirk"], placeholder["agent"]["quirk"])
        self.assertEqual(real["agent"]["purpose"], "run the healthcare business")
        self.assertIsNone(db.get_agent_by_token(old_token))
        self.assertEqual(db.get_agent_by_token(real["token"])["aid"], "Doc")
        # Still exactly one Doc.
        self.assertEqual(sum(1 for a in db.all_aids() if a == "Doc"), 1)

    def test_checkin_locks_a_name_against_reclaim(self):
        first = _reg("Doc")
        db.mark_checkin("Doc")  # the holder has checked in: name is locked
        second = actions.register(purpose="impostor", specialty="healthcare",
                                  proposed_name="Doc", history=None, origin="test")
        self.assertFalse(second["reclaimed"])
        self.assertNotEqual(second["agent"]["aid"], "Doc")
        # Original token untouched.
        self.assertEqual(db.get_agent_by_token(first["token"])["aid"], "Doc")

    def test_rename_moves_history_and_keeps_token(self):
        vitae = _reg("Vitae", "healthcare", "second brain")
        _reg("Codey")
        actions.post("Vitae", "general", "hello from vitae")
        db.add_insight("Codey", "Vitae", "clinical evidence", None)
        self.assertTrue(db.rename_agent("Vitae", "Doc"))
        self.assertIsNone(db.get_agent("Vitae"))
        self.assertEqual(db.get_agent_by_token(vitae["token"])["aid"], "Doc")
        posts = db.channel_posts("general", 0, 50)
        self.assertTrue(any(p["aid"] == "Doc" and "hello from vitae" in p["body"]
                            for p in posts))
        self.assertEqual(db.list_insights("Doc")[0]["teacher"], "Doc")
        # Old name searchable no more; new name findable.
        self.assertFalse([h for h in db.search("Vitae", kinds=["agent"])
                          if h["ref_id"] == "Vitae"])
        # And the old name is free again.
        self.assertTrue(db.rename_agent("Doc", "Vitae"))

    def test_rename_refuses_collisions_and_system_names(self):
        _reg("Codey")
        _reg("Doc")
        self.assertFalse(db.rename_agent("Codey", "Doc"))    # taken
        self.assertFalse(db.rename_agent("Codey", "Bill"))   # reserved
        self.assertFalse(db.rename_agent("Bill", "Robert"))  # system
        self.assertFalse(db.rename_agent("Ghost", "Anything"))

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
