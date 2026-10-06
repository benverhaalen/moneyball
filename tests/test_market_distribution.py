"""Synthetic fixtures using the inspected vendor example schema; not real props."""
import copy
import unittest

from moneyball.market_distribution import (MarketDataError, devig, fit_curve,
    game_total_coverage, implied_probability, isotonic, normalize_event, quantile_bounds)


def event(lines=None):
    lines = lines or [(49.5, 2.0, 2.0)]
    return {'id': 'SYNTHETIC_GAME', 'commence_time': '2026-09-10T00:00:00Z',
            'bookmakers': [{'key': 'SYNTHETIC_BOOK', 'last_update': '2026-09-07T17:00:00Z',
                'markets': [{'key': 'player_reception_yds', 'outcomes': [
                    {'name': side, 'description': 'SYNTHETIC_PLAYER', 'point': line,
                     'price': price} for line, over, under in lines
                    for side, price in [('Over', over), ('Under', under)]]}]}]}


def normalized(data=None):
    return normalize_event(data or event(), observed_at='2026-09-07T18:00:00Z',
                           market_keys=['player_reception_yds'])


class MarketDistributionTests(unittest.TestCase):
    def test_odds_formats_and_balanced_devig(self):
        self.assertAlmostEqual(implied_probability(-110, 'american'), 110/210)
        self.assertAlmostEqual(implied_probability(200, 'american'), 1/3)
        for method in ('multiplicative', 'additive', 'power'):
            r = devig(-110, -110, 'american', method)
            self.assertAlmostEqual(r['under'], .5)
            self.assertAlmostEqual(r['overround'], 1/21)

    def test_asymmetric_margin_methods_are_not_identified_truth(self):
        results = [devig(1.25, 4, method=m)['over']
                   for m in ('multiplicative', 'additive', 'power')]
        self.assertGreater(max(results) - min(results), .01)
        self.assertTrue(all(0 < x < 1 for x in results))

    def test_prices_fail_loudly(self):
        for value in (None, True, float('nan'), 1, -1):
            with self.assertRaises(MarketDataError):
                implied_probability(value)

    def test_missing_one_side_or_push_rejected(self):
        data = event()
        data['bookmakers'][0]['markets'][0]['outcomes'].pop()
        with self.assertRaises(MarketDataError): normalized(data)
        with self.assertRaises(MarketDataError): normalized(event([(50, 2, 2)]))

    def test_observed_after_cutoff_rejected(self):
        with self.assertRaises(MarketDataError):
            normalize_event(event(), observed_at='2026-09-07T18:00:00Z',
                cutoff='2026-09-07T17:00:00Z', market_keys=['player_reception_yds'])

    def test_future_quote_and_missing_time_rejected(self):
        for quote in ('2026-09-07T19:00:00Z', None, '2026-09-07T17:00:00'):
            data = event();data['bookmakers'][0]['last_update'] = quote
            with self.assertRaises(MarketDataError): normalized(data)

    def test_duplicate_or_cross_book_rejected(self):
        data = event();data['bookmakers'].append(copy.deepcopy(data['bookmakers'][0]))
        with self.assertRaises(MarketDataError): normalized(data)
        rows = normalized(event([(19.5, 1.25, 5), (79.5, 5, 1.25)]))
        rows[1]['book'] = 'other'
        with self.assertRaises(MarketDataError): fit_curve(rows, support=(0, 100))

    def test_single_median_cannot_identify_mean(self):
        curve = fit_curve(normalized(), support=(0, 100))
        self.assertEqual(curve['mean_identification_bounds'], [25, 74.5])
        self.assertIsNone(curve['confidence_interval'])
        self.assertIsNone(curve['independent_sample_size'])
        self.assertEqual(quantile_bounds(curve, .1), [0, 49])
        self.assertEqual(quantile_bounds(curve, .9), [50, 100])

    def test_more_thresholds_narrow_bounds_and_include_known_mean(self):
        curve = fit_curve(normalized(event([(19.5, 1.25, 5), (49.5, 2, 2),
                                            (79.5, 5, 1.25)])), support=(0, 100))
        a, b = curve['mean_identification_bounds']
        self.assertLess(b-a, 49.5)
        self.assertLess(a, 49.5);self.assertGreater(b, 49.5)

    def test_pava_and_repair_is_explicit_and_bounded(self):
        self.assertEqual(isotonic([.1, .6, .4, .9]), [.1, .5, .5, .9])
        rows = normalized(event([(19.5, 1/.4, 1/.6), (49.5, 1/.5, 1/.5)]))
        with self.assertRaises(MarketDataError): fit_curve(rows, support=(0, 100))
        curve = fit_curve(rows, support=(0, 100), repair=True, max_adjustment=.051)
        self.assertAlmostEqual(curve['max_isotonic_adjustment'], .05)
        with self.assertRaises(MarketDataError):
            fit_curve(rows, support=(0, 100), repair=True, max_adjustment=.01)

    def test_support_is_explicit_finite_and_consistent(self):
        for support in ((0, float('inf')), (50, 100), (100, 0), (.5, 100)):
            with self.assertRaises(MarketDataError): fit_curve(normalized(), support=support)

    def test_quote_span_not_sample_size(self):
        rows = normalized(event([(19.5, 1.25, 5), (79.5, 5, 1.25)]))
        rows[1]['quoted_at'] = '2026-09-07T17:20:00Z'
        with self.assertRaises(MarketDataError): fit_curve(rows, support=(0, 100))

    def test_retrospective_calibration_does_not_read_evaluation_residuals(self):
        rows = [{'season': y, 'week': i+1, 'game_id': f'{y}_{i}', 'game_type': 'REG',
                 'home_score': 20, 'away_score': 20, 'total': 40, 'total_line': 40-r}
                for y in range(2018, 2026) for i, r in enumerate((-5, 0, 5, 10))]
        first = game_total_coverage(rows, bootstrap_draws=40)
        rows[-1].update(home_score=120, total=140)
        changed = game_total_coverage(rows, bootstrap_draws=40)
        self.assertEqual(first['calibration_n'], 24)
        self.assertEqual(first['evaluations'][0]['residual_interval'], [-5, 10])
        self.assertEqual(first['evaluations'][0]['residual_interval'],
                         changed['evaluations'][0]['residual_interval'])
        self.assertFalse(changed['point_in_time_verified'])
        self.assertLess(changed['evaluations'][1]['observed_coverage'], 1)

    def test_score_reconciliation_prevents_bad_market_validation(self):
        rows = [{'season': 2020, 'week': 1, 'game_id': 'x', 'game_type': 'REG',
                 'home_score': 20, 'away_score': 20, 'total': 41, 'total_line': 40}]
        with self.assertRaises(MarketDataError): game_total_coverage(rows)


if __name__ == '__main__':
    unittest.main()
