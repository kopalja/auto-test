"""Durable checkpoints and an independent publication outbox."""
import hashlib
import json
import sqlite3
import time


def identity(repository, component, root_cause):
    normalized = "\n".join(" ".join(v.lower().split()) for v in (repository, component, root_cause))
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS repositories (
                name TEXT PRIMARY KEY, completed_sha TEXT, completed_at REAL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, repository TEXT NOT NULL, sha TEXT,
                started REAL NOT NULL, ended REAL, outcome TEXT NOT NULL,
                artifacts TEXT NOT NULL, settings TEXT NOT NULL,
                cleanup TEXT NOT NULL DEFAULT 'pending', summary TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY, repository TEXT NOT NULL, destination TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL,
                title TEXT NOT NULL, body TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', url TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
                uncertain_since REAL, reconciliations INTEGER NOT NULL DEFAULT 0,
                error TEXT, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS successors (
                id TEXT PRIMARY KEY REFERENCES reports(id),
                run_id TEXT NOT NULL REFERENCES runs(id), report TEXT NOT NULL
            );
        """)

    def close(self):
        self.db.close()

    def checkpoint(self, repository):
        row = self.db.execute("SELECT completed_sha FROM repositories WHERE name=?", (repository,)).fetchone()
        return row[0] if row else None

    def unfinished(self, repository, sha):
        row = self.db.execute("SELECT outcome FROM runs WHERE repository=? AND sha=? AND outcome NOT IN ('running','skipped') ORDER BY started DESC LIMIT 1", (repository, sha)).fetchone()
        return bool(row and row[0] in {"blocked", "incomplete"})

    def start(self, run_id, repository, sha, artifacts, settings):
        with self.db:
            self.db.execute("INSERT INTO runs(id,repository,sha,started,outcome,artifacts,settings) VALUES(?,?,?,?,?,?,?)", (run_id, repository, sha, time.time(), "running", str(artifacts), json.dumps(settings)))

    def pin(self, run_id, sha):
        with self.db:
            self.db.execute("UPDATE runs SET sha=? WHERE id=?", (sha, run_id))

    def finish(self, run_id, outcome, summary, cleanup):
        with self.db:
            self.db.execute("UPDATE runs SET outcome=?,summary=?,cleanup=?,ended=? WHERE id=?", (outcome, summary, cleanup, time.time(), run_id))
            if outcome == "completed":
                row = self.db.execute("SELECT repository,sha FROM runs WHERE id=?", (run_id,)).fetchone()
                self.db.execute("INSERT INTO repositories(name,completed_sha,completed_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET completed_sha=excluded.completed_sha,completed_at=excluded.completed_at", (row[0], row[1], time.time()))

    def enqueue(self, report):
        existing = self.report(report["id"])
        if existing and existing["status"] == "closed" and report["kind"] != "blocker":
            return
        # Keep the latest candidate while preserving any unresolved create intent.
        if existing and existing["status"] in {"pending", "uncertain"}:
            with self.db:
                self.db.execute("INSERT INTO successors(id,run_id,report) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id,report=excluded.report",
                                (report["id"], report["run_id"], json.dumps(report)))
            return
        with self.db:
            self.db.execute("""INSERT INTO reports(id,repository,destination,run_id,kind,title,body,payload,updated)
                VALUES(:id,:repository,:destination,:run_id,:kind,:title,:body,:payload,:updated)
                ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id,title=excluded.title,
                body=excluded.body,payload=excluded.payload,kind=CASE WHEN reports.url IS NULL THEN excluded.kind ELSE reports.kind END,
                status='pending',next_retry=0,uncertain_since=NULL,reconciliations=0,error=NULL,updated=excluded.updated""",
                {**report, "payload": json.dumps(report.get("payload", {})), "updated": time.time()})

    def report(self, report_id):
        row = self.db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        return dict(row) if row else None

    def reports(self, repository=None):
        if repository:
            rows = self.db.execute("SELECT * FROM reports WHERE repository=? ORDER BY updated", (repository,))
        else:
            rows = self.db.execute("SELECT * FROM reports ORDER BY updated")
        return [dict(row) for row in rows]

    def pending(self, repository=None):
        return [r for r in self.reports(repository) if r["status"] in {"pending", "uncertain"} and r["next_retry"] <= time.time()]

    def update_report(self, report_id, **values):
        allowed = {"status", "url", "attempts", "next_retry", "uncertain_since", "reconciliations", "error", "body", "payload", "kind"}
        if not values or set(values) - allowed:
            raise ValueError("Invalid report update")
        with self.db:
            self.db.execute("UPDATE reports SET " + ",".join(f"{key}=?" for key in values) + ",updated=? WHERE id=?", (*values.values(), time.time(), report_id))
            if values.get("status") in {"published", "deferred", "closed"}:
                successor = self.db.execute("SELECT report FROM successors WHERE id=?", (report_id,)).fetchone()
                if successor:
                    self.db.execute("DELETE FROM successors WHERE id=?", (report_id,))
                    self.enqueue(json.loads(successor[0]))

    def recover(self):
        with self.db:
            self.db.execute("UPDATE runs SET outcome='incomplete',ended=?,summary='Interrupted before durable completion' WHERE outcome='running'", (time.time(),))
        return self.db.execute("SELECT * FROM runs WHERE cleanup!='clean' ORDER BY started").fetchall()

    def cleanup(self, run_id, status):
        with self.db:
            self.db.execute("UPDATE runs SET cleanup=? WHERE id=?", (status, run_id))

    def status(self):
        latest = self.db.execute("SELECT * FROM runs r WHERE started=(SELECT MAX(started) FROM runs WHERE repository=r.repository) ORDER BY repository").fetchall()
        return {
            "repositories": [dict(row) for row in latest],
            "pending_publication": [{k: r[k] for k in ("id", "repository", "status", "url", "error")} for r in self.reports() if r["status"] in {"pending", "uncertain", "deferred"}],
            "blockers": [{k: r[k] for k in ("repository", "title", "url", "status")} for r in self.reports() if r["kind"] == "blocker" and r["status"] != "closed"],
            "cleanup_failures": [dict(r) for r in self.db.execute("SELECT id,repository,artifacts,cleanup FROM runs WHERE cleanup!='clean'")],
        }

    def expirable(self, days):
        return self.db.execute("""SELECT * FROM runs WHERE ended<? AND cleanup='clean'
            AND outcome!='running' AND NOT EXISTS (SELECT 1 FROM reports WHERE run_id=runs.id
            AND status IN ('pending','uncertain','deferred'))
            AND NOT EXISTS (SELECT 1 FROM successors WHERE run_id=runs.id)""", (time.time() - days * 86400,)).fetchall()
