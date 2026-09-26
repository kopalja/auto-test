"""Small SQLite persistence: repository checkpoints, runs, and GitHub reports."""
import json
import sqlite3
import time

# Report statuses. A report is keyed by `key` (base finding/blocker identity plus generation).
ACTIVE = ('pending', 'uncertain', 'update', 'published', 'revalidate', 'prepared')
DUE = ('pending', 'uncertain', 'update')
UNSUCCESSFUL = ('blocked', 'incomplete', 'interrupted')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS checkpoints(
  repo TEXT PRIMARY KEY, sha TEXT NOT NULL, run_id TEXT NOT NULL, completed REAL NOT NULL,
  mode TEXT NOT NULL DEFAULT 'source');
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, repo TEXT NOT NULL, sha TEXT NOT NULL, forced INTEGER NOT NULL,
  status TEXT NOT NULL, started REAL NOT NULL, finished REAL, agents TEXT NOT NULL,
  directory TEXT NOT NULL, summary TEXT, error TEXT,
  cleanup TEXT NOT NULL DEFAULT 'none', cleanup_attempts INTEGER NOT NULL DEFAULT 0,
  pruned INTEGER NOT NULL DEFAULT 0, deployment TEXT);
CREATE TABLE IF NOT EXISTS reports(
  key TEXT PRIMARY KEY, base_key TEXT NOT NULL, generation INTEGER NOT NULL, kind TEXT NOT NULL,
  repo TEXT NOT NULL, target TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
  data TEXT NOT NULL, status TEXT NOT NULL, url TEXT, number INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0, error TEXT,
  run_id TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS reports_base ON reports(base_key, generation);
'''


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('PRAGMA journal_mode=WAL;' + SCHEMA)
        # Old checkpoints have no reliable mode once their artifacts are pruned. Recheck once.
        for table, column, definition in (('checkpoints', 'mode', "TEXT NOT NULL DEFAULT 'unknown'"),
                                           ('runs', 'deployment', 'TEXT')):
            if column not in {r['name'] for r in self.db.execute(f'PRAGMA table_info({table})')}:
                self._write(f'ALTER TABLE {table} ADD COLUMN {column} {definition}', ())

    def close(self):
        self.db.close()

    def _write(self, sql, args=()):
        with self.db:
            self.db.execute(sql, args)

    # Checkpoints -------------------------------------------------------------------------
    def checkpoint(self, repo):
        return self.db.execute('SELECT * FROM checkpoints WHERE repo=?', (repo,)).fetchone()

    def set_checkpoint(self, repo, sha, run_id, mode='source'):
        self._write('INSERT OR REPLACE INTO checkpoints(repo,sha,run_id,completed,mode) VALUES(?,?,?,?,?)',
                    (repo, sha, run_id, time.time(), mode))

    def save_deployment(self, run_id, record):
        self._write('UPDATE runs SET deployment=? WHERE id=?', (json.dumps(record), run_id))

    def arm_deployment_cleanup(self, run_id):
        self._write("UPDATE runs SET cleanup='pending',cleanup_attempts=0 WHERE id=?", (run_id,))

    # Runs --------------------------------------------------------------------------------
    def start_run(self, run_id, repo, sha, forced, agents, directory):
        self._write('INSERT INTO runs(id,repo,sha,forced,status,started,agents,directory) '
                    'VALUES(?,?,?,?,?,?,?,?)',
                    (run_id, repo, sha, int(forced), 'running', time.time(), json.dumps(agents), str(directory)))

    def finish_run(self, run_id, status, summary, error=None):
        self._write('UPDATE runs SET status=?,summary=?,error=?,finished=? WHERE id=?',
                    (status, summary, error, time.time(), run_id))

    def set_cleanup(self, run_id, status, attempted=False):
        self._write('UPDATE runs SET cleanup=?,cleanup_attempts=cleanup_attempts+? WHERE id=?',
                    (status, int(attempted), run_id))

    def run(self, run_id):
        return self.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()

    def failures(self, repo, sha):
        return self.db.execute(f'SELECT COUNT(*) FROM runs WHERE repo=? AND sha=? AND status IN {UNSUCCESSFUL}',
                               (repo, sha)).fetchone()[0]

    def mark_interrupted(self):
        """Runs left 'running' while we hold the lock were cut off by a crash or kill."""
        rows = self.db.execute("SELECT * FROM runs WHERE status='running'").fetchall()
        for row in rows:
            self.finish_run(row['id'], 'interrupted', 'Runner stopped before the run finished',
                            'interrupted')
        return rows

    def cleanup_due(self, max_attempts):
        return self.db.execute("SELECT * FROM runs WHERE cleanup='pending' AND cleanup_attempts<?",
                               (max_attempts,)).fetchall()

    def latest_runs(self):
        return self.db.execute('SELECT r.* FROM runs r WHERE r.started=(SELECT MAX(started) FROM runs '
                               'WHERE repo=r.repo) ORDER BY r.repo').fetchall()

    def cleanup_problems(self):
        return self.db.execute("SELECT * FROM runs WHERE cleanup IN ('pending','failed') ORDER BY started").fetchall()

    def prunable(self, cutoff):
        return self.db.execute("SELECT * FROM runs WHERE pruned=0 AND status!='running' AND finished<? "
                               "AND cleanup NOT IN ('pending','failed')", (cutoff,)).fetchall()

    def mark_pruned(self, run_id):
        self._write('UPDATE runs SET pruned=1,deployment=NULL WHERE id=?', (run_id,))

    # Reports -----------------------------------------------------------------------------
    def report(self, key):
        return self.db.execute('SELECT * FROM reports WHERE key=?', (key,)).fetchone()

    def latest_report(self, base_key):
        return self.db.execute('SELECT * FROM reports WHERE base_key=? ORDER BY generation DESC LIMIT 1',
                               (base_key,)).fetchone()

    def save_report(self, key, base_key, generation, kind, repo, target, title, body, data, status, run_id):
        now = time.time()
        with self.db:
            self.db.execute(
                'INSERT INTO reports(key,base_key,generation,kind,repo,target,title,body,data,status,run_id,'
                'first_seen,last_seen,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET '
                'kind=excluded.kind,title=excluded.title,body=excluded.body,data=excluded.data,'
                'status=excluded.status,run_id=excluded.run_id,last_seen=excluded.last_seen,'
                'updated=excluded.updated,'
                "attempts=CASE WHEN reports.status IN ('pending','uncertain','update') THEN reports.attempts ELSE 0 END,"
                "next_retry=CASE WHEN reports.status IN ('pending','uncertain','update') THEN reports.next_retry ELSE 0 END,"
                "error=CASE WHEN reports.status IN ('pending','uncertain','update') THEN reports.error ELSE NULL END",
                (key, base_key, generation, kind, repo, target, title, body, json.dumps(data), status, run_id,
                 now, now, now))

    def touch_report(self, key):
        self._write('UPDATE reports SET last_seen=? WHERE key=?', (time.time(), key))

    def update_report(self, key, **fields):
        fields['updated'] = time.time()
        self._write(f'UPDATE reports SET {",".join(k + "=?" for k in fields)} WHERE key=?',
                    (*fields.values(), key))

    def reports(self, statuses, repo=None, kind=None):
        sql = f'SELECT * FROM reports WHERE status IN ({",".join("?" * len(statuses))})'
        args = list(statuses)
        if repo is not None:
            sql += ' AND repo=?'
            args.append(repo)
        if kind is not None:
            sql += ' AND kind=?'
            args.append(kind)
        return self.db.execute(sql + ' ORDER BY first_seen', args).fetchall()

    def due_reports(self):
        return [r for r in self.reports(DUE) if r['next_retry'] <= time.time()]
