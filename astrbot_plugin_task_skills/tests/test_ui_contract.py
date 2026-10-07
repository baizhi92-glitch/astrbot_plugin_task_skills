import asyncio
import importlib.util
import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_task_skills import ROOT, load_plugin, storage, structured

ASTRBOT = ROOT.parents[2]


class UIContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        module = load_plugin()

        class Config(dict):
            def save_config(self, values):
                self.update(values)

        with patch.object(module.StarTools, 'get_data_dir', return_value=self.temp.name):
            self.plugin = module.TaskSkillsPlugin(types.SimpleNamespace(), Config())
        self.plugin.publisher = storage.NativePublisher(Path(self.temp.name) / 'native')
        spec = importlib.util.spec_from_file_location('ui_contract_web', ROOT / 'web.py')
        web = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(web)
        self.web_module = web
        self.web = web.SkillWeb(self.plugin)
        self.scope = storage.SkillStore.scope('shared', 'shared')
        self.plugin.store.save(self.scope, structured())

    def test_real_dispatch_all_buttons_and_node_contract(self):
        fixtures = {}

        def dispatch(key, **payload):
            result = self.web.dispatch(payload)
            self.assertEqual(set(result), {'records', 'stats', 'config', 'notice'})
            result['csrf'] = 'test-session-csrf'
            fixtures[key] = result
            return result

        dispatch('list')
        base = {'name': 'check-task', 'revision': 1}
        with self.assertRaisesRegex(ValueError, 'revision conflict'):
            self.web.dispatch(dict(base, action='approve_publish', revision=99))
        self.assertEqual(self.plugin.store.read(self.scope, 'check-task')[0]['review_status'], 'pending')
        dispatch('approve', action='approve', **base)
        self.assertEqual(self.plugin.store.read(self.scope, 'check-task')[0]['review_verification'], '')
        dispatch('approve_publish', action='approve_publish', verification='', **base)
        target = self.plugin.publisher.target('check-task')
        self.assertTrue((target / 'SKILL.md').is_file())
        self.assertIn('name: "task-learned-check-task"', (target / 'SKILL.md').read_text(encoding='utf-8'))
        self.plugin.publisher.owned(target)
        dispatch('publish', action='publish', name='check-task')
        changed = dict(structured(), tags=['文档分类', '验证分类'], checks=['Verify the changed document against actual results.'])
        dispatch('edit', action='edit', name='check-task', revision=1, skill=changed)
        self.assertEqual(fixtures['edit']['records'][0]['skill']['tags'], changed['tags'])
        self.assertEqual(fixtures['edit']['records'][0]['review_status'], 'pending')
        self.assertEqual(fixtures['edit']['records'][0]['approved_skill']['tags'], structured()['tags'])
        with self.assertRaisesRegex(ValueError, 'revision conflict'):
            self.web.dispatch(dict(action='edit', name='check-task', revision=1, skill=changed))
        with self.assertRaisesRegex(ValueError, 'administrator approval required'):
            self.web.dispatch(dict(action='publish', name='check-task'))
        dispatch('rollback', action='rollback', name='check-task', revision=1)
        dispatch('reject', action='reject', name='check-task', revision=3)
        values = dict(auto_learn=False, provider_id='', max_skills=12, cooldown_seconds=120,
                      shared_skills=True, review_mode='manual', auto_publish=False)
        dispatch('config', action='config', config=values)
        self.assertFalse(self.plugin.enabled(self.scope))
        dispatch('delete', action='delete', name='check-task')
        self.assertFalse(target.exists())
        completed = subprocess.run(['node', str(ROOT / 'tests' / 'frontend_contract.cjs')],
                                   input=json.dumps(fixtures), text=True, encoding='utf-8', capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_optional_notes_validation_and_redaction(self):
        base = dict(action='approve_publish', name='check-task', revision=1)
        for note in (None, 1, False, [], {}, 'x' * 4001, 'password=private-review-secret',
                     'sk-123456789abcdef', 'Bearer private-review-secret'):
            with self.subTest(note_type=type(note).__name__):
                before = self.plugin.store.read(self.scope, 'check-task')[0]
                with self.assertRaisesRegex(ValueError, 'invalid review note') as raised:
                    self.web.dispatch(dict(base, verification=note))
                self.assertNotIn('private-review-secret', self.web_module.public_error(raised.exception))
                self.assertEqual(self.plugin.store.read(self.scope, 'check-task')[0], before)
                self.assertFalse(self.plugin.publisher.target('check-task').exists())
        for action in ('approve', 'approve_publish'):
            for note in ('', '好', 'short', '   ', 'x' * 4000):
                result = self.web.dispatch(dict(base, action=action, verification=note))
                record = result['records'][0]
                self.assertEqual(record['review_status'], 'approved')
                self.assertEqual(record['review_verification'], storage.safe_task(note))
        result = self.web.dispatch(dict(base, verification='看 https://private.example/a /private/file a@private.example 1234567890'))
        note = result['records'][0]['review_verification']
        for private in ('private.example', '/private/file', '1234567890'):
            self.assertNotIn(private, note)
        self.assertIn('<resource>', note)
        self.assertIn('<path>', note)
        self.plugin.publisher.owned(self.plugin.publisher.target('check-task'))

    def test_approve_publish_refuses_modified_and_foreign_native(self):
        payload = dict(action='approve_publish', name='check-task', revision=1)
        self.web.dispatch(payload)
        md = self.plugin.publisher.target('check-task') / 'SKILL.md'
        md.write_text('manual content', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'modified native'):
            self.web.dispatch(payload)
        self.assertEqual(md.read_text(encoding='utf-8'), 'manual content')
        foreign = self.plugin.publisher.target('foreign-task')
        foreign.mkdir()
        (foreign / 'SKILL.md').write_text('foreign content', encoding='utf-8')
        self.plugin.store.save(self.scope, dict(structured('foreign-task'), description='Another task with a distinct purpose.'))
        with self.assertRaisesRegex(ValueError, 'foreign native files'):
            self.web.dispatch(dict(payload, name='foreign-task'))
        self.assertEqual((foreign / 'SKILL.md').read_text(encoding='utf-8'), 'foreign content')

    def test_actual_json_response_shape_and_api_errors(self):
        # Load the real web response/request proxy rather than replacing json_response.
        spec = importlib.util.spec_from_file_location('contract_astrbot_web', ASTRBOT / 'astrbot/api/web.py')
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        from starlette.requests import Request

        async def exercise():
            async def call(method='GET', payload=None, token='', origin='http://localhost', username='admin', bridge=False):
                raw = json.dumps(payload or {}).encode()
                async def receive():
                    return {'type': 'http.request', 'body': raw, 'more_body': False}
                route = '/api/v1/plugins/extensions/astrbot_plugin_task_skills/api' if bridge else '/api/plug/task-skills/api'
                request = Request({'type': 'http', 'method': method, 'path': route, 'scheme': 'http',
                                   'query_string': b'', 'server': ('localhost', 80),
                                   'headers': [(b'host', b'localhost'), (b'origin', origin.encode()),
                                               (b'content-type', b'application/json'), (b'x-task-csrf', token.encode())]}, receive)
                with api.bind_request_context(api.PluginRequest(request, username=username)):
                    response = await self.web.api()
                return response.status_code, json.loads(response.body)

            import sys
            auth = types.ModuleType('astrbot.dashboard.api.auth')
            auth.require_dashboard_user = AsyncMock(return_value='admin')
            responses = types.ModuleType('astrbot.dashboard.responses')
            responses.ApiError = type('ApiError', (Exception,), {})
            with patch.dict(sys.modules, {'astrbot.api.web': api, 'astrbot.dashboard.api.auth': auth,
                                         'astrbot.dashboard.responses': responses}):
                code, data = await call()
                self.assertEqual(code, 200)
                self.assertEqual(set(data), {'records', 'stats', 'config', 'notice', 'csrf'})
                token = data['csrf']
                payload = dict(action='approve_publish', name='check-task', revision=1,
                               verification='password=private-review-secret')
                code, data = await call('POST', payload, token)
                self.assertEqual(code, 400)
                self.assertIn('可选备注', data['error'])
                self.assertNotIn('private-review-secret', json.dumps(data))
                self.assertEqual(self.plugin.store.read(self.scope, 'check-task')[0]['review_status'], 'pending')
                self.assertFalse(self.plugin.publisher.target('check-task').exists())
                payload.update(verification='', revision=99)
                code, data = await call('POST', payload, token)
                self.assertEqual(code, 400)
                self.assertIn('版本冲突', data['error'])
                payload.update(revision=1, csrf=token)
                code, data = await call('POST', payload, bridge=True)
                self.assertEqual(code, 200)
                self.assertEqual(data['records'][0]['review_status'], 'approved')
                self.assertEqual(data['records'][0]['review_verification'], '')
                values = dict(auto_learn=False, provider_id='', max_skills=12, cooldown_seconds=120,
                              shared_skills=True, review_mode='manual', auto_publish=False)
                actions = [dict(action='approve', name='check-task', revision=1, verification='好'),
                           dict(action='publish', name='check-task'),
                           dict(action='edit', name='check-task', revision=1, skill=structured()),
                           dict(action='rollback', name='check-task', revision=1),
                           dict(action='reject', name='check-task', revision=3),
                           dict(action='config', config=values),
                           dict(action='delete', name='check-task')]
                for action in actions:
                    code, data = await call('POST', action, token)
                    self.assertEqual(code, 200, action['action'])
                    self.assertEqual(set(data), {'records', 'stats', 'config', 'notice', 'csrf'})
                    self.assertIsInstance(data['records'], list)
                    self.assertEqual(data['csrf'], token)
                self.assertEqual((await call(username=None))[0], 403)
                self.assertEqual((await call('POST', {}, 'wrong'))[0], 403)
                self.assertEqual((await call('POST', {}, token, origin='null'))[0], 403)
                self.assertEqual((await call('DELETE'))[0], 403)
        asyncio.run(exercise())

    def test_public_errors_do_not_expose_arbitrary_exception_text(self):
        for exc in (OSError('private path'), ValueError('请求 private path'), KeyError('private path')):
            self.assertNotIn('private path', self.web_module.public_error(exc))

    def test_dashboard_sandbox_and_bridge_source_contract(self):
        dashboard = (ASTRBOT / 'dashboard/src/views/PluginPagePage.vue').read_text(encoding='utf-8')
        self.assertIn('sandbox="allow-scripts allow-forms allow-downloads"', dashboard)
        self.assertIn('response.data?.data ?? response.data', dashboard)
        html = (ROOT / 'pages/技能管理/index.html').read_text(encoding='utf-8')
        self.assertNotIn('prompt(', html)
        self.assertNotIn('confirm(', html)

    def test_page_hierarchy_category_and_responsive_contract(self):
        from html.parser import HTMLParser

        class PageParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids = []
                self.styles = 0
                self.scripts = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if 'id' in attrs:
                    self.ids.append(attrs['id'])
                if tag == 'style':
                    self.styles += 1
                if tag == 'script' and 'src' in attrs:
                    self.scripts.append(attrs['src'])

        html = (ROOT / 'pages/技能管理/index.html').read_text(encoding='utf-8')
        parser = PageParser()
        parser.feed(html)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertEqual(parser.styles, 1)
        self.assertEqual(parser.scripts, ['/api/plugin/page/bridge-sdk.js'])
        for ident in ('libraryTab', 'reviewTab', 'settingsTab', 'sectionTitle', 'sectionNote', 'overview'):
            self.assertIn(ident, parser.ids)
        for text in ('技能分类', '未分类', '修改 tags 数组', '保存后再次待审核', '审核与发布'):
            self.assertIn(text, html)
        self.assertIn('grid-template-columns:300px minmax(0,1fr)', html)
        self.assertIn('@media(max-width:760px)', html)
        self.assertIn('.settings-grid{grid-template-columns:minmax(0,1fr)}', html)
        self.assertNotIn('gradient(', html)
        self.assertNotIn('https://', html)


if __name__ == '__main__':
    unittest.main()
