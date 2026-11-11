import base64
import binascii
from datetime import date, datetime, timedelta
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
import uuid

from PIL import Image


class ReportStore:
    CHUNK_BYTES = 288 * 1024
    MAX_IMAGE_BYTES = 32 * 1024 * 1024
    MAX_HTML_BYTES = 32 * 1024 * 1024
    MAX_META_BYTES = 128 * 1024
    MAX_CONTEXT_BYTES = 4 * 1024 * 1024
    MAX_PIXELS = 40_000_000
    SUMMARY_FIELDS = ('group_id', 'group_name', 'day', 'theme', 'created_at', 'revision',
                      'has_image', 'message_count', 'participant_count', 'elapsed_seconds', 'version')
    COMIC_FILES = ('comic.png', 'comic_thumb.jpg', 'comic.json')
    COMIC_SUMMARY_FIELDS = ('group_id', 'group_name', 'day', 'style', 'created_at', 'revision',
                            'message_count', 'sent', 'request_id')
    MAX_COMIC_PROMPT = 8000

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).absolute() / 'reports'
        self._lock = threading.RLock()

    @staticmethod
    def _check_path(path):
        for item in (path, *path.parents):
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('报告路径不能是链接或重解析点')

    @staticmethod
    def _group(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
            raise ValueError('群标识无效')
        if re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', value, re.I):
            raise ValueError('群标识不能是系统设备名')
        return value

    @staticmethod
    def _day(value):
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('日期需为 YYYY-MM-DD')
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError('日期无效') from None
        return value

    def _directory(self, group_id, day):
        path = self.root / self._group(group_id) / self._day(day)
        self._check_path(path)
        return path

    def _file(self, directory, name):
        path = directory / name
        self._check_path(path)
        return path

    def _read_meta(self, directory):
        path = self._file(directory, 'meta.json')
        if path.stat().st_size > self.MAX_META_BYTES:
            raise ValueError('报告元数据过大')
        try:
            meta = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(meta, dict) or meta['group_id'] != directory.parent.name or meta['day'] != directory.name:
                raise ValueError('报告元数据不匹配')
            self._group(meta['group_id'])
            self._day(meta['day'])
            if not re.fullmatch(r'[0-9a-f]{32}', meta['revision']):
                raise ValueError('报告版本无效')
            datetime.fromisoformat(meta['created_at'])
            for key in ('group_name', 'theme', 'version'):
                if not isinstance(meta[key], str) or len(meta[key]) > 200:
                    raise ValueError('报告文本字段无效')
            for key in ('message_count', 'participant_count'):
                if type(meta[key]) is not int or not 0 <= meta[key] <= 10**12:
                    raise ValueError('报告计数字段无效')
            if type(meta['elapsed_seconds']) not in (int, float) or not 0 <= meta['elapsed_seconds'] <= 10**9:
                raise ValueError('报告耗时字段无效')
            if type(meta['has_image']) is not bool:
                raise ValueError('报告类型无效')
            required = ['report.html'] + (['report.png', 'thumb.jpg'] if meta['has_image'] else [])
            if any(not self._file(directory, name).is_file() for name in required):
                raise ValueError('报告文件不完整')
        except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
            raise ValueError('报告元数据无效') from None
        return meta

    @staticmethod
    def _revision(meta, revision):
        if revision and revision != meta['revision']:
            raise ValueError('报告已更新，请刷新后重试')

    def _image(self, image_b64):
        if not isinstance(image_b64, str) or len(image_b64) > (self.MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise ValueError('报告图片过大或编码无效')
        try:
            raw = base64.b64decode(image_b64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('报告图片编码无效') from None
        if not raw or len(raw) > self.MAX_IMAGE_BYTES:
            raise ValueError('报告图片大小无效')
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ('PNG', 'JPEG') or image.width * image.height > self.MAX_PIXELS:
                raise ValueError('仅支持四千万像素以内的 PNG/JPEG')
            image.load()
            if image.format != 'PNG':
                output = io.BytesIO()
                image.convert('RGB').save(output, format='PNG')
                raw = output.getvalue()
            if len(raw) > self.MAX_IMAGE_BYTES:
                raise ValueError('PNG 图片超过 32 MiB')
            thumb = image.convert('RGB')
            thumb.thumbnail((320, 320))
            while True:
                output = io.BytesIO()
                thumb.save(output, format='JPEG', quality=60)
                jpeg = output.getvalue()
                if len('data:image/jpeg;base64,') + (len(jpeg) + 2) // 3 * 4 <= 20 * 1024:
                    return raw, jpeg
                thumb.thumbnail((max(1, thumb.width * 3 // 4), max(1, thumb.height * 3 // 4)))

    def save(self, group_id, day, image_b64, html_text, metadata, context=None):
        directory = self._directory(group_id, day)
        if not isinstance(html_text, str) or not html_text:
            raise ValueError('报告 HTML 不能为空')
        html = html_text.encode('utf-8')
        if len(html) > self.MAX_HTML_BYTES or not isinstance(metadata, dict):
            raise ValueError('报告内容过大或元数据无效')
        image, thumb = self._image(image_b64) if image_b64 else (None, None)
        meta = {key: metadata[key] for key in ('stream_id', 'model_tasks', 'analysis') if key in metadata}
        meta.update(group_id=group_id, day=day, revision=uuid.uuid4().hex,
                    created_at=datetime.now().isoformat(timespec='microseconds'), has_image=image is not None,
                    group_name=str(metadata.get('group_name') or group_id)[:200],
                    theme=str(metadata.get('theme') or '')[:200], version=str(metadata.get('version') or '')[:200],
                    message_count=metadata.get('message_count', 0), participant_count=metadata.get('participant_count', 0),
                    elapsed_seconds=metadata.get('elapsed_seconds', 0))
        context_bytes = b''
        if context is not None:
            try:
                context_bytes = json.dumps(context, ensure_ascii=False, allow_nan=False).encode('utf-8')
            except (TypeError, ValueError):
                context_bytes = b''
            if len(context_bytes) > self.MAX_CONTEXT_BYTES:
                context_bytes = b''
        meta['has_context'] = bool(context_bytes)
        encoded = json.dumps(meta, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > self.MAX_META_BYTES:
            raise ValueError('报告元数据超过 128 KiB')
        with self._lock:
            self._check_path(directory)
            directory.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f'.{day}-', dir=directory.parent))
            backup = directory.parent / f'.{day}-old-{uuid.uuid4().hex}'
            moved = False
            try:
                (staging / 'report.html').write_bytes(html)
                if context_bytes:
                    (staging / 'context.json').write_bytes(context_bytes)
                if image is not None:
                    (staging / 'report.png').write_bytes(image)
                    (staging / 'thumb.jpg').write_bytes(thumb)
                (staging / 'meta.json').write_bytes(encoded)
                # 整目录替换前保留当日漫画存档（P3），否则重新生成日报会抹掉漫画
                if directory.exists():
                    for name in self.COMIC_FILES:
                        source = directory / name
                        if source.is_file():
                            shutil.copy2(source, staging / name)
                self._check_path(directory)
                if directory.exists():
                    os.replace(directory, backup)
                    moved = True
                try:
                    os.replace(staging, directory)
                except OSError:
                    if moved:
                        os.replace(backup, directory)
                        moved = False
                    raise
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            if moved:
                shutil.rmtree(backup)
        return meta

    def _catalog(self):
        self._check_path(self.root)
        if not self.root.exists():
            return []
        result = []
        for group in self.root.iterdir():
            try:
                self._group(group.name)
                self._check_path(group)
                if not group.is_dir():
                    continue
                for directory in group.iterdir():
                    try:
                        self._day(directory.name)
                        self._check_path(directory)
                        if directory.is_dir():
                            result.append(self._read_meta(directory))
                    except (OSError, ValueError):
                        continue
            except (OSError, ValueError):
                continue
        result.sort(key=lambda row: (row['created_at'], row['day'], row['group_id']), reverse=True)
        return result

    def read_context(self, group_id, day):
        with self._lock:
            directory = self._directory(group_id, day)
            meta = self._read_meta(directory)
            if not meta.get('has_context'):
                raise ValueError('该存档没有渲染上下文，无法免 Token 重绘')
            path = self._file(directory, 'context.json')
            if path.stat().st_size > self.MAX_CONTEXT_BYTES:
                raise ValueError('渲染上下文过大')
            context = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(context, dict):
                raise ValueError('渲染上下文无效')
            return context

    def list_reports(self, group_id='', date_from='', date_to='', page=1, page_size=12,
                     search='', report_type='', sort_order='desc'):
        if group_id:
            self._group(group_id)
        if date_from:
            self._day(date_from)
        if date_to:
            self._day(date_to)
        if date_from and date_to and date_from > date_to:
            raise ValueError('起始日期晚于结束日期')
        if type(page) is not int or not 1 <= page <= 1_000_000 or type(page_size) is not int or not 1 <= page_size <= 12:
            raise ValueError('分页参数无效')
        if not isinstance(search, str) or len(search) > 100:
            raise ValueError('搜索词无效')
        if report_type not in ('', 'image', 'html'):
            raise ValueError('报告类型无效')
        if sort_order not in ('desc', 'asc'):
            raise ValueError('排序方向无效')
        keyword = search.strip().casefold()
        with self._lock:
            all_rows = self._catalog()
            groups = {}
            for row in all_rows:
                groups.setdefault(row['group_id'], row['group_name'])
            rows = [row for row in all_rows if (not group_id or row['group_id'] == group_id)
                    and (not date_from or row['day'] >= date_from) and (not date_to or row['day'] <= date_to)
                    and (not report_type or row['has_image'] == (report_type == 'image'))
                    and (not keyword or any(keyword in str(row[key]).casefold()
                                            for key in ('group_name', 'group_id', 'day', 'theme')))]
            if sort_order == 'asc':
                rows.reverse()
            start = (page - 1) * page_size
            return {'items': [{key: row[key] for key in self.SUMMARY_FIELDS} for row in rows[start:start + page_size]],
                    'total': len(rows), 'page': page, 'pages': (len(rows) + page_size - 1) // page_size,
                    'groups': [{'group_id': gid, 'group_name': name} for gid, name in sorted(groups.items())[:100]]}

    def detail(self, group_id, day):
        with self._lock:
            return self._read_meta(self._directory(group_id, day))

    def thumbnail(self, group_id, day, revision=''):
        with self._lock:
            directory = self._directory(group_id, day)
            meta = self._read_meta(directory)
            self._revision(meta, revision)
            if not meta['has_image']:
                raise ValueError('该报告仅有 HTML')
            path = self._file(directory, 'thumb.jpg')
            if path.stat().st_size > 15 * 1024:
                raise ValueError('缩略图文件过大')
            return {'data_uri': 'data:image/jpeg;base64,' + base64.b64encode(path.read_bytes()).decode('ascii'),
                    'revision': meta['revision']}

    def _content(self, group_id, day, kind, revision):
        if kind not in ('image', 'html'):
            raise ValueError('不支持的报告文件类型')
        directory = self._directory(group_id, day)
        meta = self._read_meta(directory)
        self._revision(meta, revision)
        if kind == 'image' and not meta['has_image']:
            raise ValueError('该报告仅有 HTML')
        path = self._file(directory, 'report.png' if kind == 'image' else 'report.html')
        maximum = self.MAX_IMAGE_BYTES if kind == 'image' else self.MAX_HTML_BYTES
        size = path.stat().st_size
        if not 0 < size <= maximum:
            raise ValueError('报告文件大小无效')
        return path, meta, size

    def read_bytes(self, group_id, day, kind, revision=''):
        with self._lock:
            path, _, _ = self._content(group_id, day, kind, revision)
            return path.read_bytes()

    def read_chunk(self, group_id, day, kind='image', offset=0, revision=''):
        with self._lock:
            path, meta, size = self._content(group_id, day, kind, revision)
            if type(offset) is not int or not 0 <= offset <= size:
                raise ValueError('分块偏移无效')
            with path.open('rb') as stream:
                stream.seek(offset)
                block = stream.read(self.CHUNK_BYTES)
            next_offset = offset + len(block)
            return {'data': base64.b64encode(block).decode('ascii'), 'offset': offset,
                    'next_offset': next_offset, 'size': size, 'done': next_offset == size,
                    'mime': 'image/png' if kind == 'image' else 'text/html',
                    'filename': f'{group_id}-{day}.{"png" if kind == "image" else "html"}',
                    'revision': meta['revision']}

    def delete(self, group_id, day, revision=''):
        with self._lock:
            directory = self._directory(group_id, day)
            if not directory.exists():
                return {'deleted': False}
            meta = self._read_meta(directory)
            self._revision(meta, revision)
            for entry in directory.iterdir():
                self._check_path(entry)
            shutil.rmtree(directory)
            return {'deleted': True}

    def cleanup(self, retention_days=90):
        if type(retention_days) is not int or not 0 <= retention_days <= 3650:
            raise ValueError('保留天数无效')
        if retention_days == 0:
            return 0
        cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
        with self._lock:
            old = [row for row in self._catalog() if row['day'] < cutoff]
            return sum(self.delete(row['group_id'], row['day'], row['revision'])['deleted'] for row in old)

    # ------------------------------------------------------------------ 漫画存档（P3）
    # 漫画三件套 comic.png / comic_thumb.jpg / comic.json 与日报同住 reports/<群>/<日>/，
    # 日报 save() 整目录替换时会保留它们；delete() 删整日目录则一并删除。

    def _read_comic_meta(self, directory):
        path = self._file(directory, 'comic.json')
        if path.stat().st_size > self.MAX_META_BYTES:
            raise ValueError('漫画元数据过大')
        try:
            meta = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(meta, dict) or meta['group_id'] != directory.parent.name or meta['day'] != directory.name:
                raise ValueError('漫画元数据不匹配')
            self._group(meta['group_id'])
            self._day(meta['day'])
            if not re.fullmatch(r'[0-9a-f]{32}', meta['revision']):
                raise ValueError('漫画版本无效')
            if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', meta['request_id']):
                raise ValueError('漫画请求键无效')
            datetime.fromisoformat(meta['created_at'])
            for key in ('group_name', 'style', 'version'):
                if not isinstance(meta[key], str) or len(meta[key]) > 200:
                    raise ValueError('漫画文本字段无效')
            if type(meta['message_count']) is not int or not 0 <= meta['message_count'] <= 10**12:
                raise ValueError('漫画计数字段无效')
            if type(meta['sent']) is not bool:
                raise ValueError('漫画发送标记无效')
            if any(not self._file(directory, name).is_file() for name in ('comic.png', 'comic_thumb.jpg')):
                raise ValueError('漫画文件不完整')
        except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
            raise ValueError('漫画元数据无效') from None
        return meta

    def save_comic(self, group_id, day, image_b64, metadata):
        directory = self._directory(group_id, day)
        if not isinstance(metadata, dict):
            raise ValueError('漫画元数据无效')
        image, thumb = self._image(image_b64)
        storyboard = metadata.get('storyboard')
        if not isinstance(storyboard, dict):
            storyboard = {}
        meta = {
            'group_id': group_id, 'day': day, 'revision': uuid.uuid4().hex,
            'created_at': datetime.now().isoformat(timespec='microseconds'),
            'group_name': str(metadata.get('group_name') or group_id)[:200],
            'style': str(metadata.get('style') or '')[:200],
            'request_id': str(metadata.get('request_id') or ''),
            'task_id': str(metadata.get('task_id') or '')[:64],
            'prompt': str(metadata.get('prompt') or '')[:self.MAX_COMIC_PROMPT],
            'storyboard': storyboard,
            'message_count': metadata.get('message_count', 0),
            'sent': False, 'version': str(metadata.get('version') or '')[:200],
        }
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', meta['request_id']):
            raise ValueError('漫画请求键无效')
        encoded = json.dumps(meta, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > self.MAX_META_BYTES:
            raise ValueError('漫画元数据超过 128 KiB')
        with self._lock:
            self._check_path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            # 逐文件原子替换，comic.json 最后写（它是列表可见性的提交标记）
            for name, payload in (('comic.png', image), ('comic_thumb.jpg', thumb), ('comic.json', encoded)):
                fd, tmp = tempfile.mkstemp(prefix=f'.{name}-', dir=str(directory))
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(payload)
                    self._check_path(directory / name)
                    os.replace(tmp, directory / name)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
        return meta

    def _comic_catalog(self):
        self._check_path(self.root)
        if not self.root.exists():
            return []
        result = []
        for group in self.root.iterdir():
            try:
                self._group(group.name)
                self._check_path(group)
                if not group.is_dir():
                    continue
                for directory in group.iterdir():
                    try:
                        self._day(directory.name)
                        self._check_path(directory)
                        if directory.is_dir() and (directory / 'comic.json').is_file():
                            result.append(self._read_comic_meta(directory))
                    except (OSError, ValueError):
                        continue
            except (OSError, ValueError):
                continue
        result.sort(key=lambda row: (row['created_at'], row['day'], row['group_id']), reverse=True)
        return result

    def list_comics(self, group_id='', date_from='', date_to='', page=1, page_size=12,
                    search='', sort_order='desc'):
        if group_id:
            self._group(group_id)
        if date_from:
            self._day(date_from)
        if date_to:
            self._day(date_to)
        if date_from and date_to and date_from > date_to:
            raise ValueError('起始日期晚于结束日期')
        if type(page) is not int or not 1 <= page <= 1_000_000 or type(page_size) is not int or not 1 <= page_size <= 12:
            raise ValueError('分页参数无效')
        if not isinstance(search, str) or len(search) > 100:
            raise ValueError('搜索词无效')
        if sort_order not in ('desc', 'asc'):
            raise ValueError('排序方向无效')
        keyword = search.strip().casefold()
        with self._lock:
            all_rows = self._comic_catalog()
            groups = {}
            for row in all_rows:
                groups.setdefault(row['group_id'], row['group_name'])
            rows = [row for row in all_rows if (not group_id or row['group_id'] == group_id)
                    and (not date_from or row['day'] >= date_from) and (not date_to or row['day'] <= date_to)
                    and (not keyword or any(keyword in str(row[key]).casefold()
                                            for key in ('group_name', 'group_id', 'day', 'style')))]
            if sort_order == 'asc':
                rows.reverse()
            start = (page - 1) * page_size
            return {'items': [{key: row[key] for key in self.COMIC_SUMMARY_FIELDS} for row in rows[start:start + page_size]],
                    'total': len(rows), 'page': page, 'pages': (len(rows) + page_size - 1) // page_size,
                    'groups': [{'group_id': gid, 'group_name': name} for gid, name in sorted(groups.items())[:100]]}

    def comic_detail(self, group_id, day):
        with self._lock:
            return self._read_comic_meta(self._directory(group_id, day))

    def comic_thumbnail(self, group_id, day, revision=''):
        with self._lock:
            directory = self._directory(group_id, day)
            meta = self._read_comic_meta(directory)
            self._revision(meta, revision)
            path = self._file(directory, 'comic_thumb.jpg')
            if path.stat().st_size > 15 * 1024:
                raise ValueError('缩略图文件过大')
            return {'data_uri': 'data:image/jpeg;base64,' + base64.b64encode(path.read_bytes()).decode('ascii'),
                    'revision': meta['revision']}

    def read_comic_bytes(self, group_id, day, revision=''):
        with self._lock:
            directory = self._directory(group_id, day)
            meta = self._read_comic_meta(directory)
            self._revision(meta, revision)
            path = self._file(directory, 'comic.png')
            size = path.stat().st_size
            if not 0 < size <= self.MAX_IMAGE_BYTES:
                raise ValueError('漫画文件大小无效')
            return path.read_bytes()

    def comic_chunk(self, group_id, day, offset=0, revision=''):
        with self._lock:
            directory = self._directory(group_id, day)
            meta = self._read_comic_meta(directory)
            self._revision(meta, revision)
            path = self._file(directory, 'comic.png')
            size = path.stat().st_size
            if not 0 < size <= self.MAX_IMAGE_BYTES:
                raise ValueError('漫画文件大小无效')
            if type(offset) is not int or not 0 <= offset <= size:
                raise ValueError('分块偏移无效')
            with path.open('rb') as stream:
                stream.seek(offset)
                block = stream.read(self.CHUNK_BYTES)
            next_offset = offset + len(block)
            return {'data': base64.b64encode(block).decode('ascii'), 'offset': offset,
                    'next_offset': next_offset, 'size': size, 'done': next_offset == size,
                    'mime': 'image/png', 'filename': f'{group_id}-{day}-comic.png',
                    'revision': meta['revision']}

    def mark_comic_sent(self, group_id, day, revision=''):
        with self._lock:
            directory = self._directory(group_id, day)
            meta = self._read_comic_meta(directory)
            self._revision(meta, revision)
            meta['sent'] = True
            encoded = json.dumps(meta, ensure_ascii=False, allow_nan=False).encode('utf-8')
            path = self._file(directory, 'comic.json')
            fd, tmp = tempfile.mkstemp(prefix='.comic.json-', dir=str(directory))
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(encoded)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return meta

    def delete_comic(self, group_id, day, revision=''):
        with self._lock:
            directory = self._directory(group_id, day)
            if not (directory / 'comic.json').is_file():
                return {'deleted': False}
            meta = self._read_comic_meta(directory)
            self._revision(meta, revision)
            for name in self.COMIC_FILES:
                path = self._file(directory, name)
                if path.is_file():
                    path.unlink()
            return {'deleted': True}
