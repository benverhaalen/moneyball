"""Focused source retrieval: temporal ordering, pinned pages and integrity."""
import unittest
from test_pipelines import ConnectedPipelineTests
from moneyball.league import pipelines
from moneyball.league.store import DataError
from moneyball.warehouse import WarehouseError


class PipelineContextTests(ConnectedPipelineTests):
    def test_latest_pages_do_not_repeat_or_admit_later_batches(self):
        self.publish([{"id":str(w),"season":2026,"week":w,"position":"QB"} for w in (1,3,2)])
        first = pipelines.query(self.service,"weekly",seasons=[2026],limit=1,positions=["QB"])
        self.assertEqual(first["rows"][0]["week"],3)
        self.publish([{"id":"new","season":2026,"week":4,"position":"QB"}])
        second = pipelines.query(self.service,"weekly",seasons=[2026],limit=1,positions=["QB"],cursor=first["next_cursor"])
        self.assertEqual(second["rows"][0]["week"],2)
        third = pipelines.query(self.service,"weekly",seasons=[2026],limit=5,positions=["QB"],cursor=second["next_cursor"])
        self.assertEqual([r["week"] for r in third["rows"]],[1])
        self.assertIsNone(third["next_cursor"])
        self.assertEqual(first["evidence_as_of"],third["evidence_as_of"])
        with self.assertRaises(DataError):
            pipelines.query(self.service,"weekly",seasons=[2025],cursor=first["next_cursor"])

    def test_corrupt_record_is_not_accepted_as_evidence(self):
        self.publish([{"id":"x","season":2026}])
        with self.w.connect() as db:
            db.execute("UPDATE records SET row_hash='broken'")
        with self.assertRaises(WarehouseError):
            pipelines.query(self.service,"weekly",seasons=[2026])
