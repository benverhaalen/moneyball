import unittest

from art_of_the_deal.markets import (
    decimal_odds, evidence_bundle, fetch_kalshi, market_sources,
    normalize_kalshi_market, normalize_two_sided,
)
from art_of_the_deal.store import DataError


OBSERVED = "2040-09-10T12:00:00Z"
EXPIRY = "2040-09-13T20:00:00Z"


def generic(**changes):
    outcomes = [
        {"side": "over", "odds": -110, "odds_format": "american", "event_id": "game-1",
         "scope_id": "player-a:receiving_yards:64.5", "observed_at": OBSERVED},
        {"side": "under", "odds": 1.91, "odds_format": "decimal", "event_id": "game-1",
         "scope_id": "player-a:receiving_yards:64.5", "observed_at": OBSERVED},
    ]
    args = {
        "source": {"provider": "fixture-book", "market_id": "m1", "url": "https://example.test/m1",
                   "terms_url": "https://example.test/terms"},
        "event": {"id": "game-1", "label": "A at B"},
        "scope": {"id": "player-a:receiving_yards:64.5", "kind": "player_prop",
                  "metric": "receiving_yards", "line": 64.5,
                  "settlement": "official receiving yards", "void": "void if game cancelled",
                  "availability": "book injury/participation terms apply"},
        "outcomes": outcomes, "observed_at": OBSERVED, "expires_at": EXPIRY,
        "horizon": {"kind": "single_event", "event_id": "game-1", "starts_at": EXPIRY}, "raw": {"fixture": True},
    }
    args.update(changes)
    return normalize_two_sided(**args)


