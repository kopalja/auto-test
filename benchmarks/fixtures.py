"""Evaluator-only ground truth. Never transfer this module or fixed history to agents."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def case(source, replacement, contract, existing, oracle, root_cause):
    return dict(source=source, fixed=source.replace(*replacement), contract=contract,
                existing=existing, oracle=oracle, root_cause=root_cause, clean=False)


CASES = {
    'case-01': case('''import sqlite3
class Queue:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs(request TEXT, payload TEXT)')
    def submit(self, request, payload):
        self.db.execute('INSERT INTO jobs VALUES(?,?)', (request, payload))
        self.db.commit()
    def jobs(self):
        return self.db.execute('SELECT request,payload FROM jobs').fetchall()
''', ("        self.db.execute('INSERT INTO jobs VALUES(?,?)', (request, payload))",
      "        if not self.db.execute('SELECT 1 FROM jobs WHERE request=?', (request,)).fetchone():\n"
      "            self.db.execute('INSERT INTO jobs VALUES(?,?)', (request, payload))"),
      'Queue.submit durably accepts a request. Retrying the same request identifier has exactly one logical effect.',
      "q=app.Queue('smoke.db'); q.submit('one','data'); assert q.jobs()==[('one','data')]",
      "q=app.Queue('oracle.db'); q.submit('retry','payload'); q.submit('retry','payload'); assert len(q.jobs())==1",
      'Queue.submit inserts an already accepted request again'),
    'case-02': case('''import json
from pathlib import Path
class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.items = json.loads(self.path.read_text()) if self.path.exists() else {}
    def put(self, key, value):
        self.items[key] = value
        return 'acknowledged'
    def get(self, key):
        return self.items.get(key)
''', ("        return 'acknowledged'", "        self.path.write_text(json.dumps(self.items))\n        return 'acknowledged'"),
      'Store.put acknowledges durable writes. A new Store instance after application restart returns acknowledged data.',
      "s=app.Store('smoke.json'); assert s.put('key','value')=='acknowledged'; assert s.get('key')=='value'",
      "s=app.Store('oracle.json'); s.put('item','payload'); del s; assert app.Store('oracle.json').get('item')=='payload'",
      'Store.put acknowledges without persisting the new state'),
    'case-03': case('''import threading
class Inventory:
    def __init__(self, available, after_read=lambda: None):
        self.available = available
        self.lock = threading.Lock()
        self.after_read = after_read
    def reserve(self):
        observed = self.available
        self.after_read()
        with self.lock:
            if observed <= 0:
                return False
            self.available = observed - 1
            return True
''', ('            if observed <= 0:', '            observed = self.available\n            if observed <= 0:'),
      'Inventory.reserve is atomic across concurrent callers: at most the initial quantity succeeds. '
      'after_read is an optional deterministic test barrier called after the optimistic read.',
      'i=app.Inventory(1); assert i.reserve(); assert not i.reserve()',
      'import threading\nbarrier=threading.Barrier(2,timeout=5)\ni=app.Inventory(1,barrier.wait)\nresults=[]\n'
      'threads=[threading.Thread(target=lambda: results.append(i.reserve())) for _ in range(2)]\n'
      '[t.start() for t in threads]\n[t.join(6) for t in threads]\nassert len(results)==2 and sum(results)==1',
      'Inventory.reserve checks a stale read outside the atomic update'),
    'case-04': case('''def encode_label(text):
    if len(text) > 16:
        raise ValueError('label exceeds storage limit')
    return text.encode('utf-8')
''', ('if len(text) > 16:', "if len(text.encode('utf-8')) > 16:"),
      'encode_label accepts text whose UTF-8 representation is at most 16 bytes. Larger values raise ValueError.',
      "assert app.encode_label('example') == b'example'",
      "rejected=False\ntry: app.encode_label('é'*9)\nexcept ValueError: rejected=True\nassert rejected",
      'encode_label checks character count instead of encoded storage bytes'),
    'case-05': case('''class Documents:
    def __init__(self):
        self.rows = {}
    def save(self, tenant, document, content):
        self.rows[tenant, document] = content
    def read(self, tenant, document):
        return next((v for (t,d),v in self.rows.items() if d == document), None)
''', ('if d == document)', 'if d == document and t == tenant)'),
      'Documents.read returns only the named tenant\'s document. Document identifiers are tenant-local. '
      'An absent tenant/document pair returns None. All examples use synthetic data.',
      "d=app.Documents(); d.save('a','doc','public example'); assert d.read('a','doc')=='public example'",
      "d=app.Documents(); d.save('a','doc','alpha'); d.save('b','doc','beta'); assert d.read('b','doc')=='beta'; "
      "assert d.read('c','doc') is None",
      'Documents.read omits tenant from the lookup predicate'),
}
for ident, source_id in (('case-06', 'case-01'), ('case-07', 'case-05')):
    item = dict(CASES[source_id])
    item.update(source=item['fixed'], clean=True, root_cause=None)
    CASES[ident] = item


def materialize(ident, destination, variant='initial'):
    """A neutral source snapshot only: no oracle, defect labels, patches or fixed history."""
    item = CASES[ident]
    destination.mkdir(parents=True, exist_ok=True)
    # 'later' reintroduces the initial defect after a fixed control: retained passing scenarios should catch it.
    source = item['fixed'] if variant == 'fixed' else item['source']
    (destination / 'app.py').write_text(source)
    (destination / 'README.md').write_text('# Local application\n\n' + item['contract'] + '\n')
    (destination / 'test_existing.py').write_text('import app\n' + item['existing'] + '\n')
    (destination / 'auto-test.md').write_text(
        '# Local testing contract\n\n' + item['contract'] + '\n\n'
        'Use Python 3 standard library; no services or external dependencies. Existing checks: '
        '`python3 test_existing.py`. Use temporary filenames and synthetic inputs.\n'
        'Workflows: accept input; observe returned/persisted result; open a new application instance where relevant.\n'
        'Reset: remove only data created by your scenario. Worker removal handles all local storage.\n')
    return destination


def evaluate_fixture(ident, variant, root=None):
    with tempfile.TemporaryDirectory(dir=root) as directory:
        path = materialize(ident, Path(directory), variant)
        existing = subprocess.run([sys.executable, 'test_existing.py'], cwd=path, capture_output=True, text=True, timeout=10)
        oracle = subprocess.run([sys.executable, '-c', 'import app\n' + CASES[ident]['oracle']],
                                cwd=path, capture_output=True, text=True, timeout=15)
        return {'case': ident, 'variant': variant, 'existing_checks': existing.returncode,
                'oracle': oracle.returncode, 'oracle_stderr': oracle.stderr[-2000:]}


def validate_all(root=None):
    rows = [evaluate_fixture(ident, variant, root) for ident in CASES for variant in ('initial', 'fixed', 'later')]
    for r in rows:
        expected = 0 if CASES[r['case']]['clean'] or r['variant'] == 'fixed' else 1
        if r['existing_checks'] != 0 or r['oracle'] != expected:
            raise AssertionError(json.dumps(r))
    return rows
