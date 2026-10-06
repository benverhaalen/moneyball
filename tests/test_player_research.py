import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from moneyball.player_research import verified_packet, compact_market


class PlayerResearchTests(unittest.TestCase):
    def test_actual_depth_is_retained_with_newer_book(self):
        row = {'source_id': 'example', 'market_id': 'm', 'target': {}, 'question': 'Q',
               'observed_at': 1, 'market_url': 'old', 'raw_id': 1,
               'orderbook': {'top': {'yes_bid': .6, 'yes_ask': .7}, 'best_bid_size': 1.08,
                            'best_ask_size': 103., 'size_sensitivity': {'100': {'yes_bid': .3, 'yes_ask': .7}},
                            'observed_at': 2, 'url': 'new', 'raw_id': 2}}
        result = compact_market(row)
        self.assertEqual(result['sizes']['yes_bid'], 1.08)
        self.assertEqual(result['depth_100_share_sensitivity']['yes_bid'], .3)
        self.assertEqual(result['observed_at'], 2)

    def test_lookup_rejects_corrupt_or_wrong_identity_packet(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            data = json.dumps({'player_id': '1'}).encode()
            (directory/'p.json').write_bytes(data)
            entry = {'player_id': '1', 'path': 'p.json', 'sha256': hashlib.sha256(data).hexdigest()}
            self.assertEqual(verified_packet(directory, entry)['player_id'], '1')
            entry['player_id'] = '2'
            with self.assertRaises(ValueError):
                verified_packet(directory, entry)
            entry['player_id'], entry['sha256'] = '1', 'bad'
            with self.assertRaises(ValueError):
                verified_packet(directory, entry)


if __name__ == '__main__':
    unittest.main()
