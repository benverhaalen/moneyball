import unittest

from moneyball.market_estimates import component_subtotal, source_anchors, variants


class MarketEstimateTests(unittest.TestCase):
    def test_missing_and_zero_components_differ(self):
        result = component_subtotal({'rec': 0, 'rec_yd': 100}, {'rec': 1, 'rec_yd': .1, 'rec_td': 6})
        self.assertEqual(result['subtotal'], 10)
        self.assertEqual(result['terms']['rec'], 0)
        self.assertEqual(result['missing_core_components'], ['rec_td'])
        self.assertFalse(result['complete_league_projection'])

    def test_bad_asof_and_duplicate_identity_fail(self):
        row = {'known_at': '2026-09-08T12:00:00+00:00', 'season': 2026,
               'kind': 'statistical_projection_components', 'player_id': '1'}
        with self.assertRaises(ValueError):
            source_anchors([row], cutoff=0)
        with self.assertRaises(ValueError):
            source_anchors([row, row], cutoff=1900000000)

    def test_count_width_is_not_continuous_cv(self):
        self.assertEqual({v['family'] for v in variants('rec')}, {'negative_binomial'})
        self.assertEqual({v['family'] for v in variants('rec_yd')}, {'gamma', 'lognormal'})
        self.assertEqual(len(list(variants('rec_yd'))), 12)


if __name__ == '__main__':
    unittest.main()
