import json
from pathlib import Path
import tempfile
import unittest

from moneyball.store import Store
from moneyball import draft_advisor_watch as advisor
from tests.test_draft_advisor_watch import scene


class PortabilityTests(unittest.TestCase):
    def test_unique_alias_preserves_existing_private_configuration_without_default(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            with self.assertRaises(ValueError): store.resolve_alias()
            store.record('my-old-alias', {'league':{}}, {})
            self.assertEqual(store.resolve_alias(), 'my-old-alias')
            (Path(temp)/'config.json').write_text(json.dumps({'leagues':{'another':{}}}))
            with self.assertRaisesRegex(ValueError, 'explicitly'): store.resolve_alias()
            self.assertEqual(store.resolve_alias('my-old-alias'), 'my-old-alias')

    def test_real_advisory_requires_explicit_bound_room_not_a_fixed_whitelist(self):
        setup = scene()[0]
        setup['mode'] = 'real_advisory'
        setup['draft']['draft_id'] = '70001'
        setup['draft']['league_id'] = '70002'
        with self.assertRaisesRegex(ValueError, 'room_binding'): advisor.validate_setup(setup)
        setup['room_binding'] = {'draft_id':'70001','league_id':'70002','own_roster_id':7,
                                 'observed_at':setup['setup_observed_at']}
        advisor.validate_setup(setup)
        for field, value in [('draft_id','70003'), ('league_id','70003'), ('own_roster_id',8)]:
            old = setup['room_binding'][field]; setup['room_binding'][field] = value
            with self.assertRaisesRegex(ValueError, 'room_binding'): advisor.validate_setup(setup)
            setup['room_binding'][field] = old


if __name__ == '__main__': unittest.main()
