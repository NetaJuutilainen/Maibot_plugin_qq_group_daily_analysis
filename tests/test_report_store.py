import base64
from datetime import date, timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

spec = importlib.util.spec_from_file_location('daily_report_store', Path(__file__).resolve().parents[1] / 'report_store.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ReportStore = module.ReportStore
DAY = date.today().isoformat()


def encoded_image(fmt='PNG', noise=False):
    image = Image.effect_noise((1000, 1000), 100).convert('RGB') if noise else Image.new('RGB', (32, 64), 'white')
    output = io.BytesIO()
    image.save(output, format=fmt)
    return base64.b64encode(output.getvalue()).decode('ascii')


class ReportStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.store = ReportStore(self.data)

    def save(self, group='123', day=DAY, image=None, context=None, **meta):
        return self.store.save(group, day, encoded_image() if image is None else image, '<html>synthetic</html>',
                               {'group_name': '测试群', 'theme': 'atri', 'message_count': 20, 'participant_count': 3, **meta},
                               context)

    def test_query_has_no_write_side_effect(self):
        self.assertEqual(self.store.list_reports()['total'], 0)
        self.assertFalse(self.store.root.exists())
        with self.assertRaises(FileNotFoundError):
            self.store.detail('123', DAY)
        self.assertFalse(self.store.root.exists())

    def test_persistence_and_metadata_projection(self):
        meta = self.save(analysis={'topics': ['话题']}, sample='private raw sample')
        reopened = ReportStore(self.data)
        self.assertEqual(reopened.detail('123', DAY), meta)
        self.assertNotIn('sample', meta)
        rows = reopened.list_reports()
        self.assertEqual(rows['total'], 1)
        self.assertNotIn('analysis', rows['items'][0])
        self.assertEqual(rows['groups'], [{'group_id': '123', 'group_name': '测试群'}])
        self.assertEqual(reopened.read_bytes('123', DAY, 'image'), base64.b64decode(encoded_image()))

    def test_jpeg_converted_to_png_and_thumbnail_bounded(self):
        self.save(image=encoded_image('JPEG', noise=True))
        original = self.store.read_bytes('123', DAY, 'image')
        self.assertTrue(original.startswith(b'\x89PNG\r\n\x1a\n'))
        uri = self.store.thumbnail('123', DAY)['data_uri']
        self.assertLessEqual(len(uri.encode()), 20 * 1024)
        with Image.open(io.BytesIO(base64.b64decode(uri.split(',')[1]))) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertLessEqual(max(image.size), 320)

    def test_html_only_replaces_image_without_leftovers(self):
        old = self.save()
        new = self.save(image='')
        self.assertNotEqual(old['revision'], new['revision'])
        self.assertFalse(new['has_image'])
        self.assertFalse((self.store.root / '123' / DAY / 'report.png').exists())
        with self.assertRaises(ValueError):
            self.store.thumbnail('123', DAY)
        self.assertIn(b'synthetic', self.store.read_bytes('123', DAY, 'html'))

    def test_chunk_reassembly_and_payload_budget(self):
        meta = self.save(image=encoded_image(noise=True))
        offset = 0
        blocks = []
        while True:
            chunk = self.store.read_chunk('123', DAY, offset=offset, revision=meta['revision'])
            self.assertLessEqual(len(chunk['data']), 384 * 1024)
            self.assertLess(len(json.dumps(chunk).encode()), 512 * 1024)
            self.assertEqual(chunk['offset'], offset)
            self.assertEqual(chunk['mime'], 'image/png')
            blocks.append(base64.b64decode(chunk['data']))
            offset = chunk['next_offset']
            if chunk['done']:
                break
        self.assertGreater(len(blocks), 1)
        self.assertEqual(b''.join(blocks), self.store.read_bytes('123', DAY, 'image'))
        self.assertEqual(self.store.read_chunk('123', DAY, offset=offset)['data'], '')
        for bad in [-1, offset + 1, True, 0.0, '0']:
            with self.subTest(offset=bad), self.assertRaises(ValueError):
                self.store.read_chunk('123', DAY, offset=bad)

    def test_stale_revision_rejected_everywhere(self):
        old = self.save()
        self.save()
        for method, args in [(self.store.thumbnail, ()), (self.store.read_chunk, ()),
                             (self.store.read_bytes, ('image',)), (self.store.delete, ())]:
            with self.subTest(method=method), self.assertRaises(ValueError):
                method('123', DAY, *args, revision=old['revision'])
        self.assertEqual(self.store.list_reports()['total'], 1)

    def test_filters_pagination_and_recent_regeneration(self):
        for index in range(15):
            self.save(day=(date.today() - timedelta(days=index)).isoformat())
        self.save(group='456')
        rows = self.store.list_reports()
        self.assertEqual((len(rows['items']), rows['total'], rows['pages']), (12, 16, 2))
        self.assertEqual(len(self.store.list_reports(page=2)['items']), 4)
        self.assertEqual(self.store.list_reports(group_id='456')['total'], 1)
        self.assertEqual(self.store.list_reports(date_from=DAY, date_to=DAY)['total'], 2)
        old_day = (date.today() - timedelta(days=14)).isoformat()
        self.save(day=old_day)
        self.assertEqual(self.store.list_reports()['items'][0]['day'], old_day)
        for kwargs in [{'page': 0}, {'page': True}, {'page_size': 13}, {'date_from': DAY, 'date_to': '2020-01-01'}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.list_reports(**kwargs)

    def test_search_type_and_sort(self):
        self.save(group='123', theme='atri')
        self.save(group='456', day=(date.today() - timedelta(days=1)).isoformat(), image='', theme='simple')
        self.assertEqual(self.store.list_reports(search='456')['total'], 1)
        self.assertEqual(self.store.list_reports(search='SIMPLE')['total'], 1)
        self.assertEqual(self.store.list_reports(report_type='html')['total'], 1)
        self.assertEqual(self.store.list_reports(report_type='image')['total'], 1)
        self.assertEqual(self.store.list_reports(sort_order='asc')['items'][0]['group_id'], '123')
        self.assertEqual(self.store.list_reports(sort_order='desc')['items'][0]['group_id'], '456')
        for kwargs in [{'search': 'x' * 101}, {'report_type': 'comic'}, {'sort_order': 'up'}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.list_reports(**kwargs)

    def test_context_roundtrip_and_cap(self):
        meta = self.save(context={'sample': [{'t': '你好'}]})
        self.assertTrue(meta['has_context'])
        self.assertEqual(self.store.read_context('123', DAY)['sample'][0]['t'], '你好')
        oversized = self.save(context={'blob': 'x' * (ReportStore.MAX_CONTEXT_BYTES + 1)})
        self.assertFalse(oversized['has_context'])
        with self.assertRaises(ValueError):
            self.store.read_context('123', DAY)
        self.assertFalse(self.save()['has_context'])

    def test_path_traversal_devices_dates_and_kinds(self):
        for group in ['../123', 'a/b', 'a\\b', '.', '', 'a:b', 'CON', 'lpt1', 'x' * 129]:
            with self.subTest(group=group), self.assertRaises(ValueError):
                self.save(group=group)
        for day in ['20261110', '2026-02-30', '../2026-01-01', '2026-1-01']:
            with self.subTest(day=day), self.assertRaises(ValueError):
                self.save(day=day)
        self.save()
        with self.assertRaises(ValueError):
            self.store.read_chunk('123', DAY, kind='../../stats.json')

    def test_corrupt_metadata_hidden_and_not_deleted(self):
        self.save()
        path = self.store.root / '123' / DAY / 'meta.json'
        path.write_text('{invalid', encoding='utf-8')
        self.assertEqual(self.store.list_reports()['total'], 0)
        with self.assertRaises(ValueError):
            self.store.delete('123', DAY)
        self.assertTrue(path.exists())

    def test_oversized_metadata_and_invalid_image_preserve_old_report(self):
        meta = self.save()
        for kwargs in [{'analysis': 'x' * (128 * 1024)}, {'image': 'not-base64'}, {'image': encoded_image('GIF')}]:
            with self.subTest(kwargs=list(kwargs)), self.assertRaises((ValueError, OSError)):
                self.save(**kwargs)
        self.assertEqual(self.store.detail('123', DAY)['revision'], meta['revision'])

    def test_atomic_replace_failure_restores_previous_revision(self):
        old = self.save()
        replace = os.replace
        calls = []
        def fail_install(source, target):
            calls.append((source, target))
            if len(calls) == 2:
                raise OSError('simulated installation failure')
            replace(source, target)
        with patch.object(module.os, 'replace', side_effect=fail_install), self.assertRaises(OSError):
            self.save()
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.store.detail('123', DAY)['revision'], old['revision'])
        self.assertEqual([path.name for path in (self.store.root / '123').iterdir()], [DAY])

    def test_retention_and_delete_do_not_touch_other_plugin_data(self):
        stats = self.data / 'stats.json'
        stats.write_text('keep', encoding='utf-8')
        old_day = (date.today() - timedelta(days=91)).isoformat()
        self.save(day=old_day)
        self.save(day=(date.today() - timedelta(days=90)).isoformat())
        current = self.save()
        self.assertEqual(self.store.cleanup(0), 0)
        self.assertEqual(self.store.cleanup(), 1)
        self.assertEqual(self.store.list_reports()['total'], 2)
        self.assertTrue(self.store.delete('123', DAY, current['revision'])['deleted'])
        self.assertFalse(self.store.delete('123', DAY)['deleted'])
        self.assertEqual(stats.read_text(), 'keep')

    def test_directory_link_is_not_followed(self):
        outside = self.data / 'outside'
        outside.mkdir()
        marker = outside / 'keep.txt'
        marker.write_text('keep', encoding='utf-8')
        self.store.root.mkdir()
        link = self.store.root / '123'
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
            if result.returncode:
                self.skipTest('junction creation unavailable')
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(ValueError):
                self.save()
            with self.assertRaises(ValueError):
                self.store.delete('123', DAY)
            self.assertEqual(self.store.list_reports()['total'], 0)
            self.assertEqual(marker.read_text(), 'keep')
        finally:
            if os.name == 'nt':
                link.rmdir()
            else:
                link.unlink()

    def test_root_link_is_rejected(self):
        with patch.object(module.Path, 'lstat') as mocked:
            mocked.return_value = type('Stat', (), {'st_mode': 0, 'st_file_attributes': 0x400})()
            with self.assertRaises(ValueError):
                ReportStore(self.data).list_reports()


class ComicStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.store = ReportStore(self.data)

    def save_report(self, group='123', day=DAY):
        return self.store.save(group, day, encoded_image(), '<html>synthetic</html>',
                               {'group_name': '测试群', 'theme': 'atri', 'message_count': 20})

    def save_comic(self, group='123', day=DAY, **meta):
        return self.store.save_comic(group, day, encoded_image(), {
            'group_name': '测试群', 'style': 'four_panel', 'request_id': 'comic-abcdef123456',
            'task_id': 'task-1', 'prompt': '绘图提示词', 'storyboard': {'title': 't', 'panels': []},
            'message_count': 20, 'version': '1.5.0', **meta})

    def test_save_comic_writes_three_files_and_summary_projection(self):
        meta = self.save_comic()
        directory = self.store.root / '123' / DAY
        for name in ReportStore.COMIC_FILES:
            self.assertTrue((directory / name).is_file())
        rows = self.store.list_comics()
        self.assertEqual(rows['total'], 1)
        item = rows['items'][0]
        self.assertEqual(set(item), set(ReportStore.COMIC_SUMMARY_FIELDS))
        self.assertFalse(item['sent'])
        detail = self.store.comic_detail('123', DAY)
        self.assertEqual(detail['prompt'], '绘图提示词')
        self.assertEqual(detail['storyboard']['title'], 't')

    def test_save_comic_requires_valid_request_id_and_image(self):
        with self.assertRaises(ValueError):
            self.save_comic(request_id='bad')
        with self.assertRaises((ValueError, OSError)):
            self.store.save_comic('123', DAY, 'not-base64', {'request_id': 'comic-abcdef123456'})
        self.assertEqual(self.store.list_comics()['total'], 0)

    def test_report_regeneration_preserves_comic(self):
        comic = self.save_comic()
        self.save_report()
        self.save_report()
        self.assertEqual(self.store.comic_detail('123', DAY)['revision'], comic['revision'])
        self.assertEqual(self.store.list_comics()['total'], 1)

    def test_report_delete_removes_comic_too(self):
        self.save_report()
        self.save_comic()
        self.assertTrue(self.store.delete('123', DAY)['deleted'])
        self.assertEqual(self.store.list_comics()['total'], 0)
        self.assertFalse((self.store.root / '123' / DAY).exists())

    def test_delete_comic_keeps_report_and_requires_revision(self):
        report = self.save_report()
        comic = self.save_comic()
        with self.assertRaises(ValueError):
            self.store.delete_comic('123', DAY, revision='0' * 32)
        self.assertTrue(self.store.delete_comic('123', DAY, comic['revision'])['deleted'])
        self.assertFalse(self.store.delete_comic('123', DAY)['deleted'])
        self.assertEqual(self.store.detail('123', DAY)['revision'], report['revision'])
        directory = self.store.root / '123' / DAY
        for name in ReportStore.COMIC_FILES:
            self.assertFalse((directory / name).exists())
        self.assertTrue((directory / 'meta.json').is_file())

    def test_mark_sent_and_stale_revision_guards(self):
        old = self.save_comic()
        current = self.save_comic()
        for method, args in [(self.store.comic_thumbnail, ()), (self.store.read_comic_bytes, ()),
                             (self.store.comic_chunk, ()), (self.store.mark_comic_sent, ()),
                             (self.store.delete_comic, ())]:
            with self.subTest(method=method), self.assertRaises(ValueError):
                method('123', DAY, *args, revision=old['revision'])
        marked = self.store.mark_comic_sent('123', DAY, current['revision'])
        self.assertTrue(marked['sent'])
        self.assertTrue(self.store.comic_detail('123', DAY)['sent'])
        self.assertTrue(self.store.list_comics()['items'][0]['sent'])

    def test_comic_chunk_reassembly_and_thumbnail(self):
        meta = self.save_comic()
        offset = 0
        blocks = []
        while True:
            chunk = self.store.comic_chunk('123', DAY, offset=offset, revision=meta['revision'])
            self.assertEqual(chunk['mime'], 'image/png')
            self.assertEqual(chunk['filename'], f'123-{DAY}-comic.png')
            blocks.append(base64.b64decode(chunk['data']))
            offset = chunk['next_offset']
            if chunk['done']:
                break
        self.assertEqual(b''.join(blocks), self.store.read_comic_bytes('123', DAY))
        uri = self.store.comic_thumbnail('123', DAY)['data_uri']
        self.assertTrue(uri.startswith('data:image/jpeg;base64,'))
        self.assertLessEqual(len(uri.encode()), 20 * 1024)
        for bad in [-1, offset + 1, True, '0']:
            with self.subTest(offset=bad), self.assertRaises(ValueError):
                self.store.comic_chunk('123', DAY, offset=bad)

    def test_list_comics_filters_pagination_and_search(self):
        for index in range(15):
            self.save_comic(day=(date.today() - timedelta(days=index)).isoformat())
        self.save_comic(group='456')
        rows = self.store.list_comics()
        self.assertEqual((len(rows['items']), rows['total'], rows['pages']), (12, 16, 2))
        self.assertEqual(self.store.list_comics(group_id='456')['total'], 1)
        self.assertEqual(self.store.list_comics(date_from=DAY, date_to=DAY)['total'], 2)
        self.assertEqual(self.store.list_comics(search='456')['total'], 1)
        self.assertEqual(self.store.list_comics(sort_order='asc')['items'][0]['group_id'], '123')
        self.assertEqual([group['group_id'] for group in rows['groups']], ['123', '456'])
        for kwargs in [{'page': 0}, {'page_size': 13}, {'date_from': DAY, 'date_to': '2020-01-01'},
                       {'search': 'x' * 101}, {'sort_order': 'up'}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.list_comics(**kwargs)

    def test_corrupt_comic_meta_hidden_from_catalog(self):
        self.save_comic()
        path = self.store.root / '123' / DAY / 'comic.json'
        path.write_text('{invalid', encoding='utf-8')
        self.assertEqual(self.store.list_comics()['total'], 0)
        with self.assertRaises(ValueError):
            self.store.comic_detail('123', DAY)
        self.assertTrue(path.exists())

    def test_comic_without_report_and_missing_files(self):
        self.save_comic()
        self.assertEqual(self.store.list_comics()['total'], 1)
        (self.store.root / '123' / DAY / 'comic.png').unlink()
        self.assertEqual(self.store.list_comics()['total'], 0)


if __name__ == '__main__':
    unittest.main()
