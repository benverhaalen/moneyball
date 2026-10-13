"""Offline end-to-end research and roster decision, with deliberately invented worlds."""
from tempfile import TemporaryDirectory
import time
from .demo import fixture
from .service import Service
from . import workbench


def inputs(league):
    ids = list(league['players'])
    report = {'binding': workbench.binding(league), 'objective': 'Improve future legal lineup capacity while measuring current opportunity cost; title implications remain a host judgment.',
              'player_ids': ids, 'sources': [{'id': 'synthetic', 'origin_group': 'single-invented-input',
                'scope': 'Invented demonstration, not NFL evidence', 'url': 'https://example.invalid/demo', 'observed_at': time.time()}],
              'claims': [dict(id=pid, player_id=pid, facet='opportunity', kind='assumption', use='conditional', confidence='low',
                 text='Supplied role scenario for '+pid, reasoning='Compare whether a roster swap helps under different future roles.',
                 countercase='The player may never receive this role.', reversal_condition='The competing role persists.',
                 horizon='Current and next season', source_ids=['synthetic'], depends_on=[], mechanism_ids=['volume_share']) for pid in ids]}
    values = {'aq': 10, 'ar': 12, 'aw': 8, 'ax': 7, 'bq': 10, 'br': 5, 'bw': 6, 'bx': 11}
    def world(ident, season, changed):
        projected = {**values, **changed}
        return {'id': ident, 'season': season, 'week': 5, 'condition': ident.replace('_', ' '),
                'projections': [dict(player_id=pid, stats={'rush_yd': val*10, 'rec_yd': 0, 'pass_yd': 0},
                   availability=1, drivers=[pid]) for pid,val in projected.items()]}
    # Synthetic components express invented means, not realistic football stat lines.
    spec = {'future_management': 'Frozen holdings and reserve membership; no future waiver, rookie or negotiation policy assumed.',
            'worlds': [world('current', league['season'], {}),
                       world('future_receiver_role_expands', league['season']+1, {'ar':8, 'bx':17}),
                       world('future_receiver_role_fails', league['season']+1, {'ar':12, 'bx':4})],
            'actions': {'hold': {}, 'swap_runner_for_receiver': {'transfers': [
                {'player_id':'ar','from_team':'a','to_team':'b'}, {'player_id':'bx','from_team':'b','to_team':'a'}]}}}
    return report, spec


def seed(service):
    league = fixture()
    league['format'] = 'dynasty'
    service._publish('demo', league, [{'source':'synthetic-only'}], [])
    return service.snapshot('demo')['data']


def run():
    with TemporaryDirectory(prefix='moneyball-assessment-') as home:
        service = Service(home)
        league = seed(service)
        packet = workbench.context(service, 'demo', ['ar','bx'])
        report, specification = inputs(league)
        saved = workbench.save(service,'demo','role-trade',report)
        comparison = workbench.evaluate(service,'demo','role-trade',specification)
        pruned = workbench.evaluate(service,'demo','role-trade',specification,disabled_claims=['bx'])
        return {'mode':'offline_synthetic', 'network_used':False, 'persistent_store_created':False,
                'binding':packet['binding'], 'example_assessment':report, 'example_specification':specification,
                'saved':saved, 'comparison':comparison,
                'pruned_receiver_projection':pruned['worlds'][0]['player_projections']['bx'],
                'interpretation':'The same trade changes direction under different explicit role assumptions. This demonstrates decision accounting, not forecast skill or title probabilities.'}
