import sqlite3
import tempfile
import unittest
from moneyball.dossier_swarm import Swarm
from moneyball.dossier_scouts import Scouts


class QueueConnectionLifetimeTests(unittest.TestCase):
    def test_connections_close_after_commit_and_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            for queue in (Swarm(root), Scouts(root)):
                with queue.db() as db:
                    db.execute("CREATE TABLE lifetime_probe (value INTEGER)")
                    db.execute("INSERT INTO lifetime_probe VALUES (1)")
                with self.assertRaises(sqlite3.ProgrammingError):
                    db.execute("SELECT 1")
                with self.assertRaises(RuntimeError):
                    with queue.db() as failed:
                        failed.execute("INSERT INTO lifetime_probe VALUES (2)")
                        raise RuntimeError("Rollback this write")
                with self.assertRaises(sqlite3.ProgrammingError):
                    failed.execute("SELECT 1")
                with queue.db() as check:
                    self.assertEqual(check.execute("SELECT value FROM lifetime_probe").fetchall()[0][0], 1)
                    self.assertEqual(check.execute("SELECT COUNT(*) FROM lifetime_probe").fetchone()[0], 1)
