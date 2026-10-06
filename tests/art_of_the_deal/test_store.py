from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest

from art_of_the_deal.store import DataError, Store, timestamp


class StoreTests(unittest.TestCase):
    def test_cli_numeric_and_iso_cutoffs_are_equivalent(self):
        self.assertEqual(timestamp("1789363804.5"),1789363804.5)
        self.assertEqual(timestamp("2026-09-14T05:30:04+00:00"),1789363804)
        for value in ("NaN","Infinity","2026-09-14T05:30:04"):
            with self.assertRaises(DataError): timestamp(value)

    def test_boolean_is_never_a_timestamp(self):
        for value in (True, False):
            with self.subTest(value=value), self.assertRaises(DataError):
                timestamp(value)

    def test_concurrent_identical_blob_writes_share_one_verified_object(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            raw = b"same evidence" * 10000
            with ThreadPoolExecutor(max_workers=8) as pool:
                hashes = list(pool.map(store.blob, [raw] * 32))
            self.assertEqual(len(set(hashes)), 1)
            self.assertEqual((store.blobs / hashes[0]).read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
