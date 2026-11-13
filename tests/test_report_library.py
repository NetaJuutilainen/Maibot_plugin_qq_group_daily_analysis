import asyncio
import base64
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from datetime import date

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('daily_analysis_library', ROOT / 'plugin.py', submodule_search_locations=[str(ROOT)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
DAY = date.today().isoformat()


def png_base64():
    stream = io.BytesIO()
    Image.new('RGB', (32, 64), 'white').save(stream, format='PNG')
    return base64.b64encode(stream.getvalue()).decode('ascii')


class ReportLibraryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.p = module.GroupDailyAnalysisPlugin()
        self.p._plugin_config_instance = module.DailyAnalysisConfig()
        self.p._ctx = SimpleNamespace(logger=Mock(), paths=SimpleNamespace(data_dir=Path(self.temp.name)),
                                     send=SimpleNamespace(image=AsyncMock(return_value=True), text=AsyncMock()),
                                     call_capability=AsyncMock(return_value={'image_base64': png_base64()}))
        self.p._report_store = module.ReportStore(Path(self.temp.name))
        self.p._tasks = []
        self.p._jobs = module.JobManager(self.p._spawn, Mock())
        self.p._stats = {'chats': {'stream': {'group_id': '123', 'name': '测试群', 'last_daily': '',
                                            'days': {DAY: {'total': 20, 'users': {}, 'sample': [], 'hours': [0] * 24}}}}}
        self.p._attach_avatars = AsyncMock()
        self.p._prefetch_profile_images = AsyncMock()
        self.p._render_daily = Mock(return_value='<html>report</html>')
        self.p._inline_avatars = AsyncMock(side_effect=lambda html, context: html)
        self.p._inline_static_assets = Mock(side_effect=lambda html: html)
        self.p._upload_report_to_album = AsyncMock()
        self.p._rebuild_day_from_db = AsyncMock()

    async def asyncTearDown(self):
        tasks = list(self.p._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def save(self, day=DAY, image=True, context=None):
        return self.p._report_store.save('123', day, png_base64() if image else '', '<html>report</html>',
                                         {'group_name': '测试群', 'stream_id': 'obsolete', 'theme': 'atri',
                                          'message_count': 20, 'participant_count': 3}, context)

    async def wait_for_operation(self):
        tasks = list(self.p._jobs._tasks.values())
        await asyncio.gather(*tasks, return_exceptions=True)

    def operation(self, group_id='123'):
        return next(job for job in self.p._jobs.snapshot() if job['group_id'] == group_id)

    async def test_image_generation_archives_before_delivery(self):
        async def send(image, stream):
            meta = self.p._report_store.detail('123', DAY)
            self.assertTrue(meta['has_image'])
            self.assertEqual(meta['version'], '1.6.0')
            self.assertEqual(meta['message_count'], 20)
            self.assertNotIn('sample', meta)
            self.assertEqual(meta['analysis']['topics'], [])
            return True
        self.p.ctx.send.image.side_effect = send
        self.assertEqual(await self.p._generate_daily_and_send('stream', DAY, 'atri'), 'ok')
        self.assertEqual(self.p._stats['chats']['stream']['last_daily'], DAY)

    async def test_send_failure_does_not_lose_archive(self):
        self.p.ctx.send.image.side_effect = RuntimeError('delivery failed')
        self.assertEqual(await self.p._generate_daily_and_send('stream', DAY, 'atri'), 'failed')
        self.assertTrue(self.p._report_store.detail('123', DAY)['has_image'])
        self.assertEqual(self.p._stats['chats']['stream']['last_daily'], '')

    async def test_archive_failure_does_not_block_delivery(self):
        with patch.object(self.p._report_store, 'save', side_effect=OSError('disk failure')):
            self.assertEqual(await self.p._generate_daily_and_send('stream', DAY, 'atri'), 'ok')
        self.p.ctx.send.image.assert_awaited_once()

    async def test_render_failure_does_not_create_success_archive(self):
        self.p.ctx.call_capability.return_value = {}
        with self.assertRaises(RuntimeError):
            await self.p._generate_daily_and_send('stream', DAY, 'atri')
        self.assertEqual(self.p._report_store.list_reports()['total'], 0)

    async def test_html_output_archives_without_rendering(self):
        self.p._cfg().analysis.output_format = 'html'
        self.p._send_daily_html = AsyncMock(return_value=True)
        self.assertEqual(await self.p._generate_daily_and_send('stream', DAY, 'atri'), 'ok')
        self.assertFalse(self.p._report_store.detail('123', DAY)['has_image'])
        self.p.ctx.call_capability.assert_not_awaited()

    async def test_trigger_returns_immediately_and_rejects_duplicate(self):
        gate = asyncio.Event()
        self.p._rebuild_day_from_db.side_effect = lambda *args: None
        async def rebuild(*args):
            await gate.wait()
        self.p._rebuild_day_from_db.side_effect = rebuild
        self.assertTrue((await asyncio.wait_for(self.p.webui_trigger('123'), .5))['accepted'])
        self.assertFalse((await self.p.webui_trigger('123'))['accepted'])
        self.assertTrue((await self.p.webui_cancel('123'))['accepted'])
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'cancelled')
        self.p.ctx.send.image.assert_not_awaited()
        self.assertFalse((await self.p.webui_cancel('123'))['accepted'])

    async def test_cancel_before_coroutine_starts(self):
        await self.p.webui_trigger('123')
        await self.p.webui_cancel('123')
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'cancelled')
        self.assertIsNone(self.p._jobs.active_for('123'))

    async def test_background_error_is_consumed_and_retry_allowed(self):
        self.p._generate_daily_and_send = AsyncMock(side_effect=RuntimeError('private-value'))
        await self.p.webui_trigger('123')
        await self.wait_for_operation()
        state = self.operation()
        self.assertEqual(state['status'], 'failed')
        self.assertNotIn('private-value', json.dumps(state))
        self.p._generate_daily_and_send = AsyncMock(return_value='ok')
        self.assertTrue((await self.p.webui_trigger('123'))['accepted'])
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'succeeded')

    async def test_historical_generation_uses_existing_bucket_not_today(self):
        self.p._stats['chats']['stream']['days']['2026-01-01'] = {'total': 20}
        self.p._generate_daily_and_send = AsyncMock(return_value='ok')
        self.assertTrue((await self.p.webui_trigger('123', '2026-01-01'))['accepted'])
        await self.wait_for_operation()
        self.p._rebuild_day_from_db.assert_not_awaited()
        self.p._generate_daily_and_send.assert_awaited_once_with('stream', '2026-01-01', 'atri', mark_sent=False)
        self.assertFalse((await self.p.webui_trigger('123', '2026-01-02'))['accepted'])

    async def test_trigger_validates_group_date_theme_and_whitelist(self):
        for group, day, theme in [('missing', '', ''), ('../123', '', ''), ('123', 'bad', ''),
                                  ('123', '2999-01-01', ''), ('123', '', 'unknown')]:
            self.assertFalse((await self.p.webui_trigger(group, day, theme))['accepted'])
        self.p._cfg().report.chat_whitelist = '456'
        self.assertFalse((await self.p.webui_trigger('123'))['accepted'])
        self.p._cfg().report.chat_whitelist = ''
        self.p._cfg().plugin.enabled = False
        self.assertFalse((await self.p.webui_trigger('123'))['accepted'])
        self.assertEqual(self.p._tasks, [])

    async def test_resend_uses_saved_bytes_and_current_stream_without_llm(self):
        meta = self.save()
        self.p._generate_daily_and_send = AsyncMock()
        result = await self.p.webui_rerun('123', DAY, meta['revision'])
        self.assertTrue(result['accepted'])
        await self.wait_for_operation()
        self.p.ctx.send.image.assert_awaited_once_with(png_base64(), 'stream')
        self.p._generate_daily_and_send.assert_not_awaited()
        self.assertEqual(self.p._stats['chats']['stream']['last_daily'], '')

    async def test_resend_html_and_explicit_failed_send(self):
        meta = self.save(image=False)
        self.p._send_daily_html = AsyncMock(return_value=False)
        await self.p.webui_rerun('123', DAY, meta['revision'])
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'failed')
        self.p.ctx.send.image.assert_not_awaited()

    async def test_rerender_replaces_archive_without_llm_or_send(self):
        context = {'chat_name': '测试群', 'day': DAY, 'message_count': 20, 'participant_count': 3,
                   'emoji_count': 0, 'total_characters': 100, 'most_active_period': '20点',
                   'chart_data': [], 'user_stats': {}, 'user_names': {}, 'avatars': {}, 'sample': []}
        meta = self.save(context=context)
        self.p._render_daily = Mock(return_value='<html>rerendered</html>')
        self.p._call_llm_json = AsyncMock(side_effect=AssertionError('重绘不得调用模型'))
        self.assertTrue((await self.p.webui_rerender('123', DAY, 'bluearchive', meta['revision']))['accepted'])
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'succeeded')
        current = self.p._report_store.detail('123', DAY)
        self.assertEqual(current['theme'], 'bluearchive')
        self.assertNotEqual(current['revision'], meta['revision'])
        self.assertTrue(current['has_context'])
        self.assertEqual(self.p._render_daily.call_args_list[0].args[2], 'bluearchive')
        self.p.ctx.send.image.assert_not_awaited()
        self.p._call_llm_json.assert_not_awaited()
        self.assertFalse((await self.p.webui_rerender('123', DAY, 'bluearchive', meta['revision']))['accepted'])
        self.assertFalse((await self.p.webui_rerender('123', DAY, 'unknown', current['revision']))['accepted'])

    async def test_rerender_rejected_without_context(self):
        meta = self.save()
        self.assertFalse((await self.p.webui_rerender('123', DAY, 'atri', meta['revision']))['accepted'])
        self.assertEqual(self.p._tasks, [])

    async def test_stale_resend_and_delete_are_rejected(self):
        old = self.save()
        current = self.save()
        self.assertFalse((await self.p.webui_rerun('123', DAY, old['revision']))['accepted'])
        with self.assertRaises(ValueError):
            await self.p.webui_delete('123', DAY, old['revision'])
        self.assertTrue((await self.p.webui_delete('123', DAY, current['revision']))['deleted'])
        self.assertIn(DAY, self.p._stats['chats']['stream']['days'])

    async def test_delete_rejects_while_webui_operation_is_running(self):
        meta = self.save()
        await self.p.webui_trigger('123')
        with self.assertRaises(ValueError):
            await self.p.webui_delete('123', DAY, meta['revision'])
        await self.p.webui_cancel('123')
        await self.wait_for_operation()

    async def test_queries_and_overview(self):
        meta = self.save()
        self.assertEqual((await self.p.webui_reports())['total'], 1)
        self.assertEqual((await self.p.webui_report_detail('123', DAY))['revision'], meta['revision'])
        self.assertTrue((await self.p.webui_report_thumb('123', DAY, meta['revision']))['data_uri'].startswith('data:image/jpeg;base64,'))
        chunk = await self.p.webui_report_chunk('123', DAY, revision=meta['revision'])
        self.assertEqual(chunk['data'], png_base64())
        overview = await self.p.webui_overview()
        self.assertEqual(overview['today_count'], 1)
        self.assertEqual(overview['active_count'], 0)
        self.assertEqual(len(overview['week']), 7)
        self.assertEqual(overview['message_count'], 20)
        self.assertEqual(overview['groups'][0]['group_id'], '123')
        self.assertLess(len(json.dumps(overview).encode()), 512 * 1024)


