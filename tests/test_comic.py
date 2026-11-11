import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('comic_module', ROOT / 'comic.py')
comic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comic)

ANALYSIS = {
    'topics': [{'topic': '今日话题', 'contributors': ['甲'], 'detail': '大家聊得很热闹'}],
    'titles': [{'user_id': 'u1', 'name': '甲', 'title': '水群之王', 'mbti': '', 'reason': ''}],
    'quotes': [{'content': '金句一句', 'sender': '[u1]', 'reason': '好笑'}],
    'quality': {'title': '锐评', 'subtitle': '', 'dimensions': [], 'summary': '热闹的一天'},
}


class FakeApi:
    """按 drawpic 2.2.0 信封协议回放脚本化响应；记录全部调用供断言。"""

    def __init__(self, blob=b'fake-image-bytes', chunk_size=7):
        self.calls = []
        self.blob = blob
        self.chunk_size = chunk_size
        self.submit_result = {'accepted': True, 'task_id': 'task-1', 'request_id': '', 'reused': False}
        self.task_result = {'task_id': 'task-1', 'status': 'completed', 'error': None, 'retry_after_ms': 0}
        self.request_result = {'task_id': 'task-1', 'status': 'completed', 'error': None}
        self.fail_envelope = False
        self.raise_transport = False

    async def call(self, name, *, version='', **kwargs):
        self.calls.append((name, kwargs))
        assert version == '2'
        assert name.startswith('maimai-drawpic-plugin.drawpic_') and name.endswith('_v2')
        if self.raise_transport:
            raise RuntimeError('transport down')
        if self.fail_envelope:
            return {'success': False, 'error': 'host error'}
        operation = name[len('maimai-drawpic-plugin.drawpic_'):-len('_v2')]
        if operation == 'submit':
            result = dict(self.submit_result)
            result['request_id'] = kwargs['request_id']
            return {'success': True, 'result': result}
        if operation == 'task':
            return {'success': True, 'result': dict(self.task_result)}
        if operation == 'request':
            return {'success': True, 'result': dict(self.request_result)}
        if operation == 'cancel':
            return {'success': True, 'result': {'task_id': kwargs['task_id'], 'status': 'cancelled',
                                                'cancel_requested': True, 'upstream_cancelled': None}}
        if operation == 'image':
            offset = kwargs['offset']
            block = self.blob[offset:offset + self.chunk_size]
            next_offset = offset + len(block)
            return {'success': True, 'result': {
                'data': base64.b64encode(block).decode('ascii'), 'offset': offset,
                'next_offset': next_offset, 'size': len(self.blob),
                'done': next_offset == len(self.blob), 'mime': 'image/png', 'error': None}}
        raise AssertionError(f'unexpected operation {operation}')


class DrawpicClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_submit_passes_protocol_fields_and_empty_stream(self):
        api = FakeApi()
        client = comic.DrawpicClient(api)
        result = await client.submit('提示词', 'comic-abcdef123456', model='实例 / 模型',
                                     style='水彩', source_images=['aGVsbG8='])
        self.assertTrue(result['accepted'])
        name, kwargs = api.calls[0]
        self.assertEqual(name, 'maimai-drawpic-plugin.drawpic_submit_v2')
        self.assertEqual(kwargs['stream_id'], '')
        self.assertEqual(kwargs['task_type'], 'draw')
        self.assertEqual(kwargs['model'], '实例 / 模型')
        self.assertEqual(kwargs['style'], '水彩')
        self.assertEqual(kwargs['source_images'], ['aGVsbG8='])

    async def test_submit_omits_optional_fields_when_empty(self):
        api = FakeApi()
        await comic.DrawpicClient(api).submit('提示词', 'comic-abcdef123456')
        _, kwargs = api.calls[0]
        self.assertNotIn('model', kwargs)
        self.assertNotIn('style', kwargs)
        self.assertNotIn('source_images', kwargs)

    async def test_submit_rejects_bad_prompt_and_request_id_before_rpc(self):
        api = FakeApi()
        client = comic.DrawpicClient(api)
        for prompt, request_id in [('', 'comic-abcdef123456'), ('x' * 16001, 'comic-abcdef123456'),
                                   ('提示词', 'short'), ('提示词', '非法键!')]:
            with self.subTest(request_id=request_id), self.assertRaises(comic.DrawpicError) as caught:
                await client.submit(prompt, request_id)
            self.assertEqual(caught.exception.code, 'INVALID_REQUEST')
        self.assertEqual(api.calls, [])

    async def test_not_accepted_raises_structured_business_error(self):
        api = FakeApi()
        api.submit_result = {'accepted': False, 'task_id': None, 'status': 'not_accepted',
                             'error': {'code': 'CONCURRENCY_LIMIT', 'message': '4 个后台任务已满',
                                       'retry_query': False, 'retry_submit': True}}
        with self.assertRaises(comic.DrawpicError) as caught:
            await comic.DrawpicClient(api).submit('提示词', 'comic-abcdef123456')
        self.assertEqual(caught.exception.code, 'CONCURRENCY_LIMIT')
        self.assertTrue(caught.exception.retry_submit)
        self.assertFalse(caught.exception.retry_query)

    async def test_transport_and_envelope_failures_are_submission_unknown(self):
        for mode in ('raise_transport', 'fail_envelope'):
            api = FakeApi()
            setattr(api, mode, True)
            with self.subTest(mode=mode), self.assertRaises(comic.DrawpicError) as caught:
                await comic.DrawpicClient(api).submit('提示词', 'comic-abcdef123456')
            self.assertEqual(caught.exception.code, 'RPC_FAILED')

    async def test_task_request_cancel_surface_business_errors(self):
        api = FakeApi()
        api.task_result = {'error': {'code': 'TASK_NOT_FOUND', 'message': '记录不存在', 'retry_query': False}}
        client = comic.DrawpicClient(api)
        with self.assertRaises(comic.DrawpicError) as caught:
            await client.task('task-1')
        self.assertEqual(caught.exception.code, 'TASK_NOT_FOUND')
        api.request_result = {'error': {'code': 'REQUEST_NOT_FOUND', 'message': ''}}
        with self.assertRaises(comic.DrawpicError) as caught:
            await client.request('comic-abcdef123456')
        self.assertEqual(caught.exception.code, 'REQUEST_NOT_FOUND')
        await client.cancel('task-1')
        self.assertEqual(api.calls[-1][0], 'maimai-drawpic-plugin.drawpic_cancel_v2')

    async def test_read_image_reassembles_chunks(self):
        blob = bytes(range(256)) * 3
        api = FakeApi(blob=blob, chunk_size=100)
        result = await comic.DrawpicClient(api).read_image('task-1')
        self.assertEqual(result, blob)
        image_calls = [kwargs for name, kwargs in api.calls if name.endswith('drawpic_image_v2')]
        self.assertEqual([kwargs['offset'] for kwargs in image_calls], [0, 100, 200, 300, 400, 500, 600, 700])
        self.assertTrue(all(kwargs['kind'] == 'original' and kwargs['index'] == 0 for kwargs in image_calls))

    async def test_read_image_rejects_inconsistent_chunks(self):
        blob = b'0123456789abcdef'
        cases = {
            'size_changed': lambda result: result.update(size=len(blob) + 1),
            'offset_gap': lambda result: result.update(offset=result['offset'] + 1),
            'bad_base64': lambda result: result.update(data='!!!'),
            'length_mismatch': lambda result: result.update(next_offset=result['next_offset'] + 1),
            'done_flag': lambda result: result.update(done=not result['done']),
        }
        for label, mutate in cases.items():
            api = FakeApi(blob=blob, chunk_size=8)
            original_call = api.call

            async def mutating_call(name, *, version='', _mutate=mutate, **kwargs):
                response = await original_call(name, version=version, **kwargs)
                if name.endswith('drawpic_image_v2') and kwargs['offset'] == 8:
                    _mutate(response['result'])
                return response

            api.call = mutating_call
            with self.subTest(case=label), self.assertRaises(comic.DrawpicError) as caught:
                await comic.DrawpicClient(api).read_image('task-1')
            self.assertEqual(caught.exception.code, 'RESULT_UNAVAILABLE')

    async def test_read_image_enforces_local_caps(self):
        api = FakeApi(blob=b'x' * 64, chunk_size=8)
        with self.assertRaises(comic.DrawpicError) as caught:
            await comic.DrawpicClient(api).read_image('task-1', max_bytes=32)
        self.assertEqual(caught.exception.code, 'RESULT_UNAVAILABLE')
        api = FakeApi(blob=b'x' * (comic.MAX_CHUNKS * 2 + 2), chunk_size=2)
        with self.assertRaises(comic.DrawpicError) as caught:
            await comic.DrawpicClient(api).read_image('task-1')
        self.assertEqual(caught.exception.code, 'RESULT_TOO_LARGE')

    async def test_new_request_id_matches_protocol_shape(self):
        for _ in range(20):
            self.assertTrue(comic.REQUEST_ID_RE.fullmatch(comic.new_request_id()))


class ComicRequestBookTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'comic_requests.json'
        self.book = comic.ComicRequestBook(self.path)

    def record(self, request_id, group='123', day='2026-11-11', **kwargs):
        self.book.record(request_id, group, day, 'four_panel', '提示词', **kwargs)

    def test_record_persists_before_submit_and_reloads(self):
        self.record('comic-abcdef123456', group_name='测试群', storyboard={'title': 't'})
        entry = self.book.get('comic-abcdef123456')
        self.assertEqual(entry['status'], 'submitting')
        self.assertFalse(entry['sent'])
        self.assertEqual(entry['task_id'], '')
        reopened = comic.ComicRequestBook(self.path)
        self.assertEqual(reopened.get('comic-abcdef123456'), entry)

    def test_update_only_touches_whitelisted_fields(self):
        self.record('comic-abcdef123456')
        self.book.update('comic-abcdef123456', task_id='task-1', status='running', sent=True,
                         error='x', prompt='hacked', group_id='999')
        entry = self.book.get('comic-abcdef123456')
        self.assertEqual((entry['task_id'], entry['status'], entry['sent'], entry['error']),
                         ('task-1', 'running', True, 'x'))
        self.assertEqual(entry['prompt'], '提示词')
        self.assertEqual(entry['group_id'], '123')
        self.book.update('comic-missing00000', status='running')

    def test_latest_and_recoverable_filters(self):
        self.record('comic-aaaaaaaaaaaa', day='2026-11-10')
        self.record('comic-bbbbbbbbbbbb')
        self.book.update('comic-bbbbbbbbbbbb', status='running')
        self.record('comic-cccccccccccc')
        self.book.update('comic-cccccccccccc', status='failed', error='x')
        self.record('comic-dddddddddddd')
        self.book.update('comic-dddddddddddd', status='completed', sent=True)
        latest = self.book.latest('123', '2026-11-11')
        self.assertEqual(latest['request_id'], 'comic-dddddddddddd')
        recoverable = {entry['request_id'] for entry in self.book.recoverable()}
        self.assertEqual(recoverable, {'comic-aaaaaaaaaaaa', 'comic-bbbbbbbbbbbb', 'comic-dddddddddddd'})
        self.assertEqual(self.book.recoverable(day='2026-11-10')[0]['request_id'], 'comic-aaaaaaaaaaaa')
        self.assertIsNone(self.book.latest('456', '2026-11-11'))

    def test_prune_keeps_active_entries_and_caps_terminal(self):
        for index in range(comic.BOOK_CAP + 10):
            request_id = f'comic-{index:012d}'
            self.record(request_id)
            self.book.update(request_id, status='failed')
        self.record('comic-active000000')
        self.book.update('comic-active000000', status='running')
        self.assertEqual(len(self.book.recoverable()), 1)
        self.assertIsNotNone(self.book.get('comic-active000000'))
        self.assertIsNone(self.book.get('comic-000000000000'))
        self.assertLessEqual(len(json.loads(self.path.read_text(encoding='utf-8'))), comic.BOOK_CAP)

    def test_latest_prefers_newest_within_same_second_and_after_reload(self):
        self.record('comic-aaaaaaaaaaaa')
        self.record('comic-bbbbbbbbbbbb')
        self.assertEqual(self.book.latest('123', '2026-11-11')['request_id'], 'comic-bbbbbbbbbbbb')
        reopened = comic.ComicRequestBook(self.path)
        reopened.record('comic-cccccccccccc', '123', '2026-11-11', 'four_panel', 'p')
        self.assertEqual(reopened.latest('123', '2026-11-11')['request_id'], 'comic-cccccccccccc')

    def test_corrupt_book_file_starts_empty(self):
        self.path.write_text('{invalid json', encoding='utf-8')
        book = comic.ComicRequestBook(self.path)
        self.assertEqual(book.recoverable(), [])
        book.record('comic-abcdef123456', '123', '2026-11-11', 'four_panel', 'p')
        self.assertIsNotNone(comic.ComicRequestBook(self.path).get('comic-abcdef123456'))

    def test_prompt_and_fields_are_bounded(self):
        self.book.record('comic-abcdef123456', '123', '2026-11-11', 'four_panel', 'x' * 20000,
                         group_name='n' * 500, ref_ids=['r'] * 20)
        entry = self.book.get('comic-abcdef123456')
        self.assertEqual(len(entry['prompt']), comic.BOOK_PROMPT_LIMIT)
        self.assertEqual(len(entry['group_name']), 200)
        self.assertEqual(len(entry['ref_ids']), 8)


