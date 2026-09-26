import json
import os

import auto_test
from helpers import Case, git
from util import Failure


class ConfigTest(Case):
    def load(self, **changes):
        path = self.tmp / 'config.json'
        path.write_text(json.dumps({**self.config, **changes}))
        return auto_test.load_config(path)

    def checkout(self, relative, origin):
        path = self.tmp / 'monitored-repos' / relative
        path.mkdir(parents=True)
        git(path, 'init', '-q')
        if origin:
            git(path, 'remote', 'add', 'origin', origin)
        return path

    def test_discovery_of_immediate_children_and_origin_forms(self):
        self.checkout('https-form', 'https://github.com/Owner/Web.git')
        self.checkout('ssh-url', 'ssh://git@github.com/owner/api')
        self.checkout('duplicate', 'https://github.com/owner/calc')
        self.checkout('gitlab', 'git@gitlab.com:owner/other.git')
        self.checkout('no-origin', None)
        self.checkout('group/nested', 'git@github.com:owner/nested.git')
        (self.tmp / 'monitored-repos' / 'link').symlink_to(self.tmp / 'monitored-repos' / 'calc')
        cfg = self.load()
        self.assertEqual([r['name'] for r in cfg['repositories']], ['owner/api', 'owner/calc', 'owner/web'])
        # The first checkout wins for duplicate origins ('calc' sorts before 'duplicate').
        self.assertTrue(next(r for r in cfg['repositories'] if r['name'] == 'owner/calc')['checkout']
                        .endswith('/monitored-repos/calc'))

    def test_explicit_entries_override_but_never_expand_discovery(self):
        cfg = self.load(repositories=[
            {'name': 'Owner/Calc', 'soft_budget_minutes': 45, 'instructions': 'Use tox.',
             'agents': {'fixing': {'provider': 'codex', 'model': 'gpt-test', 'reasoning_effort': 'ultra'}},
             'test_environment': {'description': 'dev', 'allowed_targets': ['ns auto-test'],
                                  'credential_environment_variables': ['KUBECONFIG']}},
            {'name': 'owner/absent', 'enabled': True}], instructions='Global rule.')
        [repo] = cfg['repositories']
        self.assertEqual(cfg['missing'], ['owner/absent'])
        self.assertEqual(repo['soft_budget_minutes'], 45)
        self.assertEqual(repo['instructions'], 'Global rule.\n\nUse tox.')
        self.assertEqual(repo['agents']['fixing'], {'provider': 'codex', 'model': 'gpt-test', 'reasoning_effort': 'ultra'})
        self.assertEqual(repo['agents']['investigation']['provider'], 'claude')
        self.assertEqual(repo['test_environment']['allowed_targets'], ['ns auto-test'])

    def test_disabled_entry(self):
        cfg = self.load(repositories=[{'name': 'owner/calc', 'enabled': False}])
        self.assertEqual((cfg['repositories'], cfg['disabled']), ([], ['owner/calc']))

    def test_deployment_mode_is_opt_in(self):
        self.assertEqual(self.load()['repositories'][0]['mode'], 'source')
        cfg = self.load(repositories=[{'name': 'owner/calc', 'mode': 'deployment'}])
        self.assertEqual(cfg['repositories'][0]['mode'], 'deployment')
        with self.assertRaisesRegex(Failure, 'mode must be'):
            self.load(repositories=[{'name': 'owner/calc', 'mode': 'invalid'}])

    def test_relative_paths_resolve_against_config_file(self):
        cwd = os.getcwd()
        os.chdir('/')
        try:
            cfg = self.load(state_directory='state')
        finally:
            os.chdir(cwd)
        self.assertEqual(cfg['state_directory'], self.tmp / 'state')
        self.assertEqual(cfg['monitored_directory'], self.tmp / 'monitored-repos')

    def test_invalid_configurations_are_rejected_clearly(self):
        agent = {'provider': 'claude', 'model': 'm', 'reasoning_effort': 'high'}
        cases = [
            ({'unknown': 1}, 'Unknown configuration keys'),
            ({'agents': {'investigation': agent}}, 'agents must configure exactly'),
            ({'agents': {**self.config['agents'], 'fixing': {**agent, 'reasoning_effort': 'ultra'}}},
             'claude reasoning_effort must be one of'),
            ({'agents': {**self.config['agents'], 'fixing': {**agent, 'model': 'YOUR_CLAUDE_MODEL'}}},
             'set a real claude model'),
            ({'agents': {**self.config['agents'], 'fixing': {**agent, 'provider': 'gemini'}}}, 'provider must be'),
            ({'environment_variables': ['ANTHROPIC_API_KEY']}, 'API-billing'),
            ({'timezone': 'Mars/Base'}, 'Unknown timezone'),
            ({'soft_budget_minutes': 0}, 'positive number'),
            ({'auto_test_repository': 'not a repo'}, 'owner/repo'),
            ({'repositories': [{'name': 'owner/calc', 'enabled': 'yes'}]}, 'enabled must be'),
            ({'repositories': [{'name': 'owner/calc', 'test_environment': {'policy': {}}}]}, 'test_environment accepts'),
            ({'repositories': [{'name': 'owner/calc', 'agents': {'fixing': {'effort': 'high'}}}]}, 'exactly provider'),
        ]
        for changes, message in cases:
            with self.subTest(message=message), self.assertRaises(Failure) as ctx:
                self.load(**changes)
            self.assertIn(message, str(ctx.exception))

    def test_example_configuration_is_valid_after_replacing_placeholders(self):
        example = json.loads((auto_test.ROOT / 'config.example.json').read_text())
        for agent in example['agents'].values():
            self.assertTrue(agent['model'].startswith('YOUR_'))
            agent['model'] = 'real-model'
        path = self.tmp / 'config.json'
        path.write_text(json.dumps(example))
        cfg = auto_test.load_config(path)
        self.assertEqual(cfg['timezone'], 'Europe/Bratislava')