class UnloadGuardTest(unittest.IsolatedAsyncioTestCase):
    """宿主注册失败时也会调 on_unload（on_load 未跑过），不得把空统计写盘覆盖真实数据。"""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stats_path = Path(self.temp.name) / 'stats.json'
        self.original = {'chats': {'s1': {'group_id': '123', 'name': '测试群',
                                          'days': {DAY: {'total': 20, 'users': {}, 'sample': [], 'hours': [0] * 24}}}}}
        self.stats_path.write_text(json.dumps(self.original, ensure_ascii=False), encoding='utf-8')
        self.p = module.GroupDailyAnalysisPlugin()
        self.p._ctx = SimpleNamespace(logger=Mock(), paths=SimpleNamespace(data_dir=Path(self.temp.name)))
        self.p._tasks = []

    async def test_unload_without_load_keeps_stats_file(self):
        await self.p.on_unload()
        self.assertEqual(json.loads(self.stats_path.read_text(encoding='utf-8')), self.original)

    async def test_unload_after_load_still_saves(self):
        self.p._stats = self.p._load_stats()
        self.p._stats_loaded = True
        self.p._stats['chats']['s1']['days'][DAY]['total'] = 21
        await self.p.on_unload()
        saved = json.loads(self.stats_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['chats']['s1']['days'][DAY]['total'], 21)


