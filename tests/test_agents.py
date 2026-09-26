import json
import os
import unittest
from pathlib import Path
from unittest import mock

import agents
from agents import AgentError, Claude, Codex
from helpers import Case

CFG = {'model': 'm-1', 'reasoning_effort': 'high'}


def valid(stage):
    import fake_agent
    return fake_agent.default(stage)


class ValidationTest(unittest.TestCase):
    def test_default_results_are_valid_for_every_stage(self):
        for stage in agents.SCHEMAS:
            agents.validate(valid(stage), agents.SCHEMAS[stage])

    def test_invalid_results_are_rejected(self):
        schema = agents.SCHEMAS['investigation']
        cases = [
            {k: v for k, v in valid('investigation').items() if k != 'findings'},
            {**valid('investigation'), 'extra': 1},
            {**valid('investigation'), 'outcome': 'done'},
            {**valid('investigation'), 'worth_continuing': 'yes'},
            {**valid('investigation'), 'findings': [{'title': 'x'}]},
            {**valid('investigation'), 'summary': 'x' * 20001},
            [],
        ]
        for value in cases:
            with self.subTest(value=str(value)[:60]), self.assertRaises(AgentError) as ctx:
                agents.validate(value, schema)
            self.assertEqual(ctx.exception.kind, 'invalid')

    def test_schemas_are_strict_mode_compatible(self):
        def walk(schema):
            if schema.get('type') == 'object':
                self.assertFalse(schema['additionalProperties'])
                self.assertEqual(set(schema['required']), set(schema['properties']))
                for item in schema['properties'].values():
                    walk(item)
            elif schema.get('type') == 'array':
                walk(schema['items'])
        for schema in agents.SCHEMAS.values():
            walk(schema)

    def test_error_classification(self):
        self.assertEqual(agents.classify("You've hit your usage limit"), 'deferred')
        self.assertEqual(agents.classify('rate_limit'), 'deferred')
        self.assertEqual(agents.classify('unexpected status 401 Unauthorized'), 'setup')
        self.assertEqual(agents.classify('authentication_failed'), 'setup')
        self.assertEqual(agents.classify('model_not_found'), 'setup')
        self.assertEqual(agents.classify('stream disconnected'), 'failed')


class EnvironmentTest(unittest.TestCase):
    def test_only_basic_and_configured_variables_reach_agents(self):
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'a', 'OPENAI_API_KEY': 'b', 'CODEX_API_KEY': 'c',
                                          'GH_TOKEN': 'd', 'KUBECONFIG': '/k', 'RANDOM_VAR': 'e', 'HOME': '/h'}):
            env = agents.environment(['KUBECONFIG', 'ANTHROPIC_API_KEY'], {'AUTO_TEST_RUN_ID': 'r'})
        self.assertEqual({k: env.get(k) for k in ('KUBECONFIG', 'HOME', 'AUTO_TEST_RUN_ID')},
                         {'KUBECONFIG': '/k', 'HOME': '/h', 'AUTO_TEST_RUN_ID': 'r'})
        for name in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'CODEX_API_KEY', 'GH_TOKEN', 'RANDOM_VAR'):
            self.assertNotIn(name, env)


class AdapterTest(Case):
    def stage_dir(self, transcript=(), last=None, stderr=''):
        path = self.tmp / f'stage-{len(list(self.tmp.glob("stage-*")))}'
        path.mkdir()
        (path / 'transcript.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in transcript))
        (path / 'stderr.log').write_text(stderr)
        if last is not None:
            (path / 'last-message.json').write_text(last)
        return path

    def test_codex_command_uses_subscription_and_effort_mapping(self):
        argv = Codex('/bin/codex').command(CFG, Path('/w'), Path('/s'), {}, Path('/r'))
        self.assertEqual(argv[:2], ['/bin/codex', 'exec'])
        for option in ('forced_login_method="chatgpt"', 'model_reasoning_effort="high"', 'approval_policy="never"'):
            self.assertIn(option, argv)
        self.assertEqual(argv[argv.index('--model') + 1], 'm-1')
        self.assertEqual(argv[argv.index('--cd') + 1], '/w')
        self.assertEqual(argv[-1], '-')

    def test_claude_command_uses_subscription_mode(self):
        argv = Claude('/bin/claude').command(CFG, Path('/w'), Path('/s'), {'type': 'object'}, Path('/r'))
        self.assertNotIn('--bare', argv)
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')
        self.assertEqual(argv[argv.index('--model') + 1], 'm-1')
        self.assertEqual(argv[argv.index('--add-dir') + 1], '/r')
        self.assertEqual(argv[argv.index('--setting-sources') + 1], '')
        self.assertIn('--strict-mcp-config', argv)

    def test_codex_parse(self):
        self.assertEqual(Codex('c').parse(self.stage_dir(last='{"a": 1}'), 0), {'a': 1})
        cases = [(self.stage_dir(last='nope'), 0, 'invalid'),
                 (self.stage_dir([{'type': 'turn.failed', 'error': {'message': '401 Unauthorized'}}]), 1, 'setup'),
                 (self.stage_dir([{'type': 'error', 'message': 'usage limit reached'}]), 1, 'deferred'),
                 (self.stage_dir([], stderr='boom'), 1, 'failed')]
        for path, code, kind in cases:
            with self.subTest(kind=kind), self.assertRaises(AgentError) as ctx:
                Codex('c').parse(path, code)
            self.assertEqual(ctx.exception.kind, kind)

    def test_claude_parse(self):
        ok = [{'type': 'result', 'is_error': False, 'structured_output': {'a': 1}}]
        self.assertEqual(Claude('c').parse(self.stage_dir(ok), 0), {'a': 1})
        cases = [([{'type': 'result', 'is_error': False, 'result': 'text only'}], 0, 'invalid'),
                 ([{'type': 'assistant', 'error': 'authentication_failed'}, {'type': 'result', 'is_error': True}], 1,
                  'setup'),
                 ([{'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}},
                   {'type': 'result', 'is_error': True}], 1, 'deferred'),
                 ([{'type': 'assistant', 'error': 'server_error'}, {'type': 'result', 'is_error': True}], 1, 'failed')]
        for transcript, code, kind in cases:
            with self.subTest(kind=kind), self.assertRaises(AgentError) as ctx:
                Claude('c').parse(self.stage_dir(transcript), code)
            self.assertEqual(ctx.exception.kind, kind)

    def test_codex_api_key_login_is_rejected(self):
        self.set_plan(codex_login='Logged in using an API key - sk-...')
        with self.assertRaises(AgentError) as ctx:
            Codex.locate().preflight(dict(os.environ))
        self.assertEqual(ctx.exception.kind, 'setup')

    def test_codex_catalog_validation(self):
        codex = Codex.locate()
        self.assertIn('catalog', codex.check_model('gpt-test', 'low', dict(os.environ)))
        with self.assertRaises(AgentError):
            codex.check_model('gpt-test', 'ultra', dict(os.environ))
        with self.assertRaises(AgentError):
            codex.check_model('unknown-model', 'low', dict(os.environ))
