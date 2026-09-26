import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agents
from fake_agent import default
from util import stop


# The leader exits on SIGTERM, but its child requires SIGKILL.
PARENT = '''
import pathlib, signal, subprocess, sys
child = subprocess.Popen([sys.executable, '-c',
    'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
    'print("ready", flush=True); time.sleep(60)'], stdout=subprocess.PIPE)
assert child.stdout.readline() == b'ready\\n'
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
if sys.argv[2] == 'stay':
    signal.pause()
'''


class ProcessCleanupTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.pidfile = self.root / 'child.pid'

    def argv(self, mode):
        return [sys.executable, '-c', PARENT, str(self.pidfile), mode]

    def wait_for_child(self):
        deadline = time.monotonic() + 5
        while not self.pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return int(self.pidfile.read_text())

    def assert_child_stopped(self, pid):
        # An orphan may remain a zombie until the host's init reaps it.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                state = Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()[0]
            except FileNotFoundError:
                return
            if state == 'Z':
                return
            time.sleep(0.01)
        self.fail(f'child {pid} survived process-group cleanup')

    def cleanup_group(self, proc):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)

    def test_stop_escalates_after_leader_exits_on_term(self):
        proc = subprocess.Popen(self.argv('stay'), start_new_session=True)
        self.addCleanup(self.cleanup_group, proc)
        pid = self.wait_for_child()
        stop(proc, grace=0.1)
        self.assertEqual(proc.returncode, -signal.SIGTERM)
        self.assert_child_stopped(pid)

    def test_stop_cleans_group_when_leader_already_exited(self):
        proc = subprocess.Popen(self.argv('exit'), start_new_session=True)
        self.addCleanup(self.cleanup_group, proc)
        pid = self.wait_for_child()
        self.assertEqual(proc.wait(timeout=5), 0)
        stop(proc, grace=0.1)
        self.assert_child_stopped(pid)

    def test_normal_stage_completion_cleans_background_children(self):
        adapter = mock.Mock()
        adapter.command.return_value = self.argv('exit')
        adapter.parse.return_value = default('investigation')
        with mock.patch('agents.stop', side_effect=lambda proc: stop(proc, grace=0.1)):
            result = agents.run_stage(adapter, {}, 'investigation', '', self.root,
                                      self.root / 'stage', dict(os.environ), self.root)
        pid = self.wait_for_child()
        self.addCleanup(self.kill_child, pid)
        self.assertEqual(result['outcome'], 'completed')
        self.assert_child_stopped(pid)

    def kill_child(self, pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
