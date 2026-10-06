"""Regression fixtures use observed public field grammar, with synthetic names.

Actual-response counts and receipts stay in the gitignored research audit. These
tests establish target/quote separation; they do not validate price calibration.
"""
import unittest

from moneyball.market_quotes import PlayerIndex, normalize_record


class MarketQuoteAuditTests(unittest.TestCase):
    def setUp(self):
        self.index = PlayerIndex([{'player_id': 'example', 'name': 'Example Player'}])
        self.receipt = {
            'source_id': 'polymarket_us_public', 'id': 10, 'sha256': 'test',
            'url': 'https://example.test/events/20', 'observed_at': 100,
        }
        self.event = {'id': '20', 'slug': 'example', 'title': '2026 Regular Season Receptions'}
        self.market = {
            'id': '30', 'title': '15.5+ Receptions',
            'description': 'This market will settle to Yes if Example Player records over 15.5 receptions in the 2026 Pro Football regular season.',
            'metadata': {'playerName': 'Example Player'},
            'outcomes': '["No","Yes"]', 'outcomePrices': '["0.4700","0.4800"]',
            'bestBidQuote': {'value': '0.4700', 'currency': 'USD'},
            'bestAskQuote': {'value': '0.4800', 'currency': 'USD'},
            'marketSides': [
                {'description': 'Yes', 'long': True, 'price': '0.4700',
                 'quote': {'value': '0.4800', 'currency': 'USD'}},
                {'description': 'No', 'long': False, 'price': '0.4800',
                 'quote': {'value': '0.5300', 'currency': 'USD'}},
            ],
        }

    def normalize(self):
        return normalize_record('polymarket_us_public', self.market, self.receipt,
                                self.index, event=self.event)

    def test_reversed_outcome_array_does_not_reverse_explicit_yes_bbo(self):
        quote = self.normalize()['quote']
        self.assertEqual(quote['yes_bid'], .47)
        self.assertEqual(quote['yes_ask'], .48)
        self.assertAlmostEqual(quote['midpoint'], .475)

    def test_noncomplementary_outcomeprices_never_get_devigged(self):
        self.market['outcomePrices'] = '["0.0100","0.0200"]'
        quote = self.normalize()['quote']
        self.assertAlmostEqual(quote['midpoint'], .475)
        self.assertIn('not a confidence interval', quote['interpretation'])

    def test_first_to_reach_is_not_an_unconditional_season_threshold(self):
        self.event['title'] = 'First quarterback to reach 2,000 passing yards'
        self.market['description'] = ('This resolves Yes if Example Player is the first quarterback '
            'to reach 2,000 passing yards during the 2026 regular season.')
        row = self.normalize()
        self.assertNotEqual(row['target']['kind'], 'player_stat_threshold')
        self.assertFalse(row['threshold_analysis_eligible'])

    def test_at_least_once_in_a_game_is_not_a_season_total(self):
        self.event['title'] = '400+ Passing Yards in a Game'
        self.market['title'] = 'Will Example Player have a 400+ passing yard game?'
        self.market['description'] = ('This resolves Yes if Example Player records 400 or more '
            'passing yards in any single game during the 2026 regular season.')
        row = self.normalize()
        self.assertEqual(row['target']['horizon'], 'single_week_or_game')
        self.assertFalse(row['threshold_analysis_eligible'])

    def test_game_total_subject_cannot_enter_player_season_analysis(self):
        self.event['title'] = 'Example City vs. Another City'
        self.market['title'] = 'Total Passing Yards'
        self.market['metadata'] = None
        self.market['description'] = ('Total Passing Yards settles Yes if Example City vs. Another City '
            'records more than 500.5 passing yards in the full game of the professional '
            'football game scheduled for Sep 14, 2026.')
        row = self.normalize()
        self.assertIsNone(row['player_id'])
        self.assertFalse(row['threshold_analysis_eligible'])

    def test_cancellation_contingency_stays_attached_to_threshold_quote(self):
        self.market['description'] += ' If the season is cancelled this resolves 50-50.'
        row = self.normalize()
        self.assertTrue(row['settlement']['cancel_50_50'])
        self.assertFalse(row['settlement']['fee_adjustment_applied'])
        self.assertIn('not a confidence interval', row['quote']['interpretation'])


if __name__ == '__main__':
    unittest.main()
