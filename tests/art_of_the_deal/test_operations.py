import copy
import tempfile
import time
import unittest
from unittest.mock import patch

from art_of_the_deal.operations import claim_scenarios, pickup_packet
from art_of_the_deal.service import Service
from art_of_the_deal.store import digest
from art_of_the_deal.strategy import strategy_profile


SCORING = {"weights": {"fantasy_points": 1.0}, "unsupported": [], "nonlinear": []}


def league():
    data = {
        "platform": "espn",
        "league_id": "123",
        "name": "Synthetic operations",
        "season": 2026,
        "week": 1,
        "format": "redraft",
        "own_team_id": "a",
        "rules": {
            "starters": [
                {"label": "RB", "eligible_positions": ["RB"]},
                {"label": "WR", "eligible_positions": ["WR"]},
                {"label": "FLEX", "eligible_positions": ["RB", "WR"]},
            ],
            "bench_slots": 1,
            "reserve_slots": 0,
            "taxi_slots": 0,
            "position_limits": {},
            "scoring": SCORING,
            "best_ball": False,
            "playoffs": {"teams": 2, "start_week": 15, "round_weeks": [15, 16],
                         "reseed": False, "byes": 0},
            "trade": {"deadline_week": None, "review_days": 0},
            "waivers": {"type": "priority", "budget": 0, "clear_days": 1},
            "keeper": {"enabled": False},
            "raw": {},
        },
        "teams": {
            "a": {"id": "a", "name": "A", "player_ids": ["rb1", "rb2", "wr1", "wr2"],
                  "starters": ["rb1", "wr1", "wr2"], "reserve": [], "taxi": []},
            "b": {"id": "b", "name": "B", "player_ids": [], "starters": [],
                  "reserve": [], "taxi": []},
        },
        "players": {
            "rb1": {"name": "RB One", "positions": ["RB"], "external_ids": {}},
            "rb2": {"name": "RB Two", "positions": ["RB"], "external_ids": {}},
            "wr1": {"name": "WR One", "positions": ["WR"], "external_ids": {}},
            "wr2": {"name": "WR Two", "positions": ["WR"], "external_ids": {}},
        },
        "picks": [],
        "schedule": [],
        "completeness": {},
        "source_receipts": [],
    }
    profile = strategy_profile(data)
    data["strategy_key"] = profile["rules_fingerprint"]
    return data, profile


def forecast_rows(values, *, week, available_at):
    return [{
        "player_id": player_id,
        "source": "synthetic-espn-baseline",
        "season": 2026,
        "period": {"kind": "week", "week": week},
        "points": float(points),
        "scoring_hash": digest(SCORING),
        "available_at": available_at,
        "conditioning": "provider_status_and_opponent_at_cutoff",
        "source_url": "https://example.test/espn-projection",
        "provider_updated_at": None,
    } for player_id, points in values.items()]


def pool(*, status="WAIVERS", week=1, observed_at=None):
    observed_at = time.time() if observed_at is None else observed_at
    players = []
    for player_id, position in (("fa1", "WR"), ("fa2", "RB")):
        players.append({
            "player_id": player_id,
            "name": "Candidate " + player_id,
            "positions": [position],
            "acquisition_status": status,
            "acquisition_route": (
                "provider_status_waivers" if status == "WAIVERS" else "provider_status_freeagent"
            ),
            "execution_actionable": None,
            "waiver_process_at_raw": 1_800_000_000_000,
            "lineup_locked": False,
            "roster_locked": status == "WAIVERS",
            "trade_locked": False,
            "on_team_id": None,
            "projection": None,
        })
    return {
        "source": {"provider": "espn", "url": "https://example.test/current-pool"},
        "observed_at": observed_at,
        "expires_at": None,
        "horizon": {"kind": "week", "season": 2026, "week": week},
        "players": players,
        "waiver_state": {
            "current_order": [
                {"rank": 1, "team_id": "b", "name": "B"},
                {"rank": 2, "team_id": "a", "name": "A"},
            ],
            "own_team_id": "a",
            "own_current_rank": 2,
            "reset_each_week": True,
            "reset_basis": "inverse_standings",
            "next_reset_rank": None,
            "tied_record_ordering": None,
            "claim_success_guaranteed": False,
            "pending_or_competing_claims": "not_exposed_by_this_read",
            "raw": {"private": "omitted by wrapper"},
        },
        "receipts": [],
    }


def future_pool(candidate_points, *, observed_at, status="FREEAGENT"):
    result = pool(status=status, week=2, observed_at=observed_at)
    result["source"]["url"] = "https://example.test/future-week-pool"
    result["scoring_hash"] = digest(SCORING)
    for row in result["players"]:
        row["projection"] = {
            "source": "synthetic-espn-baseline",
            "season": 2026,
            "period": {"kind": "week", "week": 2},
            "points": float(candidate_points[row["player_id"]]),
            "conditioning": "provider_status_and_opponent_at_cutoff",
        }
    return result


class PickupOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name)
        data, profile = league()
        self.data = data
        self.service.store.put("config", "x", {
            "alias": "x", "platform": "espn", "league_id": "123", "season": 2026,
            "own_team_id": "a", "user_id": None, "auth_mode": "none",
        })
        self.service.store.put("strategy", profile["rules_fingerprint"], profile)
        self.service.store.put("league", "x", data)

    def _store_old_chain(self, *, candidate_points=None):
        candidate_points = candidate_points or {"fa1": 14, "fa2": 13}
        observed = time.time()
        self.service.store.put("pool", "x", pool(status="WAIVERS", week=1, observed_at=observed))
        self.service.store.put("forecasts", "x:espn:week2", forecast_rows(
            {"rb1": 12, "rb2": 10, "wr1": 11, "wr2": 8},
            week=2, available_at=observed,
        ))
        self.service.store.put("forecast_pool", "x:2", future_pool(
            candidate_points, observed_at=observed, status="FREEAGENT"
        ))
        return observed

    def test_cached_pickup_and_claim_wrappers_preserve_drop_and_conditional_cost(self):
        self._store_old_chain()
        with patch("art_of_the_deal.operations.Client", side_effect=AssertionError("network client used")), \
             patch("art_of_the_deal.operations.espn_headers", side_effect=AssertionError("auth used")):
            pickup = pickup_packet(
                self.service, "x", "fa1", drop_player_id="wr2", evaluation_week=2,
                fresh=False, expanded=False,
            )
            claims = claim_scenarios(
                self.service,
                "x",
                [
                    {"order": 1, "player_id": "fa1", "drop_player_id": "wr2",
                     "priority_before": 2, "priority_after_if_success": 2},
                    {"order": 2, "player_id": "fa2", "drop_player_id": "rb2",
                     "priority_before": 2, "priority_after_if_success": 2},
                ],
                evaluation_week=2,
                fresh=False,
                expanded=False,
            )

        comparison = pickup["comparison"]
        self.assertTrue(comparison["valid"])
        self.assertTrue(comparison["roster_branch_feasible"])
        self.assertEqual(comparison["drop_player"], [{"id": "wr2", "name": "WR Two"}])
        self.assertEqual(comparison["comparisons"][0]["delta_points"], 4)
        self.assertFalse(pickup["refresh_performed"])
        self.assertEqual(pickup["candidate"]["acquisition_status"], "WAIVERS")
        self.assertEqual(pickup["waiver_state"]["own_current_rank"], 2)
        self.assertNotIn("raw", pickup["waiver_state"])

        self.assertTrue(claims["comparison"]["valid"])
        self.assertEqual(claims["comparison"]["declared_preference_order"], ["fa1", "fa2"])
        first = claims["comparison"]["scenarios"][0]["conditional_on_this_target_being_acquired"]
        self.assertEqual(first["claim_cost"]["priority_before"], 2)
        self.assertFalse(first["platform_transaction_completed"])
        self.assertIsNone(claims["comparison"]["claim_probabilities"])

    def test_as_of_excludes_newer_pool_and_forecast_revisions(self):
        self._store_old_chain(candidate_points={"fa1": 14, "fa2": 13})
        cutoff = time.time()
        time.sleep(0.01)
        newer_observed = time.time()
        self.service.store.put("pool", "x", pool(
            status="FREEAGENT", week=1, observed_at=newer_observed
        ))
        self.service.store.put("forecasts", "x:espn:week2", forecast_rows(
            {"rb1": 12, "rb2": 10, "wr1": 11, "wr2": 30},
            week=2, available_at=newer_observed,
        ))
        self.service.store.put("forecast_pool", "x:2", future_pool(
            {"fa1": 99, "fa2": 99}, observed_at=newer_observed, status="FREEAGENT"
        ))

        with patch("art_of_the_deal.operations.Client", side_effect=AssertionError("network client used")), \
             patch("art_of_the_deal.operations.espn_headers", side_effect=AssertionError("auth used")):
            packet = pickup_packet(
                self.service, "x", "fa1", drop_player_id="wr2", evaluation_week=2,
                fresh=False, expanded=True, as_of=cutoff,
            )

        self.assertEqual(packet["candidate"]["acquisition_status"], "WAIVERS")
        self.assertEqual(packet["evidence_as_of"], cutoff)
        self.assertEqual(packet["decision_at"], cutoff)
        self.assertEqual(packet["comparison"]["result"]["delta_points"], 4)
        self.assertEqual(packet["comparison"]["result"]["after"]["selected"][1]["player_id"], "fa1")

    def test_future_week_freeagent_projection_never_relabels_current_waiver_candidate(self):
        self._store_old_chain()
        packet = pickup_packet(
            self.service, "x", "fa1", drop_player_id="wr2", evaluation_week=2,
            fresh=False, expanded=True,
        )
        self.assertEqual(packet["candidate"]["acquisition_status"], "WAIVERS")
        self.assertTrue(packet["comparison"]["acquisition"]["claim_required"])
        self.assertEqual(packet["comparison"]["acquisition"]["status_at_observation"], "WAIVERS")
        self.assertEqual(packet["comparison"]["result"]["period"], {"kind": "week", "week": 2})
        self.assertFalse(packet["comparison"]["platform_transaction_completed"])


if __name__ == "__main__":
    unittest.main()