class StoryboardTest(unittest.TestCase):
    def test_fallback_uses_topics_and_quotes(self):
        board = comic.fallback_storyboard(ANALYSIS, '测试群', 4)
        self.assertEqual(len(board['panels']), 4)
        self.assertIn('今日话题', board['panels'][0]['caption'])
        self.assertEqual(board['panels'][1]['dialogue'], '金句一句')

    def test_fallback_empty_analysis_still_produces_panels(self):
        for style, count in (('four_panel', 4), ('poster', 1)):
            board = comic.fallback_storyboard({}, '测试群', count)
            self.assertEqual(len(board['panels']), count)
            self.assertTrue(all(panel['scene'] for panel in board['panels']))

    def test_normalize_accepts_valid_and_truncates(self):
        data = {'title': 't' * 100, 'panels': [
            {'scene': 's' * 500, 'caption': 'c' * 200, 'dialogue': 'd' * 300},
            {'scene': '第二格'}, {'scene': ''}, 'not-a-dict', {'scene': '多余第五格'},
        ]}
        board = comic.normalize_storyboard(data, 'four_panel', ANALYSIS, '测试群')
        self.assertEqual(len(board['title']), 60)
        self.assertEqual(len(board['panels']), 2)
        self.assertEqual(len(board['panels'][0]['scene']), 400)
        self.assertEqual(len(board['panels'][0]['caption']), 120)
        self.assertEqual(len(board['panels'][0]['dialogue']), 200)

    def test_normalize_invalid_falls_back(self):
        for data in (None, 'text', [], {}, {'panels': [{'caption': '无场景'}]}):
            board = comic.normalize_storyboard(data, 'poster', ANALYSIS, '测试群')
            self.assertEqual(len(board['panels']), 1)
            self.assertTrue(board['panels'][0]['scene'])

    def test_storyboard_prompt_mentions_constraints(self):
        prompt = comic.storyboard_prompt(ANALYSIS, '测试群', '2026-11-11', 'four_panel')
        for fragment in ('恰好 4 个画面', '不可信输入', '纯 JSON', '今日话题', '金句一句', '不得出现 QQ 号'):
            self.assertIn(fragment, prompt)
        self.assertIn('单幅海报', comic.storyboard_prompt(ANALYSIS, '测试群', '2026-11-11', 'poster'))

    def test_build_draw_prompt_styles_refs_and_cap(self):
        board = comic.fallback_storyboard(ANALYSIS, '测试群', 4)
        four = comic.build_draw_prompt(board, 'four_panel', '测试群', '2026-11-11')
        self.assertIn('四格漫画', four)
        self.assertIn('第4格', four)
        self.assertNotIn('参考附图', four)
        poster = comic.build_draw_prompt(board, 'poster', '测试群', '2026-11-11', with_refs=True)
        self.assertIn('单幅竖版海报', poster)
        self.assertIn('参考附图', poster)
        huge = {'title': 't', 'panels': [{'scene': 's' * 20000}]}
        self.assertLessEqual(len(comic.build_draw_prompt(huge, 'poster', '群', '2026-11-11')), 16000)


if __name__ == '__main__':
    unittest.main()