class MarketEvidenceTests(unittest.TestCase):
    def test_public_fetch_is_registry_bounded_and_returns_receipts_and_compact_records(self):
        market = {
            "ticker": "KXNFLPASSYDS-GAME-PLAYER-200", "event_ticker": "KXNFLPASSYDS-GAME",
            "title": "Player: 200+ passing yards", "yes_sub_title": "Player: 200+",
            "strike_type": "greater", "floor_strike": 199.5, "yes_ask_dollars": "0.45",
            "no_ask_dollars": "0.58", "expected_expiration_time": EXPIRY,
            "rules_primary": "Resolves using official passing yards.", "rules_secondary": "Participation rules apply.",
        }
        class FakeClient:
            def __init__(self): self.calls = []
            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                receipt = {"url": url, "checked_at": 2231006400.0, "http_status": 200}
                if "/series/" in url:
                    return {"series": {"ticker": "KXNFLPASSYDS", "title": "Passing Yards"}}, receipt
                return {"markets": [market], "cursor": ""}, receipt
        client = FakeClient()
        result = fetch_kalshi(client, "KXNFLPASSYDS", 1)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result["limits"], {"requested": 1, "returned": 1, "normalized": 1})
        self.assertEqual(result["records"][0]["raw"]["container"], "raw.markets")
        self.assertEqual(len(result["receipts"]), 2)
        self.assertIn("KXNFLPASSYDS", {row["series_ticker"] for row in market_sources()})

    def test_public_fetch_rejects_unverified_tickers_and_large_or_boolean_limits(self):
        for ticker in ("KXNFLPASSYDS?x=1", "kxNFL", "UNVERIFIED"):
            with self.assertRaises(DataError):
                fetch_kalshi(None, ticker)
        for limit in (0, 51, True):
            with self.assertRaises(DataError):
                fetch_kalshi(None, "KXNFLPASSYDS", limit)

    def test_converts_american_and_decimal_and_labels_approximation(self):
        self.assertAlmostEqual(decimal_odds(-110, "american"), 1.909090909)
        self.assertEqual(decimal_odds(2.5, "decimal"), 2.5)
        row = generic()
        self.assertEqual(row["probability"]["method"], "proportional_no_vig_approximation")
        self.assertAlmostEqual(sum(row["probability"]["values"].values()), 1.0)
        self.assertFalse(row["probability"]["fees_included"])

    def test_line_is_not_recast_as_mean_or_fantasy_projection(self):
        row = generic()
        self.assertEqual(row["scope"]["line"], 64.5)
        self.assertFalse(row["constraints"]["line_is_mean"])
        self.assertIsNone(row["constraints"]["fantasy_points_projection"])
        self.assertIsNone(row["constraints"]["causal_player_share"])

    def test_rejects_mismatched_event_scope_or_timestamp(self):
        for field, bad in (("event_id", "game-2"), ("scope_id", "another-prop"),
                           ("observed_at", "2040-09-10T12:01:00Z")):
            args = [
                {"side": "yes", "odds": 2, "odds_format": "decimal", "event_id": "game-1",
                 "scope_id": "scope-1", "observed_at": OBSERVED},
                {"side": "no", "odds": 2, "odds_format": "decimal", "event_id": "game-1",
                 "scope_id": "scope-1", "observed_at": OBSERVED},
            ]
            args[1][field] = bad
            with self.assertRaises(DataError):
                normalize_two_sided(source={"provider": "x", "market_id": "m", "url": "https://x.test"},
                                    event={"id": "game-1"},
                                    scope={"id": "scope-1", "kind": "binary", "settlement": None,
                                           "void": None, "availability": None}, outcomes=args,
                                    observed_at=OBSERVED, expires_at=EXPIRY,
                                    horizon={"kind": "single_event", "event_id": "game-1"})

    def test_kalshi_public_shape_uses_both_asks_and_preserves_contract_conditions(self):
        market = {
            "ticker": "KXTEST-PLAYER-70", "event_ticker": "KXTEST-GAME", "title": "Player: 70+ receiving yards",
            "yes_sub_title": "Player: 70+", "strike_type": "greater", "floor_strike": 69.5,
            "cap_strike": None, "yes_ask_dollars": "0.4400", "no_ask_dollars": "0.5900",
            "expected_expiration_time": EXPIRY, "occurrence_datetime": "2040-09-13T17:00:00Z",
            "rules_primary": "Resolves Yes at 70+ official receiving yards.",
            "rules_secondary": "Participation and correction conditions apply.",
        }
        series = {"title": "Receiving Yards", "contract_terms_url": "https://example.test/contract.pdf",
                  "settlement_sources": [{"name": "league", "url": "https://example.test/league"}]}
        row = normalize_kalshi_market(market, observed_at=OBSERVED, series=series)
        self.assertAlmostEqual(row["outcomes"]["yes"]["raw_implied_probability"], 0.44)
        self.assertAlmostEqual(row["outcomes"]["no"]["raw_implied_probability"], 0.59)
        self.assertAlmostEqual(row["probability"]["raw_implied_total"], 1.03)
        self.assertEqual(row["scope"]["availability"], market["rules_secondary"])
        self.assertEqual(row["source"]["terms_url"], series["contract_terms_url"])

    def test_missing_or_expired_scope_remains_missing_not_zero(self):
        row = generic()
        bundle = evidence_bundle([row], requested_scope_ids=[row["scope"]["id"], "missing-stat"],
                                 as_of="2040-09-11T00:00:00Z")
        self.assertEqual(bundle["missing_scope_ids"], ["missing-stat"])
        self.assertIsNone(bundle["combined_probability"])
        self.assertIsNone(bundle["fantasy_points_projection"])
        expired = evidence_bundle([row], requested_scope_ids=[row["scope"]["id"]],
                                  as_of="2040-09-14T00:00:00Z")
        self.assertEqual(expired["evidence"], [])
        self.assertEqual(expired["missing_scope_ids"], [row["scope"]["id"]])

    def test_multiple_books_or_alternate_lines_are_retained_without_pooling(self):
        a = generic()
        b = generic(source={"provider": "fixture-book-2", "market_id": "m2", "url": "https://example.test/m2"})
        bundle = evidence_bundle([a, b], requested_scope_ids=[a["scope"]["id"]],
                                 as_of="2040-09-11T00:00:00Z")
        self.assertEqual(len(bundle["evidence"]), 2)
        self.assertIsNone(bundle["combined_probability"])
        self.assertIn("not treated as independent", bundle["dependence"])

    def test_bad_or_one_sided_odds_fail_loudly(self):
        for odds, fmt in ((1, "decimal"), (50, "american"), (2, "fractional")):
            with self.assertRaises(DataError):
                decimal_odds(odds, fmt)
        with self.assertRaises(DataError):
            generic(outcomes=[{"side": "over", "odds": 2, "odds_format": "decimal",
                               "event_id": "game-1", "scope_id": "player-a:receiving_yards:64.5",
                               "observed_at": OBSERVED}])
        with self.assertRaises(DataError):
            generic(observed_at=None)


if __name__ == "__main__":
    unittest.main()
