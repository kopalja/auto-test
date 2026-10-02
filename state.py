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
  pruned INTEGER NOT NULL DEFAULT 0, deployment TEXT, execution_mode TEXT);
CREATE TABLE IF NOT EXISTS reports(
  key TEXT PRIMARY KEY, base_key TEXT NOT NULL, generation INTEGER NOT NULL, kind TEXT NOT NULL,
  repo TEXT NOT NULL, target TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
  data TEXT NOT NULL, status TEXT NOT NULL, url TEXT, number INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0, error TEXT,
  run_id TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS reports_base ON reports(base_key, generation);
CREATE TABLE IF NOT EXISTS workers(
  name TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL, record TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS executions(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, repo TEXT NOT NULL, scenario_id TEXT NOT NULL,
  version TEXT NOT NULL, revision TEXT NOT NULL, outcome TEXT NOT NULL, started REAL NOT NULL,
  receipt TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS executions_scenario ON executions(repo, scenario_id, version, started);
CREATE TABLE IF NOT EXISTS scenarios(
  repo TEXT NOT NULL, id TEXT NOT NULL, version TEXT NOT NULL, hash TEXT NOT NULL, metadata TEXT NOT NULL,
  state TEXT NOT NULL, bundle TEXT NOT NULL, origin_run TEXT NOT NULL, origin_commit TEXT NOT NULL,
  last_attempt_commit TEXT, last_pass_commit TEXT, last_fail_commit TEXT, last_execution REAL,
  finding TEXT, promotion TEXT, reason TEXT, review TEXT,
  PRIMARY KEY(repo,id,version));
CREATE TABLE IF NOT EXISTS recipes(
  repo TEXT PRIMARY KEY, recipe TEXT NOT NULL, revision TEXT NOT NULL, fingerprint TEXT NOT NULL,
  validated INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tasks(
  repo TEXT NOT NULL, id TEXT NOT NULL, proposal TEXT NOT NULL, priority INTEGER NOT NULL,
  origin_commit TEXT NOT NULL, origin_run TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, next_eligible REAL NOT NULL DEFAULT 0,
  blocker TEXT, fingerprint TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(repo,id));
'''


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('PRAGMA journal_mode=WAL;' + SCHEMA)
        # Old checkpoints have no reliable mode once their artifacts are pruned. Recheck once.
        for table, column, definition in (('checkpoints', 'mode', "TEXT NOT NULL DEFAULT 'unknown'"),
                                           ('runs', 'deployment', 'TEXT'), ('runs', 'execution_mode', 'TEXT')):
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
    def start_run(self, run_id, repo, sha, forced, agents, directory, execution_mode=None):
        self._write('INSERT INTO runs(id,repo,sha,forced,status,started,agents,directory,execution_mode) '
                    'VALUES(?,?,?,?,?,?,?,?,?)',
                    (run_id, repo, sha, int(forced), 'running', time.time(), json.dumps(agents), str(directory), execution_mode))

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

    # Isolated execution. These tables are never exported back into authoritative state.
    def save_worker(self, run_id, record):
        self._write('INSERT INTO workers VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET '
                    'status=excluded.status,record=excluded.record',
                    (record['name'], run_id, record['status'], json.dumps(record)))

    def pending_workers(self, run_id=None):
        sql = "SELECT * FROM workers WHERE status!='removed'"
        return self.db.execute(sql + (' AND run_id=?' if run_id else ''),
                               (run_id,) if run_id else ()).fetchall()

    def save_execution(self, receipt):
        self._write('INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                    'outcome=excluded.outcome,receipt=excluded.receipt',
                    (receipt['id'], receipt['run_id'], receipt['repo'], receipt['scenario_id'],
                     receipt['version'], receipt['revision'], receipt['outcome'], receipt['started'],
                     json.dumps(receipt)))

    def prune_execution_outputs(self, run_id):
        rows = self.db.execute('SELECT receipt FROM executions WHERE run_id=?', (run_id,)).fetchall()
        for row in rows:
            receipt = json.loads(row['receipt'])
            for command in [*receipt.get('commands', []), receipt.get('existing_checks', {})]:
                for key in ('stdout', 'stderr'):
                    if key in command:
                        command[key] = '[pruned; artifact hash retained]'
            self.save_execution(receipt)

    def executions(self, repo, scenario_id=None, version=None):
        sql, args = 'SELECT receipt FROM executions WHERE repo=?', [repo]
        for key, value in (('scenario_id', scenario_id), ('version', version)):
            if value is not None:
                sql += f' AND {key}=?'
                args.append(value)
        return [json.loads(r[0]) for r in self.db.execute(sql + ' ORDER BY started', args)]

    def proof_receipts(self, ids):
        if not isinstance(ids, list) or not 2 <= len(ids) <= 10 or len(ids) != len(set(ids)):
            return []
        rows = self.db.execute('SELECT receipt FROM executions WHERE id IN (' +
                               ','.join('?' for _ in ids) + ') ORDER BY started', ids).fetchall()
        return [json.loads(r[0]) for r in rows] if len(rows) == len(ids) else []

    def add_scenario(self, repo, manifest, content_hash, bundle, run_id, commit, review=None):
        old = self.db.execute('SELECT hash FROM scenarios WHERE repo=? AND id=? AND version=?',
                              (repo, manifest['id'], manifest['version'])).fetchone()
        if old and old['hash'] != content_hash:
            raise ValueError('Scenario content versions are immutable')
        self._write('INSERT OR IGNORE INTO scenarios(repo,id,version,hash,metadata,state,bundle,origin_run,'
                    'origin_commit,review) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (repo, manifest['id'], manifest['version'], content_hash, json.dumps(manifest),
                     'candidate', str(bundle), run_id, commit, json.dumps(review) if review else None))
        if review is not None:
            self._write('UPDATE scenarios SET review=? WHERE repo=? AND id=? AND version=?',
                        (json.dumps(review), repo, manifest['id'], manifest['version']))

    def catalog(self, repo, states=('candidate', 'active', 'quarantined')):
        return self.db.execute('SELECT * FROM scenarios WHERE repo=? AND state IN (' +
                               ','.join('?' for _ in states) + ') ORDER BY COALESCE(last_execution,0),id',
                               (repo, *states)).fetchall()

    def scenario_result(self, repo, ident, version, receipts):
        if not receipts:
            return
        current = receipts[-1]
        consistent = len(receipts) >= 2 and len({r['worker'] for r in receipts}) >= 2 and all(
            r.get('semantic_approved') and r.get('reset_ok') and r.get('cleanup') == 'clean'
            and r['bundle_hash'] == current['bundle_hash'] and r['revision'] == current['revision']
            and r['outcome'] == current['outcome'] and
            {f['id'] for f in (r.get('assertions') or {}).get('failed', [])} ==
            {f['id'] for f in (current.get('assertions') or {}).get('failed', [])}
            for r in receipts)
        state = 'active' if consistent and current['outcome'] in ('passed', 'failed') else 'quarantined'
        reason = None if state == 'active' else 'Inconsistent or broken replay; bounded repair required'
        self._write('UPDATE scenarios SET state=?,reason=?,last_attempt_commit=?,last_execution=?,'
                    'last_pass_commit=CASE WHEN ?=\'passed\' THEN ? ELSE last_pass_commit END,'
                    'last_fail_commit=CASE WHEN ?=\'failed\' THEN ? ELSE last_fail_commit END '
                    'WHERE repo=? AND id=? AND version=?',
                    (state, reason, current['revision'], current['started'], current['outcome'], current['revision'],
                     current['outcome'], current['revision'], repo, ident, version))

    def touch_scenario(self, receipt):
        outcome = receipt['outcome']
        self._write('UPDATE scenarios SET last_attempt_commit=?,last_execution=?,'
                    'last_pass_commit=CASE WHEN ?=\'passed\' THEN ? ELSE last_pass_commit END,'
                    'last_fail_commit=CASE WHEN ?=\'failed\' THEN ? ELSE last_fail_commit END '
                    'WHERE repo=? AND id=? AND version=?',
                    (receipt['revision'], receipt['started'], outcome, receipt['revision'], outcome,
                     receipt['revision'], receipt['repo'], receipt['scenario_id'], receipt['version']))

    def link_scenario(self, repo, ident, version, finding=None, promotion=None):
        self._write('UPDATE scenarios SET finding=COALESCE(?,finding),promotion=COALESCE(?,promotion) '
                    'WHERE repo=? AND id=? AND version=?', (finding, promotion, repo, ident, version))

    def save_recipe(self, repo, recipe, revision, fingerprint, validated=False):
        self._write('INSERT OR REPLACE INTO recipes VALUES(?,?,?,?,?)',
                    (repo, json.dumps(recipe), revision, fingerprint, int(validated)))

    def recipe(self, repo, fingerprint):
        row = self.db.execute('SELECT * FROM recipes WHERE repo=? AND fingerprint=? AND validated=1',
                              (repo, fingerprint)).fetchone()
        return row

    def enqueue(self, repo, ident, proposal, priority, commit, run_id, fingerprint, limit=20):
        old = self.db.execute('SELECT * FROM tasks WHERE repo=? AND id=?', (repo, ident)).fetchone()
        if old and old['status'] in ('pending', 'blocked'):
            # Unrelated commits and repeated agent suggestions cannot reset a paused task.
            if old['fingerprint'] != fingerprint:
                self._write("UPDATE tasks SET fingerprint=?,attempts=0,next_eligible=0,status='pending',"
                            'proposal=?,updated=? WHERE repo=? AND id=?',
                            (fingerprint, json.dumps(proposal), time.time(), repo, ident))
            elif proposal.get('scenario_id'):
                # Retain newly frozen candidate references without resetting open-task backoff.
                self._write('UPDATE tasks SET proposal=?,updated=? WHERE repo=? AND id=?',
                            (json.dumps({**json.loads(old['proposal']), **proposal}), time.time(), repo, ident))
            return True
        opened = self.tasks(repo)
        if len(opened) >= limit:
            lowest = min(opened, key=lambda r: (r['priority'], -r['updated']))
            if lowest['priority'] >= priority:
                return False
            self._write("UPDATE tasks SET status='dismissed',blocker='Deferred by backlog limit' WHERE repo=? AND id=?",
                        (repo, lowest['id']))
        self._write('INSERT OR REPLACE INTO tasks(repo,id,proposal,priority,origin_commit,origin_run,status,fingerprint,updated) '
                    'VALUES(?,?,?,?,?,?,\'pending\',?,?)',
                    (repo, ident, json.dumps(proposal), priority, commit, run_id, fingerprint, time.time()))
        return True

    def tasks(self, repo):
        return self.db.execute("SELECT * FROM tasks WHERE repo=? AND status IN ('pending','blocked') "
                               'ORDER BY priority DESC,updated', (repo,)).fetchall()

    def due_tasks(self, repo, retry_cap=3, force=False, now=None):
        now = time.time() if now is None else now
        return [r for r in self.tasks(repo) if force or (r['attempts'] < retry_cap and r['next_eligible'] <= now)]

    def attempt_task(self, repo, ident, done, blocker=None, retry_hours=24, force=False):
        self._write('UPDATE tasks SET status=?,attempts=CASE WHEN ? THEN 0 WHEN ? THEN 1 ELSE attempts+1 END,'
                    'next_eligible=?,blocker=?,updated=? WHERE repo=? AND id=?',
                    ('done' if done else 'blocked', done, force, 0 if done else time.time() + retry_hours * 3600,
                     blocker, time.time(), repo, ident))
