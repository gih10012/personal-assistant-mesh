"""Actual production journal SQL: retain interrupted runs and bounded results.

This does not attest a device callback, Asset, a model or HAP installation.
"""
from pathlib import Path
import re
import sqlite3
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'apps/harmonyos/entry/src/main/ets/maintenance/MaintenanceJournal.ets'


class MaintenanceJournalTests(unittest.TestCase):
    def setUp(self):
        sql = re.findall(r"executeSql\('((?:\\.|[^'\\])*)'", SOURCE.read_text())
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute(next(value for value in sql if value.startswith('CREATE TABLE')))
        self.begin = next(value for value in sql if value.startswith('INSERT INTO'))
        self.finish = next(value for value in sql if value.startswith('UPDATE '))
        self.prune = next(value for value in sql if value.startswith('DELETE '))

    def test_interrupted_unknown_retained_across_completed_observation_pruning(self):
        self.db.execute(self.begin, ['interrupted-original', 'system_work_scheduler', 1, 'running_or_interrupted_unknown'])
        for index in range(150):
            identity = 'completed-' + str(index)
            self.db.execute(self.begin, [identity, 'system_work_scheduler', index + 10, 'running_or_interrupted_unknown'])
            self.db.execute(self.finish, [index + 11, 'check_finished', 0, 3, 0, 0, identity])
            self.db.execute(self.prune)
        self.assertEqual((0, 'running_or_interrupted_unknown'), self.db.execute(
            "SELECT ended,outcome FROM maintenance_runs WHERE id='interrupted-original'").fetchone())
        self.assertEqual(128, self.db.execute('SELECT COUNT(*) FROM maintenance_runs WHERE ended>0').fetchone()[0])

    def test_stopped_or_unconfirmed_observation_cannot_be_relabelled_as_success(self):
        for identity, outcome, stopped in [('original-stop', 'stopped', 1), ('original-error', 'unconfirmed_old_data_retained', 0)]:
            self.db.execute(self.begin, [identity, 'system_work_scheduler', 100, 'running_or_interrupted_unknown'])
            self.db.execute(self.finish, [101, outcome, stopped, 7, 1, -1, identity])
            before = self.db.execute('SELECT * FROM maintenance_runs WHERE id=?', [identity]).fetchone()
            self.db.execute(self.finish, [102, 'check_finished', 0, 8, 0, 0, identity])
            self.assertEqual(before, self.db.execute('SELECT * FROM maintenance_runs WHERE id=?', [identity]).fetchone())