class DeclarationTest(unittest.TestCase):
    def test_sdk_discovers_bound_apis(self):
        from maibot_sdk.components import collect_components
        components = collect_components(module.GroupDailyAnalysisPlugin())
        apis = {item['name']: item['metadata'] for item in components if item['type'] == 'API'}
        declaration = json.loads((ROOT / 'webui.json').read_text(encoding='utf-8'))
        bound = {binding['api'] for page in declaration['pages']
                 for binding in [*page.get('queries', {}).values(), *page.get('actions', {}).values()]}
        self.assertEqual(set(apis), bound)
        for name, metadata in apis.items():
            self.assertEqual(metadata['version'], '1')
            self.assertEqual(metadata['handler_name'], 'webui_' + name)
            self.assertFalse(metadata['public'])

    def test_custom_page_does_not_require_form_or_download_permissions(self):
        for entry in ('report_library.html', 'comic_gallery.html', 'overview.html'):
            page = (ROOT / 'webui' / entry).read_text(encoding='utf-8')
            self.assertNotIn('<form', page)
            self.assertNotIn('type="submit"', page)
        page = (ROOT / 'webui/report_library.html').read_text(encoding='utf-8')
        self.assertIn('id="apply-filter" type="button"', page)
        self.assertIn('disabled aria-describedby="download-note"', page)

    def test_manifest_and_bound_api_signatures(self):
        manifest = json.loads((ROOT / '_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['version'], '1.6.0')
        self.assertEqual(manifest['host_application']['min_version'], '1.3.19')
        self.assertEqual(manifest['sdk']['min_version'], '2.12.0')
        self.assertIn('webui.claim_upload', manifest['capabilities'])
        self.assertIn('api.call', manifest['capabilities'])
        self.assertIn('api.get', manifest['capabilities'])
        self.assertTrue((ROOT / manifest['changelog']).is_file())
        declaration = json.loads((ROOT / 'webui.json').read_text(encoding='utf-8'))
        for page in declaration['pages']:
            for binding in [*page.get('queries', {}).values(), *page.get('actions', {}).values()]:
                method = getattr(module.GroupDailyAnalysisPlugin, 'webui_' + binding['api'])
                self.assertTrue(inspect.iscoroutinefunction(method))
                parameters = inspect.signature(method).parameters
                self.assertTrue(set(binding.get('parameters', {})).issubset(parameters))
                required = {name for name, parameter in parameters.items()
                            if name != 'self' and parameter.default is inspect.Parameter.empty}
                self.assertTrue(required.issubset(binding.get('parameters', {})))
                self.assertEqual(binding.get('version', '1'), '1')

    @unittest.skipUnless(os.environ.get('MAIBOT_HOST_ROOT'), 'set MAIBOT_HOST_ROOT to validate against host source')
    def test_host_loader(self):
        host = Path(os.environ['MAIBOT_HOST_ROOT'])
        schema_spec = importlib.util.spec_from_file_location('host_webui_schema', host / 'src/plugin_runtime/webui_schema.py')
        schema = importlib.util.module_from_spec(schema_spec)
        sys.modules[schema_spec.name] = schema
        schema_spec.loader.exec_module(schema)
        extension = schema.load_webui_extension(ROOT)
        self.assertEqual(len(extension.pages), 4)
        self.assertEqual(extension.schema_version, 2)
        stats = next(p for p in extension.pages if p.id == 'stats-data')
        self.assertEqual(stats.mode, 'custom')
        self.assertEqual(stats.entry, 'webui/stats_data.html')
        self.assertEqual(set(stats.actions), {'storage_delete', 'cache_clear'})
        self.assertTrue(stats.actions['storage_delete'].confirmation)
        overview = next(p for p in extension.pages if p.id == 'overview')
        self.assertEqual(overview.mode, 'custom')
        self.assertEqual(overview.entry, 'webui/overview.html')
        self.assertEqual(set(overview.actions), {'trigger', 'cancel'})
        self.assertTrue(overview.actions['trigger'].confirmation)
        gallery = next(p for p in extension.pages if p.id == 'comic-gallery')
        self.assertIn('image_upload', extension.required_capabilities)
        upload = gallery.uploads['ref']
        self.assertEqual(upload.action, 'upload_ref')
        self.assertEqual(upload.image_max_edge, 4096)
        self.assertTrue(gallery.actions['upload_ref'].parameters['upload_id'].required)

    @unittest.skipUnless(os.environ.get('MAIBOT_HOST_ROOT'), 'set MAIBOT_HOST_ROOT to validate against host source')
    def test_page_default_args_pass_host_validation(self):
        # 页面默认带 report_type=''（不限）；choices 漏收空串会被宿主拒绝（2026-11-11 真机实测）
        host = Path(os.environ['MAIBOT_HOST_ROOT'])
        schema_spec = importlib.util.spec_from_file_location('host_webui_schema', host / 'src/plugin_runtime/webui_schema.py')
        schema = importlib.util.module_from_spec(schema_spec)
        sys.modules[schema_spec.name] = schema
        schema_spec.loader.exec_module(schema)
        extension = schema.load_webui_extension(ROOT)
        page = next(p for p in extension.pages if p.id == 'report-library')
        default_args = {'group_id': '', 'date_from': '', 'date_to': '', 'search': '',
                        'report_type': '', 'sort_order': 'desc', 'page': 1}
        page.queries['reports'].validate_args(default_args)
        page.queries['reports'].validate_args({**default_args, 'report_type': 'image', 'search': '测试'})
        page.queries['reports'].validate_args({**default_args, 'report_type': 'html', 'sort_order': 'asc'})
        gallery = next(p for p in extension.pages if p.id == 'comic-gallery')
        gallery.queries['comics'].validate_args({'group_id': '', 'date_from': '', 'date_to': '',
                                                 'search': '', 'sort_order': 'desc', 'page': 1})
        gallery.queries['comic_chunk'].validate_args({'group_id': '123', 'day': '2026-01-01',
                                                      'offset': 0, 'revision': 'a' * 32})
        gallery.actions['draw'].validate_args({'group_id': '123', 'day': '', 'style': 'four_panel'})
        gallery.actions['upload_ref'].validate_args({'upload_id': 'upload-1'})
        with self.assertRaises(ValueError):
            gallery.actions['upload_ref'].validate_args({})
        with self.assertRaises(ValueError):
            gallery.actions['draw'].validate_args({'group_id': '123', 'style': 'unknown'})


if __name__ == '__main__':
    unittest.main()
