import json
import unittest

from moneyball.professional_tables import (
    draftsharks_adp, draftsharks_news, draftsharks_rankings, fftoday, number, provenance,
)


RECEIPT = {'url': 'https://example.test/table', 'known_at': '2026-09-08T16:00:00+00:00',
           'status': 200, 'sha256': 'fixture'}


def qb_table(values=None):
    values = values or ['300', '450', '3,750', '24', '10', '100', '500', '8']
    headers = ['Chg', 'Player', 'Tm', 'Bye', 'Cmp', 'Att', 'Yds', 'TD', 'INT', 'Att', 'Yds', 'TD', 'FPts']
    cells = ['', '<a href="/stats/players/42/Example_QB?LeagueID=1">Example QB</a>', 'BUF', '7'] + values + ['340.0']
    return ('Projections: 2026 Regular Season, Updated: 9/6/2026'
            '<tr class="tableclmhdr">' + ''.join('<td>' + h + '</td>' for h in headers) + '</tr>'
            '<tr>' + ''.join('<td>' + c + '</td>' for c in cells) + '</tr>')


class ProfessionalTablesTests(unittest.TestCase):
    def test_components_not_rank_utility_and_omissions_not_zero(self):
        r = fftoday(qb_table(), 'QB', RECEIPT)[0]
        self.assertEqual(r['stats']['pass_yd'], 3750)
        self.assertEqual(r['stats']['rush_att'], 100)
        self.assertNotIn('fum_lost', r['stats'])
        self.assertIsNone(r['uncertainty'])
        self.assertEqual(r['source_stated_updated'], '2026-09-06')
        self.assertEqual(r['known_at'], RECEIPT['known_at'])

    def test_bad_headers_wrong_season_and_shifted_row_fail(self):
        for text in (qb_table().replace('Cmp', 'NewColumn'), qb_table().replace('Projections: 2026', 'Projections: 2025'),
                     qb_table().replace('<td>300</td>', '')):
            with self.assertRaises(ValueError):
                fftoday(text, 'QB', RECEIPT)

    def test_impossible_completions_and_duplicate_rows_fail(self):
        with self.assertRaises(ValueError):
            fftoday(qb_table(['451', '450', '3750', '24', '10', '100', '500', '8']), 'QB', RECEIPT)
        with self.assertRaises(ValueError):
            fftoday(qb_table() + qb_table(), 'QB', RECEIPT)

    def test_number_and_receipt_validation(self):
        for n in ('nan', 'inf', '-1', '20001'):
            with self.assertRaises(ValueError): number(n)
        with self.assertRaises(ValueError): provenance({**RECEIPT, 'known_at': '2026-09-08T16:00:00'})
        with self.assertRaises(ValueError): provenance({**RECEIPT, 'status': 403})

    def test_platform_adp_stays_distinct_and_preserves_null_rank(self):
        obj = {'availability': [{'key': 'a', 'source': 'cbs', 'scoring': 'ppr', 'superflex': 0, 'size': 12, 'type': ''}],
               'seed': {'players': {'42': {'fn': 'Example', 'ln': 'QB', 'tm': 'BUF', 'pos': 'QB'}},
                        'adpSets': {'a': [{'id': 42, 'pick': 32.5, 'posAdp': 2, 'dsRank': None}]}}}
        prefix = '<title>2026 ADP</title>var vueAppData = '
        r = draftsharks_adp(prefix + json.dumps(obj), RECEIPT)[0]
        self.assertEqual(r['adp'], 32.5)
        self.assertEqual(r['originating_platform'], 'cbs')
        self.assertIsNone(r['ds_rank_overlay_reference'])
        self.assertIsNone(r['sample_size'])
        self.assertNotIn('mean', r)
        obj['seed']['adpSets']['a'] *= 2
        with self.assertRaises(ValueError): draftsharks_adp(prefix + json.dumps(obj), RECEIPT)

    def test_rank_preview_retains_median_label_and_literal_metrics(self):
        config = {'leagueType': 'Redraft', 'pprScoring': 'ppr', 'superflex': False, 'selectedScoringConfig': {}}
        s = ('<title>2026 ranks</title><script>var appData = ' + json.dumps(config) + '</script>'
             '<th data-attribute="fantasy_points" data-ds-tooltip="Median projection"></th>'
             '<tr class="player-row"><div class="rank-index"><span>1</span></div>'
             '<player-name first-name="Example" last-name="QB" player-id="42"></player-name>'
             '<span class="player-details-group__team-name">BUF</span><pos-roster-spot pos-roster-spot="QB"></pos-roster-spot>'
             '<td data-value="300" data-attribute="fantasy_points"></td></tr>')
        r = draftsharks_rankings(s, RECEIPT, 'QB')[0]
        self.assertEqual(r['source_field_definitions']['fantasy_points'], 'Median projection')
        self.assertEqual(r['raw_reference_metrics']['fantasy_points'], '300')
        self.assertFalse(r['format']['superflex'])
        self.assertNotIn('mean', r)
        with self.assertRaises(ValueError): draftsharks_rankings(s.replace('2026 ranks', '2025 ranks'), RECEIPT, 'QB')

    def test_future_news_is_not_accepted_and_no_probability_manufactured(self):
        s = ('<article data-id="1"><h2><a href="/news/1">Practice update</a></h2>'
             '<time datetime="2026-09-08T15:00:00Z"></time><div class="article-summary">Coach report.</div>'
             '<div class="player-detail-card" data-pid="42"><p class="name" data-name="Example QB"></p>'
             '<span data-team="BUF"></span></div></article>')
        r = draftsharks_news(s, RECEIPT)[0]
        self.assertEqual(r['players'][0]['source_player_id'], '42')
        self.assertIsNone(r['model_adjustment'])
        self.assertEqual(draftsharks_news(s.replace('<time datetime=', '<time data-utc-time="2026-09-08 15:00:00" datetime='), RECEIPT)[0]['article_id'], '1')
        with self.assertRaises(ValueError):
            draftsharks_news(s.replace('15:00:00Z', '17:00:00Z'), RECEIPT)


if __name__ == '__main__':
    unittest.main()
