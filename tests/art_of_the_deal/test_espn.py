import copy
import unittest

from art_of_the_deal.espn import normalize, projections
from art_of_the_deal.store import DataError, digest


def fixture():
    player = {
        "id": 101, "fullName": "Fixture Quarterback", "defaultPositionId": 1,
        "eligibleSlots": [0, 7, 20, 21], "injuryStatus": "ACTIVE", "proTeamId": 9,
        "stats": [
            {"seasonId": 2040, "scoringPeriodId": 2, "statSourceId": 1,
             "statSplitTypeId": 1, "appliedTotal": 0.0, "appliedStats": {}},
            {"seasonId": 2040, "scoringPeriodId": 0, "statSourceId": 1,
             "statSplitTypeId": 0, "appliedTotal": 300.0},
            {"seasonId": 2040, "scoringPeriodId": 2, "statSourceId": 0,
             "statSplitTypeId": 1, "appliedTotal": 22.0},
        ],
    }
    return {
        "id": 4242, "seasonId": 2040, "scoringPeriodId": 2,
        "settings": {
            "name": "Fixture League", "size": 1,
            "draftSettings": {"keeperCount": 0, "keeperCountFuture": 0, "keeperOrderType": "TRADITIONAL"},
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2, "4": 2, "6": 1,
                                                         "16": 1, "17": 1, "20": 6, "21": 3,
                                                         "23": 1},
                               "positionLimits": {"1": 4}},
            "scoringSettings": {"scoringType": "H2H_POINTS", "scoringItems": [
                {"statId": 3, "points": 0.04, "pointsOverrides": {}, "isReverseItem": False},
                {"statId": 53, "points": 1.0, "pointsOverrides": {}, "isReverseItem": False},
                {"statId": 95, "points": 0.0, "pointsOverrides": {"16": 0.0}, "isReverseItem": False},
                {"statId": 17, "points": 3.0, "pointsOverrides": {}, "isReverseItem": False},
                {"statId": 999, "points": 7.0, "pointsOverrides": {}, "isReverseItem": False},
            ]},
            "scheduleSettings": {"matchupPeriodCount": 14, "matchupPeriods": {str(x): [x] for x in range(1, 18)},
                                 "playoffTeamCount": 7, "playoffReseed": True},
            "acquisitionSettings": {"acquisitionType": "WAIVERS_TRADITIONAL", "isUsingAcquisitionBudget": False,
                                    "acquisitionBudget": 100, "waiverHours": 48},
            "tradeSettings": {"deadlineDate": 2200000000000, "revisionHours": 24},
        },
        "teams": [{"id": 8, "name": "Synthetic Team", "owners": ["owner-a"], "record": {"overall": {"wins": 0}},
                   "roster": {"entries": [{"playerId": 101, "lineupSlotId": 0,
                                             "playerPoolEntry": {"player": player}}]}}],
        "schedule": [{"id": 1, "matchupPeriodId": 2, "winner": "UNDECIDED",
                      "home": {"teamId": 8, "totalPoints": 0.0, "totalProjectedPoints": 10.0,
                               "pointsByScoringPeriod": {"2": 0.0}}}],
    }


class EspnNormalizerTests(unittest.TestCase):
    def test_normalizes_observed_core_view_shape_and_exact_roster_counts(self):
        league = normalize(fixture(), "4242", 2040, own_team_id=8)
        self.assertEqual(league["format"], "redraft")
        self.assertEqual(league["rules"]["bench_slots"], 6)
        self.assertEqual(league["rules"]["reserve_slots"], 3)
        self.assertEqual(league["rules"]["taxi_slots"], 0)
        self.assertEqual(league["rules"]["waivers"]["budget"], 0.0)
        self.assertEqual(league["rules"]["position_limits"], {"1": 4})
        labels = [slot["label"] for slot in league["rules"]["starters"]]
        for label in ("QB", "RB", "WR", "TE", "FLEX", "K", "DST"):
            self.assertIn(label, labels)
        self.assertEqual(league["teams"]["8"]["starters"], ["101"])
        self.assertEqual(league["players"]["101"]["positions"], ["QB"])
        self.assertEqual(league["schedule"][0]["scores"], {"8": 0.0})

    def test_unknown_cardinality_stays_unknown_instead_of_becoming_zero(self):
        raw = fixture()
        del raw["settings"]["rosterSettings"]["lineupSlotCounts"]["20"]
        league = normalize(raw, 4242, 2040)
        self.assertIsNone(league["rules"]["bench_slots"])

    def test_position_override_including_zero_is_preserved_as_unsupported(self):
        league = normalize(fixture(), 4242, 2040)
        unsupported = league["rules"]["scoring"]["unsupported"]
        self.assertIn({"statId": 95, "points": 0.0, "pointsOverrides": {"16": 0.0},
                       "isReverseItem": False}, unsupported)
        self.assertNotIn("dst_int", league["rules"]["scoring"]["weights"])

    def test_threshold_and_unknown_scoring_are_lossless_explicit_gates(self):
        league = normalize(fixture(), 4242, 2040)
        scoring = league["rules"]["scoring"]
        self.assertEqual(scoring["weights"]["pass_yd"], 0.04)
        self.assertEqual(scoring["weights"]["rec"], 1.0)
        self.assertEqual([row["statId"] for row in scoring["nonlinear"]], [17])
        self.assertEqual([row["statId"] for row in scoring["unsupported"]], [95, 999])
        self.assertIs(scoring["raw"], league["rules"]["raw"]["scoringSettings"])

    def test_playoff_round_weeks_and_byes_are_derived_from_exact_calendar(self):
        playoffs = normalize(fixture(), 4242, 2040)["rules"]["playoffs"]
        self.assertEqual(playoffs["start_week"], 15)
        self.assertEqual(playoffs["round_weeks"], [15, 16, 17])
        self.assertEqual(playoffs["byes"], 1)
        self.assertTrue(playoffs["reseed"])

    def test_extracts_only_weekly_provider_projections_and_keeps_zero(self):
        raw = fixture()
        league = normalize(raw, 4242, 2040)
        rows = projections(raw, league, "2040-01-01T00:00:00Z")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["points"], 0.0)
        self.assertEqual(rows[0]["period"], {"kind": "week", "week": 2})
        self.assertEqual(rows[0]["scoring_hash"], digest(league["rules"]["scoring"]))
        self.assertEqual(rows[0]["conditioning"], "provider_unspecified")

    def test_duplicate_projection_and_identity_mismatch_fail_loudly(self):
        raw = fixture()
        league = normalize(raw, 4242, 2040)
        stat = raw["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]["player"]["stats"][0]
        raw["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]["player"]["stats"].append(copy.deepcopy(stat))
        with self.assertRaises(DataError):
            projections(raw, league, 1)
        with self.assertRaises(DataError):
            normalize(fixture(), 9999, 2040)


if __name__ == "__main__":
    unittest.main()
