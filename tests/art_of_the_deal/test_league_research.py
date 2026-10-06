"""Research lifecycle through connection and real decision packets, offline."""
import copy
import tempfile
import time
import unittest
from unittest.mock import patch

from art_of_the_deal.service import Service
from art_of_the_deal.store import DataError
from test_service import league, forecasts, proposal


def report(plan):
    return {
        "research_key": plan["research_key"],
        "summary": "Use constrained deployment and explicit outside options; no universal positional premium.",
        "sources": [{"id": "assignment", "title": "Synthetic inspected reference",
                     "url": "https://example.test/assignment", "access": "excerpt",
                     "inspected_scope": "Fixture only, no real source reading claimed",
                     "checked_at": time.time()}],
        "mechanisms": [{"id": "deployment", "domain": "allocation", "status": "candidate",
                        "structural_match": "One resource per eligible service slot",
                        "transfer": "Compare full assignments with actual displaced service",
                        "rule_paths": ["/rules/starters"], "source_ids": ["assignment"],
                        "assumptions": ["Eligibility is known"],
                        "failure_conditions": ["Corrected lock rule changes feasibility"],
                        "evidence_needed": ["Coherent scoring forecasts"],
                        "test": {"comparison": "Joint assignment versus additive ranking",
                                 "observable_outcome": "Different legal starter choices in a FLEX fixture"}}],
        "policies": {op: {"reasoning": "Compare deployable service and the best feasible alternative",
                          "mechanism_ids": ["deployment"], "evidence_needed": ["Fresh relevant inputs"],
                          "reversal_conditions": ["Changed deployability or outside option"]}
                     for op in ("draft", "lineup", "waiver", "trade")},
        "rejected_transfers": [{"mechanism": "Mean variance roster rule",
                                "reason": "Title utility need not penalize variance",
                                "revisit_when": "User's objective establishes the matching utility"}],
        "research_gaps": ["Forecast merit unmeasured"],
    }


class LeagueResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Service(self.tmp.name)
        self.data, _ = league(position_limits={})
        self.data.update(platform="sleeper", league_id="100")

    def connect(self):
        with patch.object(self.service, "_fetch", return_value=(self.data, [], [])):
            return self.service.connect("x", "sleeper", "100", 2026, own_team_id="a")

    def test_connection_prepares_research_without_claiming_completion(self):
        connected = self.connect()
        research = connected["research"]
        self.assertEqual(research["status"], "pending_agent_research")
        stored = self.service.store.get("research_request", research["research_key"])["data"]
        self.assertEqual(stored["league_inputs"]["rules"], self.data["rules"])
        self.assertIn("moneyball://research/trades", stored["method_resources"])
        self.assertTrue(any("equally feasible future management" in step for step in stored["workflow"]))
        self.assertFalse(self.service.store.keys("league_research"))

    def test_saved_policies_reach_actual_trade_packet_and_survive_roster_refresh(self):
        self.connect()
        plan = self.service.research_plan("x")
        self.service.save_research("x", report(plan))
        self.service.store.put("forecasts", "x:test", forecasts(
            self.data, {"aq": 10, "ar": 8, "bq": 10, "br": 12}))
        packet = self.service.trade_packet("x", proposal(), fresh=False)
        self.assertEqual(packet["strategy"]["research"]["status"], "saved_agent_research")
        self.assertIn("outside option", packet["strategy"]["research"]["policies"]["trade"]["reversal_conditions"][0])
        changed = copy.deepcopy(self.data)
        changed["week"] = 2
        changed["teams"]["a"]["record"] = {"wins": 1}
        self.service._publish("x", changed, [], [])
        self.assertEqual(self.service.research_plan("x")["research_key"], plan["research_key"])
        self.assertEqual(self.service.context("x")["research"]["status"], "saved_agent_research")

    def test_rules_change_invalidates_research_and_rejects_stale_submission(self):
        self.connect()
        plan = self.service.research_plan("x")
        saved = report(plan)
        self.service.save_research("x", saved)
        changed = copy.deepcopy(self.data)
        changed["rules"]["bench_slots"] = 1
        self.service._publish("x", changed, [], [])
        self.assertEqual(self.service.context("x")["research"]["status"], "pending_agent_research")
        with self.assertRaises(DataError):
            self.service.save_research("x", saved)
        self.assertEqual(len(self.service.store.keys("league_research")), 1)

    def test_historical_view_excludes_later_research(self):
        self.connect()
        cutoff = time.time()
        self.service.save_research("x", report(self.service.research_plan("x")))
        self.assertEqual(self.service.strategy("x", as_of=cutoff)["research"]["status"], "pending_agent_research")
        self.assertEqual(self.service.strategy("x")["research"]["status"], "saved_agent_research")

    def test_method_upgrade_preserves_historical_research(self):
        self.connect()
        old_plan = self.service.research_plan("x")
        self.service.save_research("x", report(old_plan))
        cutoff = time.time()
        with patch("art_of_the_deal.league_research.METHOD_VERSION", "future-method"):
            self.assertEqual(self.service.context("x")["research"]["status"], "pending_agent_research")
            historical = self.service.research_plan("x", as_of=cutoff)
            self.assertEqual(historical["research_key"], old_plan["research_key"])
            self.assertEqual(historical["progress"]["status"], "saved_agent_research")
            self.assertEqual(self.service.strategy("x", as_of=cutoff, expanded=True)["research_report"]["summary"], report(old_plan)["summary"])

    def test_new_season_and_team_binding_do_not_reuse_prior_report(self):
        self.connect()
        plan = self.service.research_plan("x")
        self.service.save_research("x", report(plan))
        for field, value in (("season", 2027), ("own_team_id", "b")):
            changed = copy.deepcopy(self.data)
            changed[field] = value
            self.service._publish("x", changed, [], [])
            self.assertNotEqual(self.service.research_plan("x")["research_key"], plan["research_key"])
            self.assertEqual(self.service.context("x")["research"]["status"], "pending_agent_research")

    def test_incomplete_or_unbound_evidence_cannot_be_saved(self):
        self.connect()
        plan = self.service.research_plan("x")
        mutations = [
            lambda r: r["sources"][0].update(access="unverified"),
            lambda r: r["sources"][0].update(checked_at=time.time() + 3600),
            lambda r: r["mechanisms"][0].update(rule_paths=["/rules/invented"]),
            lambda r: r["mechanisms"][0].update(source_ids=["invented"]),
            lambda r: r["mechanisms"][0].update(status="contradicted"),
            lambda r: r["policies"].pop("waiver"),
        ]
        for mutate in mutations:
            bad = report(plan)
            mutate(bad)
            with self.subTest(report=bad):
                with self.assertRaises(DataError):
                    self.service.save_research("x", bad)
        self.assertFalse(self.service.store.keys("league_research"))

    def test_rule_evidence_comes_from_observed_inputs_not_authored_report(self):
        self.connect()
        plan = self.service.research_plan("x")
        authored = report(plan)
        authored["mechanisms"][0]["rule_evidence"] = [{"path": "/rules/starters", "value": "invented"}]
        self.service.save_research("x", authored)
        full = self.service.strategy("x", expanded=True)["research_report"]
        self.assertEqual(full["mechanisms"][0]["rule_evidence"][0]["value"], self.data["rules"]["starters"])


if __name__ == "__main__":
    unittest.main()
