import asyncio
import base64
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from datetime import date

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('daily_analysis_wiring', ROOT / 'plugin.py', submodule_search_locations=[str(ROOT)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
DAY = date.today().isoformat()


def png_bytes(color='white'):
    stream = io.BytesIO()
    Image.new('RGB', (24, 48), color).save(stream, format='PNG')
    return stream.getvalue()


def png_base64(color='white'):
    return base64.b64encode(png_bytes(color)).decode('ascii')


ANALYSIS = {
    'topics': [{'topic': '今日话题', 'contributors': ['[10001]'], 'detail': '[10001] 主导了讨论'}],
    'titles': [{'user_id': '10001', 'name': '小明', 'title': '水群之王', 'mbti': '', 'reason': ''}],
    'quotes': [{'content': '[10001] 的金句', 'sender': '[10001]', 'reason': '好笑'}],
    'quality': None,
}


class FakeDrawpic:
    """脚本化的 drawpic v2 对端：业务字典直达（宿主+SDK 已双层解包）+ 可注入的受理/进度/传输故障。"""

    def __init__(self, blob=None):
        self.calls = []
        self.blob = blob if blob is not None else png_bytes()
        self.available = True
        self.submit_raises = False
        self.submit_result = {'accepted': True, 'task_id': 'task-1', 'reused': False}
        self.task_results = [{'task_id': 'task-1', 'status': 'completed', 'error': None, 'retry_after_ms': 0}]
        self.task_raises = False
        self.task_gate = None
        self.request_result = {'task_id': 'task-1', 'status': 'completed', 'error': None}

    async def get(self, name, *, version=''):
        return {'api': name, 'version': version} if self.available else None

    async def call(self, name, *, version='', **kwargs):
        self.calls.append((name, kwargs))
        operation = name[len('maimai-drawpic-plugin.drawpic_'):-len('_v2')]
        if operation == 'submit':
            if self.submit_raises:
                raise RuntimeError('transport down')
            result = dict(self.submit_result)
            result['request_id'] = kwargs['request_id']
            return result
        if operation == 'task':
            if self.task_gate is not None:
                await self.task_gate.wait()
            if self.task_raises:
                raise RuntimeError('transport down')
            index = min(len([c for c in self.calls if c[0] == name]) - 1, len(self.task_results) - 1)
            return dict(self.task_results[index])
        if operation == 'request':
            return dict(self.request_result)
        if operation == 'cancel':
            return {'task_id': kwargs['task_id'], 'status': 'cancelled',
                    'cancel_requested': True, 'upstream_cancelled': None}
        if operation == 'image':
            offset = kwargs['offset']
            block = self.blob[offset:offset + 192 * 1024]
            next_offset = offset + len(block)
            return {
                'data': base64.b64encode(block).decode('ascii'), 'offset': offset,
                'next_offset': next_offset, 'size': len(self.blob),
                'done': next_offset == len(self.blob), 'mime': 'image/png', 'error': None}
        raise AssertionError(f'unexpected operation {operation}')

    def ops(self):
        return [name[len('maimai-drawpic-plugin.drawpic_'):-len('_v2')] for name, _ in self.calls]

    def kwargs_for(self, operation):
        return [kwargs for name, kwargs in self.calls
                if name == f'maimai-drawpic-plugin.drawpic_{operation}_v2']


class ComicWiringTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.p = module.GroupDailyAnalysisPlugin()
        self.p._plugin_config_instance = module.DailyAnalysisConfig()
        self.api = FakeDrawpic()
        self.p._ctx = SimpleNamespace(
            logger=Mock(), paths=SimpleNamespace(data_dir=Path(self.temp.name)),
            send=SimpleNamespace(image=AsyncMock(return_value=True), text=AsyncMock()),
            api=self.api, webui=SimpleNamespace(claim_upload=AsyncMock()),
            call_capability=AsyncMock(return_value={'image_base64': png_base64()}))
        self.p._report_store = module.ReportStore(Path(self.temp.name))
        self.p._comic_book = module.ComicRequestBook(Path(self.temp.name) / 'comic_requests.json')
        self.p._drawpic_probe = None
        self.p._tasks = []
        self.p._jobs = module.JobManager(self.p._spawn, Mock())
        self.p._stats = {'chats': {'stream': {'group_id': '123', 'name': '测试群', 'last_daily': '',
                                            'days': {DAY: {'total': 20, 'users': {}, 'sample': [], 'hours': [0] * 24}}}}}
        self.p._call_llm_json = AsyncMock(return_value=None)
        self.p._comic_cfg().enabled = True

    async def asyncTearDown(self):
        tasks = list(self.p._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def save_daily(self, analysis=None, context=None):
        return self.p._report_store.save('123', DAY, png_base64(), '<html>report</html>',
                                         {'group_name': '测试群', 'theme': 'atri', 'message_count': 20,
                                          'analysis': ANALYSIS if analysis is None else analysis}, context)

    def save_comic(self, **meta):
        return self.p._report_store.save_comic('123', DAY, png_base64(), {
            'group_name': '测试群', 'style': 'four_panel', 'request_id': 'comic-abcdef123456',
            'task_id': 'task-1', 'prompt': 'p', 'storyboard': {}, 'message_count': 20,
            'version': '1.5.0', **meta})

    async def wait_for_operation(self):
        await asyncio.gather(*list(self.p._jobs._tasks.values()), return_exceptions=True)

    async def drain(self):
        for _ in range(100):
            tasks = list(self.p._tasks)
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)
        raise AssertionError('background tasks did not drain')

    def operation(self):
        return next(job for job in self.p._jobs.snapshot() if job['kind'] == 'comic')

    def book_entry(self):
        return self.p._comic_book.latest('123', DAY)

    async def test_draw_validation_chain(self):
        self.p._comic_cfg().enabled = False
        self.assertFalse((await self.p.webui_draw('123'))['accepted'])
        self.p._comic_cfg().enabled = True
        for group, day, style in [('../123', '', ''), ('123', '2999-01-01', ''), ('123', 'bad', ''),
                                  ('123', '', 'unknown')]:
            with self.subTest(group=group, day=day, style=style):
                self.assertFalse((await self.p.webui_draw(group, day, style))['accepted'])
        self.assertFalse((await self.p.webui_draw('123'))['accepted'])  # 无日报存档
        self.save_daily()
        self.api.available = False
        self.p._drawpic_probe = None
        self.assertFalse((await self.p.webui_draw('123'))['accepted'])
        self.api.available = True
        self.p._drawpic_probe = None
        self.api.task_gate = asyncio.Event()
        self.api.task_results = [{'task_id': 'task-1', 'status': 'running', 'error': None}]
        self.assertTrue((await self.p.webui_draw('123'))['accepted'])
        self.assertFalse((await self.p.webui_draw('123'))['accepted'])  # 同群互斥
        self.assertTrue((await self.p.webui_cancel('123'))['accepted'])
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'cancelled')

    async def test_draw_pipeline_happy_path(self):
        self.save_daily(context={'user_names': {'10001': '小明'}})
        self.assertTrue((await self.p.webui_draw('123', DAY, 'four_panel'))['accepted'])
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'succeeded')
        self.assertEqual(self.api.ops()[0], 'submit')
        self.assertNotIn('request', self.api.ops())
        submit = self.api.kwargs_for('submit')[0]
        self.assertEqual(submit['stream_id'], '')
        entry = self.book_entry()
        self.assertEqual(submit['request_id'], entry['request_id'])
        self.assertEqual((entry['status'], entry['sent'], entry['task_id']), ('completed', True, 'task-1'))
        self.assertIn('小明', submit['prompt'])
        self.assertNotIn('[10001]', submit['prompt'])
        detail = self.p._report_store.comic_detail('123', DAY)
        self.assertTrue(detail['sent'])
        self.assertEqual(detail['request_id'], entry['request_id'])
        self.p.ctx.send.image.assert_awaited_once_with(base64.b64encode(self.api.blob).decode('ascii'), 'stream')
        self.p.ctx.send.text.assert_awaited_once()
        self.p._call_llm_json.assert_awaited_once()

    async def test_draw_submit_unknown_keeps_request_key(self):
        self.save_daily()
        self.api.submit_raises = True
        await self.p.webui_draw('123')
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'failed')
        entry = self.book_entry()
        self.assertEqual(entry['status'], 'unknown')
        self.assertIn(entry['request_id'], [row['request_id'] for row in self.p._comic_book.recoverable()])
        self.assertEqual(len(self.api.kwargs_for('submit')), 1)
        self.assertIn('恢复', self.operation()['error'])

    async def test_draw_not_accepted_records_business_code(self):
        self.save_daily()
        self.api.submit_result = {'accepted': False, 'task_id': None,
                                  'error': {'code': 'CONCURRENCY_LIMIT', 'message': '任务已满',
                                            'retry_query': False, 'retry_submit': True}}
        await self.p.webui_draw('123')
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'failed')
        self.assertEqual(self.book_entry()['status'], 'not_accepted')
        self.assertIn('CONCURRENCY_LIMIT', self.book_entry()['error'])
        self.assertEqual(self.p._comic_book.recoverable(), [])

    async def test_wait_timeout_keeps_progress_for_resume(self):
        self.save_daily()
        self.api.task_results = [{'task_id': 'task-1', 'status': 'running', 'error': None, 'retry_after_ms': 0}]
        clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 301]))
        with patch.object(module, 'time', clock):
            await self.p.webui_draw('123')
            await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'failed')
        self.assertIn('超时', self.operation()['error'])
        self.assertEqual(self.book_entry()['status'], 'running')
        self.assertEqual(len(self.p._comic_book.recoverable()), 1)

    async def test_wait_rpc_failure_retries_then_keeps_key(self):
        self.save_daily()
        self.api.task_raises = True
        clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0, 301]))
        with patch.object(module, 'time', clock), patch.object(module.asyncio, 'sleep', new=AsyncMock()):
            await self.p.webui_draw('123')
            await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'failed')
        self.assertEqual(self.book_entry()['status'], 'unknown')
        self.assertEqual(len(self.api.kwargs_for('task')), 2)

    async def test_cancel_notifies_remote_and_marks_book(self):
        self.save_daily()
        self.api.task_gate = asyncio.Event()
        self.api.task_results = [{'task_id': 'task-1', 'status': 'running', 'error': None}]
        await self.p.webui_draw('123')
        for _ in range(300):
            if self.api.kwargs_for('task'):
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.api.kwargs_for('task'))
        self.assertTrue((await self.p.webui_cancel('123'))['accepted'])
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'cancelled')
        self.assertEqual(self.book_entry()['status'], 'cancelled')
        self.assertEqual(len(self.api.kwargs_for('cancel')), 1)
        self.assertEqual(self.p._report_store.list_comics()['total'], 0)

    async def test_resume_archived_unsent_only_resends(self):
        self.save_daily()
        self.save_comic()
        self.p._comic_book.record('comic-abcdef123456', '123', DAY, 'four_panel', 'p')
        self.p._comic_book.update('comic-abcdef123456', task_id='task-1', status='completed')
        self.assertTrue((await self.p.webui_comic_resume('123', DAY))['accepted'])
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'succeeded')
        self.assertEqual(self.api.calls, [])
        self.p.ctx.send.image.assert_awaited_once()
        self.assertTrue(self.p._report_store.comic_detail('123', DAY)['sent'])
        self.assertTrue(self.book_entry()['sent'])

    async def test_resume_recovers_by_request_key_without_submit(self):
        self.save_daily()
        self.p._comic_book.record('comic-abcdef123456', '123', DAY, 'four_panel', 'p')
        self.assertTrue((await self.p.webui_comic_resume('123', DAY))['accepted'])
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'succeeded')
        self.assertEqual(self.api.ops()[0], 'request')
        self.assertNotIn('submit', self.api.ops())
        entry = self.book_entry()
        self.assertEqual((entry['task_id'], entry['status'], entry['sent']), ('task-1', 'completed', True))
        self.assertTrue(self.p._report_store.comic_detail('123', DAY)['sent'])

    async def test_resume_request_not_found_fails_without_resubmit(self):
        self.save_daily()
        self.p._comic_book.record('comic-abcdef123456', '123', DAY, 'four_panel', 'p')
        self.api.request_result = {'error': {'code': 'REQUEST_NOT_FOUND', 'message': ''}}
        await self.p.webui_comic_resume('123', DAY)
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'failed')
        self.assertEqual(self.book_entry()['status'], 'failed')
        self.assertNotIn('submit', self.api.ops())

    async def test_resume_rejects_when_nothing_recoverable(self):
        self.assertFalse((await self.p.webui_comic_resume('123', DAY))['accepted'])
        self.p._comic_book.record('comic-abcdef123456', '123', DAY, 'four_panel', 'p')
        self.p._comic_book.update('comic-abcdef123456', status='completed', sent=True)
        self.assertFalse((await self.p.webui_comic_resume('123', DAY))['accepted'])
        self.p._comic_book.update('comic-abcdef123456', status='failed')
        self.assertFalse((await self.p.webui_comic_resume('123', DAY))['accepted'])
        self.assertFalse((await self.p.webui_comic_resume('123', 'bad-day'))['accepted'])

    async def test_comic_resend_and_delete_guards(self):
        self.save_daily()
        meta = self.save_comic()
        self.assertFalse((await self.p.webui_comic_resend('123', DAY, '0' * 32))['accepted'])
        self.assertTrue((await self.p.webui_comic_resend('123', DAY, meta['revision']))['accepted'])
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'succeeded')
        self.assertTrue(self.p._report_store.comic_detail('123', DAY)['sent'])
        with self.assertRaises(ValueError):
            await self.p.webui_delete_comic('123', DAY)
        with self.assertRaises(ValueError):
            await self.p.webui_delete_comic('123', DAY, '0' * 32)
        self.api.task_gate = asyncio.Event()
        self.api.task_results = [{'task_id': 'task-1', 'status': 'running', 'error': None}]
        await self.p.webui_draw('123')
        with self.assertRaises(ValueError):
            await self.p.webui_delete_comic('123', DAY, meta['revision'])
        await self.p.webui_cancel('123')
        await self.wait_for_operation()
        self.assertTrue((await self.p.webui_delete_comic('123', DAY, meta['revision']))['deleted'])
        self.assertEqual(self.p._report_store.list_comics()['total'], 0)
        self.assertTrue(self.p._report_store.detail('123', DAY)['has_image'])

    async def test_upload_ref_lifecycle_and_cap(self):
        self.p._comic_cfg().enabled = False
        with self.assertRaises(ValueError):
            await self.p.webui_upload_ref('upload-0')
        self.p._comic_cfg().enabled = True
        colors = ['white', 'red', 'green', 'blue', 'black']
        ref_ids = []
        for index, color in enumerate(colors):
            path = Path(self.temp.name) / f'upload-{index}.png'
            path.write_bytes(png_bytes(color))
            self.p.ctx.webui.claim_upload.return_value = {'path': str(path)}
            if index < 4:
                result = await self.p.webui_upload_ref(f'upload-{index}')
                ref_ids.append(result['ref_id'])
                self.assertFalse(path.exists())
            else:
                with self.assertRaises(ValueError):
                    await self.p.webui_upload_ref(f'upload-{index}')
        self.assertEqual(len(self.p._comic_refs()), 4)
        thumb = await self.p.webui_comic_ref_thumb(ref_ids[0])
        self.assertTrue(thumb['data_uri'].startswith('data:image/jpeg;base64,'))
        self.assertTrue((await self.p.webui_delete_ref(ref_ids[0]))['deleted'])
        self.assertFalse((await self.p.webui_delete_ref(ref_ids[0]))['deleted'])
        self.assertEqual(len(self.p._comic_refs()), 3)
        with self.assertRaises(ValueError):
            await self.p.webui_delete_ref('../stats.json')
        with self.assertRaises(ValueError):
            await self.p.webui_comic_ref_thumb('../stats.json')

    async def test_ref_preview_lifecycle_and_size_budget(self):
        with self.assertRaises(ValueError):
            await self.p.webui_comic_ref_preview('../stats.json')
        with self.assertRaises(ValueError):
            await self.p.webui_comic_ref_preview('0' * 16 + '.png')
        path = Path(self.temp.name) / 'upload-0.png'
        path.write_bytes(png_bytes('white'))
        self.p.ctx.webui.claim_upload.return_value = {'path': str(path)}
        ref_id = (await self.p.webui_upload_ref('upload-0'))['ref_id']
        preview = await self.p.webui_comic_ref_preview(ref_id)
        self.assertTrue(preview['data_uri'].startswith('data:image/jpeg;base64,'))
        self.assertEqual((preview['width'], preview['height']), (24, 48))

        big = io.BytesIO()
        Image.effect_noise((1200, 1200), 128).convert('RGB').save(big, format='PNG')
        path = Path(self.temp.name) / 'upload-1.png'
        path.write_bytes(big.getvalue())
        self.p.ctx.webui.claim_upload.return_value = {'path': str(path)}
        big_id = (await self.p.webui_upload_ref('upload-1'))['ref_id']
        preview = await self.p.webui_comic_ref_preview(big_id)
        self.assertEqual(preview['width'], preview['height'])
        self.assertLessEqual(preview['width'], 1024)
        self.assertLessEqual(len(base64.b64decode(preview['data_uri'].split(',', 1)[1])), 300 * 1024)

        oversize = self.p._comic_refs_dir() / ('f' * 16 + '.png')
        oversize.write_bytes(b'\0' * (8 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            await self.p.webui_comic_ref_preview(oversize.name)

    async def test_rename_ref_lifecycle(self):
        path = Path(self.temp.name) / 'upload-0.png'
        path.write_bytes(png_bytes('white'))
        self.p.ctx.webui.claim_upload.return_value = {'path': str(path)}
        ref_id = (await self.p.webui_upload_ref('upload-0'))['ref_id']
        self.assertEqual(self.p._comic_refs()[0]['name'], '')

        result = await self.p.webui_rename_ref(ref_id, '  麦麦角色立绘  ')
        self.assertTrue(result['renamed'])
        self.assertEqual(result['name'], '麦麦角色立绘')
        self.assertEqual(self.p._comic_refs()[0]['name'], '麦麦角色立绘')
        overview = await self.p.webui_comic_overview()
        self.assertEqual(overview['refs'][0]['name'], '麦麦角色立绘')

        self.assertTrue((await self.p.webui_rename_ref(ref_id, ''))['renamed'])
        self.assertEqual(self.p._comic_refs()[0]['name'], '')

        self.assertFalse((await self.p.webui_rename_ref('../stats.json', 'x'))['renamed'])
        self.assertFalse((await self.p.webui_rename_ref('0' * 16 + '.png', 'x'))['renamed'])
        self.assertFalse((await self.p.webui_rename_ref(ref_id, '坏\n名字'))['renamed'])
        long_name = '名' * 60
        self.assertEqual((await self.p.webui_rename_ref(ref_id, long_name))['name'], '名' * 40)

        await self.p.webui_rename_ref(ref_id, '待清理')
        self.assertTrue((await self.p.webui_delete_ref(ref_id))['deleted'])
        self.assertEqual(self.p._comic_ref_names(), {})

    async def test_rename_ref_survives_corrupt_sidecar(self):
        path = Path(self.temp.name) / 'upload-0.png'
        path.write_bytes(png_bytes('white'))
        self.p.ctx.webui.claim_upload.return_value = {'path': str(path)}
        ref_id = (await self.p.webui_upload_ref('upload-0'))['ref_id']
        (self.p._comic_refs_dir() / 'names.json').write_text('{broken', encoding='utf-8')
        self.assertEqual(self.p._comic_refs()[0]['name'], '')
        self.assertTrue((await self.p.webui_rename_ref(ref_id, '重建'))['renamed'])
        self.assertEqual(self.p._comic_refs()[0]['name'], '重建')

    async def test_album_file_stem_kind_and_day(self):
        self.assertEqual(self.p._album_file_stem('stream'), f'测试群-日报-{date.today().isoformat()}')
        self.assertEqual(self.p._album_file_stem('stream', '漫画', '2026-01-02'), '测试群-漫画-2026-01-02')
        self.assertEqual(self.p._album_file_stem('missing', '漫画', '2026-01-02'), '漫画-2026-01-02')

    async def test_comic_pipeline_album_upload_params(self):
        self.save_daily()
        ccfg = self.p._comic_cfg()
        ccfg.album_upload_enabled = True
        ccfg.album_name = '漫画专属相册'
        self.p._upload_report_to_album = AsyncMock()
        self.assertTrue((await self.p.webui_draw('123', DAY, 'four_panel'))['accepted'])
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'succeeded')
        self.p._upload_report_to_album.assert_awaited_once()
        call = self.p._upload_report_to_album.await_args
        self.assertEqual(call.args[:2], ('stream', '123'))
        self.assertEqual(call.kwargs['stem'], f'测试群-漫画-{DAY}')
        self.assertEqual(call.kwargs['album_name_override'], '漫画专属相册')
        self.assertIs(call.kwargs['enabled'], True)

    async def test_comic_resend_skips_album(self):
        self.save_daily()
        meta = self.save_comic()
        self.p._comic_cfg().album_upload_enabled = True
        self.p._upload_report_to_album = AsyncMock()
        self.assertTrue((await self.p.webui_comic_resend('123', DAY, meta['revision']))['accepted'])
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'succeeded')
        self.p._upload_report_to_album.assert_not_awaited()

    async def test_comic_album_switch_independent_of_daily(self):
        acfg = self.p._album_cfg()
        acfg.album_upload_enabled = False
        acfg.album_strict_mode = False
        self.p._napcat_api_retry = Mock(return_value={'data': [{'album_id': 'a1', 'name': '默认相册'}]})
        self.p._napcat_api = Mock(return_value={'retcode': 0})
        await self.p._upload_report_to_album('stream', '123', png_base64())
        self.p._napcat_api_retry.assert_not_called()
        await self.p._upload_report_to_album('stream', '123', png_base64(),
                                             stem='测试群-漫画-x', enabled=True)
        self.p._napcat_api_retry.assert_called()
        uploads = [c for c in self.p._napcat_api.call_args_list if c.args[0] == 'upload_image_to_qun_album']
        self.assertTrue(uploads)
        self.assertEqual(uploads[0].args[1]['album_id'], 'a1')

    async def test_comic_album_name_override_strict(self):
        acfg = self.p._album_cfg()
        acfg.album_upload_enabled = False
        acfg.album_strict_mode = True
        self.p._napcat_api_retry = Mock(return_value={'data': [{'album_id': 'a9', 'name': '漫画相册'}]})
        self.p._napcat_api = Mock(return_value={'retcode': 0})
        await self.p._upload_report_to_album('stream', '123', png_base64(),
                                             album_name_override='漫画相册', enabled=True)
        uploads = [c for c in self.p._napcat_api.call_args_list if c.args[0] == 'upload_image_to_qun_album']
        self.assertTrue(uploads)
        self.assertEqual(uploads[0].args[1]['album_id'], 'a9')
        self.assertEqual(uploads[0].args[1]['album_name'], '漫画相册')

    async def test_reference_images_passed_to_submit(self):
        self.save_daily()
        self.p._comic_cfg().use_reference_images = True
        path = Path(self.temp.name) / 'ref.png'
        path.write_bytes(png_bytes())
        self.p.ctx.webui.claim_upload.return_value = {'path': str(path)}
        ref = await self.p.webui_upload_ref('upload-1')
        await self.p.webui_draw('123')
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'succeeded')
        submit = self.api.kwargs_for('submit')[0]
        self.assertEqual(len(submit['source_images']), 1)
        self.assertIn('参考附图', submit['prompt'])
        self.assertEqual(self.book_entry()['ref_ids'], [ref['ref_id']])

    async def test_auto_comic_gating(self):
        self.save_daily()
        ccfg = self.p._comic_cfg()
        ccfg.auto_after_daily = True
        ccfg.groups = ''
        await self.p._maybe_auto_comic('stream', DAY)
        await self.drain()
        self.assertEqual(self.p._jobs.snapshot(), [])
        ccfg.groups = '456'
        await self.p._maybe_auto_comic('stream', DAY)
        await self.drain()
        self.assertEqual(self.p._jobs.snapshot(), [])
        ccfg.groups = '123'
        ccfg.auto_after_daily = False
        await self.p._maybe_auto_comic('stream', DAY)
        await self.drain()
        self.assertEqual(self.p._jobs.snapshot(), [])

    async def test_auto_comic_submits_after_daily(self):
        self.save_daily()
        ccfg = self.p._comic_cfg()
        ccfg.auto_after_daily = True
        ccfg.groups = '123\n'
        await self.p._maybe_auto_comic('stream', DAY)
        await self.drain()
        job = self.operation()
        self.assertEqual((job['source'], job['status']), ('auto', 'succeeded'))
        self.assertTrue(self.p._report_store.comic_detail('123', DAY)['sent'])

    async def test_auto_comic_skips_without_archive_or_drawpic(self):
        ccfg = self.p._comic_cfg()
        ccfg.auto_after_daily = True
        ccfg.groups = '123'
        await self.p._maybe_auto_comic('stream', DAY)
        await self.drain()
        self.assertEqual(self.p._jobs.snapshot(), [])
        self.save_daily()
        self.api.available = False
        await self.p._maybe_auto_comic('stream', DAY)
        await self.drain()
        self.assertEqual(self.p._jobs.snapshot(), [])

    async def test_cmd_comic_gates_and_ack(self):
        kwargs = {'stream_id': 'stream', 'user_id': '10001',
                  'message': {'message_info': {'group_info': {'group_id': '123'}}}}
        self.p._cfg().report.command_admins = '999'
        handled, reason, _ = await self.p.cmd_comic(**kwargs)
        self.assertEqual((handled, reason), (True, '非管理员'))
        self.p._cfg().report.command_admins = ''
        self.p._cfg().report.chat_whitelist = '456'
        self.assertEqual((await self.p.cmd_comic(**kwargs))[1], '不在白名单')
        self.p._cfg().report.chat_whitelist = ''
        self.p._comic_cfg().enabled = False
        self.assertEqual((await self.p.cmd_comic(**kwargs))[1], '漫画未启用')
        self.p._comic_cfg().enabled = True
        self.assertEqual((await self.p.cmd_comic(**kwargs))[1], '缺少日报存档')
        self.save_daily()
        self.api.available = False
        self.p._drawpic_probe = None
        self.assertEqual((await self.p.cmd_comic(**kwargs))[1], '绘图插件不可用')
        self.api.available = True
        self.p._drawpic_probe = None
        self.api.task_gate = asyncio.Event()
        self.api.task_results = [{'task_id': 'task-1', 'status': 'running', 'error': None}]
        handled, reason, _ = await self.p.cmd_comic(**kwargs)
        self.assertEqual((handled, reason), (True, '漫画生成中'))
        self.assertEqual((await self.p.cmd_comic(**kwargs))[1], '任务忙碌')
        await self.p.webui_cancel('123')
        await self.wait_for_operation()
        self.assertEqual((self.operation()['source'], self.operation()['status']), ('command', 'cancelled'))

    async def test_resolve_analysis_names(self):
        resolved = module.GroupDailyAnalysisPlugin._resolve_analysis_names(ANALYSIS, {'10001': '小明'})
        self.assertEqual(resolved['quotes'][0]['sender'], '小明')
        self.assertEqual(resolved['quotes'][0]['content'], '小明 的金句')
        self.assertEqual(resolved['topics'][0]['detail'], '小明 主导了讨论')
        untouched = module.GroupDailyAnalysisPlugin._resolve_analysis_names(ANALYSIS, {})
        self.assertEqual(untouched['quotes'][0]['sender'], '[10001]')

    async def test_comic_overview_shape(self):
        self.save_comic()
        self.p._comic_book.record('comic-abcdef123456', '123', DAY, 'four_panel', 'p')
        self.p._comic_book.record('comic-fedcba654321', '123', DAY, 'four_panel', 'p')
        self.p._comic_book.update('comic-fedcba654321', status='completed', sent=True)
        overview = await self.p.webui_comic_overview()
        self.assertTrue(overview['enabled'])
        self.assertTrue(overview['drawpic_available'])
        self.assertEqual(overview['total'], 1)
        self.assertEqual(len(overview['styles']), 2)
        self.assertEqual(overview['default_style'], 'four_panel')
        self.assertEqual([row['request_id'] for row in overview['recoverable']], ['comic-abcdef123456'])
        self.assertEqual(overview['groups'], [{'group_id': '123', 'group_name': '测试群'}])
        self.assertEqual(overview['operations'], [])
        self.assertLess(len(json.dumps(overview).encode()), 512 * 1024)
        self.p._comic_cfg().enabled = False
        self.p._drawpic_probe = None
        overview = await self.p.webui_comic_overview()
        self.assertFalse(overview['drawpic_available'])
        self.assertEqual(self.api.calls, [])

    async def test_failed_send_keeps_archive_resendable(self):
        self.save_daily()
        self.p.ctx.send.image.return_value = False
        await self.p.webui_draw('123')
        await self.wait_for_operation()
        self.assertEqual(self.operation()['status'], 'failed')
        detail = self.p._report_store.comic_detail('123', DAY)
        self.assertFalse(detail['sent'])
        self.assertEqual(self.book_entry()['status'], 'completed')
        self.assertFalse(self.book_entry()['sent'])
        self.p.ctx.send.image.return_value = True
        self.assertTrue((await self.p.webui_comic_resend('123', DAY, detail['revision']))['accepted'])
        await self.wait_for_operation()
        self.assertTrue(self.p._report_store.comic_detail('123', DAY)['sent'])


if __name__ == '__main__':
    unittest.main()