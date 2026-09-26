import sqlite3

from helpers import Case
from state import State


class MigrationTest(Case):
    def test_old_database_migrates_checkpoint_mode_once(self):
        path = self.tmp / 'old-state.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE checkpoints(repo TEXT PRIMARY KEY, sha TEXT NOT NULL, '
                       'run_id TEXT NOT NULL, completed REAL NOT NULL)')
            db.execute("INSERT INTO checkpoints VALUES('owner/calc', 'abc', 'old-run', 0)")
            db.execute('CREATE TABLE runs(id TEXT PRIMARY KEY, repo TEXT, sha TEXT)')
        state = State(path)
        self.assertEqual(state.checkpoint('owner/calc')['mode'], 'unknown')
        self.assertIn('deployment', {r['name'] for r in state.db.execute('PRAGMA table_info(runs)')})
        state.set_checkpoint('owner/calc', 'abc', 'new-run', 'deployment')
        state.close()
        state = State(path)
        self.addCleanup(state.close)
        self.assertEqual(state.checkpoint('owner/calc')['mode'], 'deployment')
