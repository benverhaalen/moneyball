"""Public onboarding and focused waiver evidence journeys, with synthetic data."""
import copy
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from art_of_the_deal.cli import main
from art_of_the_deal.operations import acquisition_context, _pickup_inputs
from art_of_the_deal.service import Service
from art_of_the_deal.store import DataError
from test_service import league, forecasts


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = Service(self.temp.name)

    def connected(self, own=None, platform='sleeper'):
        d, _ = league(position_limits={})
        d.update(platform=platform, league_id='123', own_team_id=own)
        with patch.object(self.s, '_fetch', return_value=(d, [], [])):
            return self.s.connect('friends', platform, '123', 2026)

    def test_unselected_connection_then_team_choice_changes_research_binding(self):
        pending = self.connected()
        self.assertEqual(pending['onboarding_status'], 'team_selection_required')
        self.assertIsNone(pending['my_roster'])
        first_key = pending['research']['research_key']
        selected = self.s.select_team('friends', 'a')
        self.assertEqual(selected['my_roster']['id'], 'a')
        self.assertNotEqual(selected['research']['research_key'], first_key)
        self.assertEqual(selected['research']['status'], 'pending_agent_research')
        before = self.s.snapshot('friends')['sha256']
        with self.assertRaises(DataError):
            self.s.select_team('friends', 'unknown')
        self.assertEqual(self.s.snapshot('friends')['sha256'], before)

    def test_discovery_does_not_autoconnect_and_passes_selected_user_id(self):
        with patch('art_of_the_deal.sleeper.discover', return_value=[{'league_id':'123','user_id':'456','season':'2026','name':'Friends'}]) as discovery:
            result = self.s.discover_sleeper('sample-user', 2026)
        self.assertEqual(result['leagues'][0]['user_id'], '456')
        self.assertEqual(self.s.leagues(), [])
        discovery.assert_called_once()
        with self.assertRaises(DataError):
            self.s.discover_sleeper('sample-user', True)

    def test_espn_url_and_local_diagnostics_do_not_echo_credentials(self):
        d, _ = league(position_limits={})
        with patch.object(self.s, '_fetch', return_value=(d, [], [])) as fetch:
            self.s.connect('espn-friends','espn','https://fantasy.espn.com/football/team?leagueId=123&teamId=2',2026)
        self.assertEqual(fetch.call_args.args[0]['league_id'], '123')
        with patch.dict(os.environ, {'ESPN_SWID':'SECRET_SWID','ESPN_S2':'SECRET_S2'}):
            status = self.s.onboarding_status()
        self.assertTrue(status['espn_environment_credentials_configured'])
        self.assertNotIn('SECRET', json.dumps(status))
        for url in ('https://other.test/?leagueId=123', 'https://fantasy.espn.com/?leagueId=123&leagueId=456'):
            with self.assertRaises(DataError):
                self.s.connect('bad','espn',url,2026)

    def test_pool_forecast_import_only_accepts_verified_ids_and_is_atomic(self):
        self.connected('a')
        self.s.store.put('pool','friends',{'players':[{'player_id':'candidate'}], 'league_binding':{'platform':'sleeper','league_id':'123','season':2026}})
        d = self.s.snapshot('friends')['data']
        rows = forecasts(d, {'candidate':15}, available_at=time.time())
        self.s.import_forecasts('friends','platform-baseline',rows)
        previous = self.s.store.get('forecasts','friends:platform-baseline')['sha256']
        rows.append({**rows[0], 'player_id':'guessed-name'})
        with self.assertRaises(DataError):
            self.s.import_forecasts('friends','platform-baseline',rows)
        self.assertEqual(self.s.store.get('forecasts','friends:platform-baseline')['sha256'],previous)

    def test_fresh_sleeper_pickup_targets_exact_id_beyond_alphabetical_first_fifty(self):
        self.connected('a')
        catalog = {str(i):{'full_name':f'A Early {i:03d}', 'active':True,'position':'RB'} for i in range(60)}
        catalog['target'] = {'full_name':'Z Desired Candidate','active':True,'position':'RB'}
        self.s.store.put('catalog','sleeper',catalog)
        first = acquisition_context(self.s,'friends',limit=50)
        self.assertNotIn('target', [p['player_id'] for p in first['players']])
        with patch.object(self.s,'refresh',return_value={}):
            d,pool,candidates,*rest = _pickup_inputs(self.s,'friends',['target'],1,True)
        self.assertEqual(candidates['target']['name'],'Z Desired Candidate')
        self.assertEqual([p['player_id'] for p in pool['players']],['target'])
        by_id = acquisition_context(self.s,'friends',query='target')
        self.assertEqual(by_id['players'][0]['player_id'],'target')

    def test_cli_doctor_fresh_install_and_parser_leaf_commands(self):
        with redirect_stdout(io.StringIO()) as stdout:
            main(['--home',self.temp.name,'doctor'])
        self.assertEqual(json.loads(stdout.getvalue())['leagues'],[])
        from art_of_the_deal.cli import parser
        for arguments in (['draft','friends','--cached'], ['lineup','friends','--week','2'], ['pipeline-query','player_stats','--seasons','2025'], ['demo']):
            parser().parse_args(arguments)

    def test_alias_identity_cannot_reuse_forecasts_for_colliding_provider_ids(self):
        self.connected('a')
        original = self.s.snapshot('friends')['sha256']
        # ESPN and Sleeper can use the same numeric string for different players.
        self.s.store.put('forecasts','friends:source',[{'player_id':'123','points':30}])
        with patch.object(self.s,'_fetch') as fetch:
            for platform, lid, season in [('espn','123',2026), ('sleeper','456',2026), ('sleeper','123',2027)]:
                with self.assertRaisesRegex(DataError,'new alias'):
                    self.s.connect('friends',platform,lid,season)
            fetch.assert_not_called()
        self.assertEqual(self.s.snapshot('friends')['sha256'],original)
        self.assertEqual(self.s.store.get('config','friends')['data']['platform'],'sleeper')


if __name__ == '__main__': unittest.main()
