"""v1.4.0 官 Q（QQ 开放平台）身份适配回归测试：openid 昵称兜底、头像走宿主能力、引用正则放宽。

Run with python -m unittest discover -s tests; no bot/LLM/network startup.
这里的所有断言都只跑纯函数与不经网络的分支：chat.get_avatar 用 AsyncMock 假造。
"""
import asyncio
import re
from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import unittest

spec = spec_from_file_location('daily_analysis', Path(__file__).resolve().parents[1] / 'plugin.py')
module = module_from_spec(spec)
spec.loader.exec_module(module)

NUMERIC_UID = '123456789'
OPENID_UID = '2C9FD5BC5264B1C0999D8F47C5E809F8'
NOW = 1760000000.0
# 插件按本地时区把时间戳切成日期桶，测试跟着本地时区走
DAY = datetime.fromtimestamp(NOW - 60).strftime('%Y-%m-%d')


def message(uid, text, nickname='', platform='qq', account_id='acc-1'):
    """构造一条群消息：官 Q 的 user_id 是 openid，且通常不带 user_cardname。"""
    return {
        'session_id': 'group-session',
        'timestamp': NOW - 60,
        'platform': platform,
        'account_id': account_id,
        'message_info': {
            'user_info': {'user_id': uid, 'user_nickname': nickname},
            'group_info': {'group_id': 'group-1', 'group_name': '测试群'},
            'additional_config': {'self_id': 'bot-1'},
        },
        'processed_plain_text': text,
        'raw_message': [{'type': 'text', 'data': text}],
    }


class OpenIdHelpersTest(unittest.TestCase):
    def test_is_openid_distinguishes_numeric_and_openid(self):
        self.assertFalse(module._is_openid(NUMERIC_UID))
        self.assertTrue(module._is_openid(OPENID_UID))
        self.assertFalse(module._is_openid(''))

    def test_display_name_keeps_real_nickname(self):
        self.assertEqual(module._display_name(OPENID_UID, '小明'), '小明')
        self.assertEqual(module._display_name(NUMERIC_UID, '小明'), '小明')

    def test_display_name_falls_back_for_openid(self):
        # 昵称缺失、或昵称就是 openid 本身，都退化成「群友 + 后 6 位」
        self.assertEqual(module._display_name(OPENID_UID, ''), '群友E809F8')
        self.assertEqual(module._display_name(OPENID_UID, OPENID_UID), '群友E809F8')

    def test_display_name_keeps_numeric_behaviour(self):
        self.assertEqual(module._display_name(NUMERIC_UID, ''), NUMERIC_UID)

    def test_avatar_cache_key_is_filename_safe_and_unique(self):
        key = module._avatar_cache_key(OPENID_UID)
        self.assertRegex(key, r'^[0-9A-Za-z_-]+$')
        self.assertLessEqual(len(key), 60)
        # 清洗后同形的两个 ID 不能撞到同一个缓存文件
        self.assertNotEqual(module._avatar_cache_key('a/b*'), module._avatar_cache_key('a:b*'))

    def test_extract_avatar_url_accepts_envelope_shapes(self):
        url = 'https://q.qlogo.cn/qqapp/123/%s/100' % OPENID_UID
        self.assertEqual(module._extract_avatar_url({'success': True, 'avatar': {'status': 'available', 'url': url}}), url)
        self.assertEqual(module._extract_avatar_url({'avatar': {'status': 'available', 'url': url}}), url)
        self.assertEqual(module._extract_avatar_url({'status': 'available', 'url': url}), url)

    def test_extract_avatar_url_rejects_unsupported_and_empty(self):
        self.assertEqual(module._extract_avatar_url({'avatar': {'status': 'unsupported'}}), '')
        self.assertEqual(module._extract_avatar_url({'avatar': {'status': 'available', 'url': ''}}), '')
        self.assertEqual(module._extract_avatar_url({'avatar': {'status': 'available', 'url': 'ftp://x/y'}}), '')
        self.assertEqual(module._extract_avatar_url(None), '')
        self.assertEqual(module._extract_avatar_url('boom'), '')


class UidRefRegexTest(unittest.TestCase):
    def test_matches_openid_and_numeric(self):
        found = [m.group(1) for m in module.UID_REF_RE.finditer('[%s] 和 [%s]' % (OPENID_UID, NUMERIC_UID))]
        self.assertEqual(found, [OPENID_UID, NUMERIC_UID])

    def test_ignores_placeholders_and_cq_codes(self):
        text = '[图片] [CQ:at,qq=123456] [更早的消息已截断，以下为最近的消息] [abc]'
        self.assertEqual(module.UID_REF_RE.findall(text), [])


class OpenIdIdentityTest(unittest.IsolatedAsyncioTestCase):
    def plugin(self):
        p = module.GroupDailyAnalysisPlugin()
        p._plugin_config_instance = module.DailyAnalysisConfig()
        p._stats = {'chats': {}}
        p._dirty = False
        p._ctx = SimpleNamespace(logger=Mock(), call_capability=AsyncMock())
        return p

    async def test_collect_records_platform_context_and_openid_nickname(self):
        p = self.plugin()
        with unittest.mock.patch.object(p, '_whitelist_ids', return_value=None):
            await p.collect(message=message(OPENID_UID, '官方机器人接入后这条消息要能正常统计。'))
        chat = p._stats['chats']['group-session']
        self.assertEqual(chat['platform'], 'qq')
        self.assertEqual(chat['account_id'], 'acc-1')
        self.assertEqual(chat['name'], '测试群')
        user = chat['days'][DAY]['users'][OPENID_UID]
        self.assertEqual(user['n'], '群友E809F8')
        self.assertEqual(user['msg'], 1)

    async def test_collect_keeps_numeric_uid_untouched(self):
        p = self.plugin()
        with unittest.mock.patch.object(p, '_whitelist_ids', return_value=None):
            await p.collect(message=message(NUMERIC_UID, 'NapCat 侧的消息行为必须保持不变。', nickname=NUMERIC_UID))
        chat = p._stats['chats']['group-session']
        self.assertEqual(chat['days'][DAY]['users'][NUMERIC_UID]['n'], NUMERIC_UID)

    def test_daily_context_splits_avatar_urls_by_id_shape(self):
        p = self.plugin()
        p._stats = {'chats': {'group-session': {
            'name': '测试群', 'platform': 'qq', 'account_id': 'acc-1',
            'days': {DAY: {
                'total': 20,
                'hours': [0] * 24,
                'users': {
                    NUMERIC_UID: {'n': NUMERIC_UID, 'msg': 10},
                    OPENID_UID: {'n': '群友E809F8', 'msg': 10},
                },
            }},
        }}}
        context = p._daily_context('group-session', DAY)
        self.assertEqual(context['avatars'][NUMERIC_UID],
                         'https://q1.qlogo.cn/g?b=qq&nk=%s&s=100' % NUMERIC_UID)
        self.assertEqual(context['avatars'][OPENID_UID], '')
        self.assertEqual(context['user_names'][OPENID_UID], '群友E809F8')

    async def test_attach_avatars_fills_openid_urls_via_capability(self):
        p = self.plugin()
        p._stats = {'chats': {'group-session': {
            'name': '测试群', 'platform': 'qq', 'account_id': 'acc-1', 'days': {},
        }}}
        url = 'https://q.qlogo.cn/qqapp/123/%s/100' % OPENID_UID
        p._ctx.call_capability = AsyncMock(return_value={'success': True, 'avatar': {'status': 'available', 'url': url}})
        context = {
            'avatars': {NUMERIC_UID: 'https://q1.qlogo.cn/g?b=qq&nk=%s&s=100' % NUMERIC_UID, OPENID_UID: ''},
            'user_stats': {NUMERIC_UID: {'msg': 3}, OPENID_UID: {'msg': 9}},
        }
        await p._attach_avatars('group-session', context)
        self.assertEqual(context['avatars'][OPENID_UID], url)
        # 纯数字 ID 不浪费能力调用
        p._ctx.call_capability.assert_awaited_once()
        kwargs = p._ctx.call_capability.await_args.kwargs
        self.assertEqual(kwargs['platform'], 'qq')
        self.assertEqual(kwargs['target_id'], OPENID_UID)
        self.assertEqual(kwargs['target_type'], 'user')
        self.assertEqual(kwargs['account_id'], 'acc-1')

    async def test_attach_avatars_swallows_failures(self):
        p = self.plugin()
        p._stats = {'chats': {'group-session': {'name': '测试群', 'platform': 'qq', 'days': {}}}}
        p._ctx.call_capability = AsyncMock(side_effect=RuntimeError('适配器头像查询失败'))
        context = {'avatars': {OPENID_UID: ''}, 'user_stats': {OPENID_UID: {'msg': 5}}}
        await p._attach_avatars('group-session', context)
        self.assertEqual(context['avatars'][OPENID_UID], '')

    async def test_attach_avatars_skips_when_platform_unknown(self):
        p = self.plugin()
        p._stats = {'chats': {'group-session': {'name': '测试群', 'days': {}}}}
        context = {'avatars': {OPENID_UID: ''}, 'user_stats': {OPENID_UID: {'msg': 5}}}
        await p._attach_avatars('group-session', context)
        p._ctx.call_capability.assert_not_awaited()

    async def test_attach_avatars_limits_lookup_to_top_talkers(self):
        p = self.plugin()
        p._stats = {'chats': {'group-session': {'name': '测试群', 'platform': 'qq', 'days': {}}}}
        p._ctx.call_capability = AsyncMock(return_value={'avatar': {'status': 'unsupported'}})
        avatars = {('%032d' % i): '' for i in range(module.AVATAR_LOOKUP_LIMIT + 5)}
        stats = {uid: {'msg': i} for i, uid in enumerate(avatars)}
        await p._attach_avatars('group-session', {'avatars': avatars, 'user_stats': stats})
        self.assertEqual(p._ctx.call_capability.await_count, module.AVATAR_LOOKUP_LIMIT)

    def test_render_mentions_renders_openid_capsule(self):
        p = self.plugin()
        context = {
            'avatars': {OPENID_UID: ''},
            'user_names': {OPENID_UID: '群友E809F8'},
        }
        out = p._render_mentions('由 [%s] 提出的观点' % OPENID_UID, context)
        self.assertIn('user-capsule', out)
        self.assertIn('群友E809F8', out)
        self.assertNotIn(OPENID_UID, out)

    def test_replace_uid_refs_ignores_unknown_ids(self):
        p = self.plugin()
        context = {'user_names': {OPENID_UID: '群友E809F8'}}
        self.assertEqual(p._replace_uid_refs('[%s] 很活跃' % OPENID_UID, context), '群友E809F8 很活跃')
        # 不在统计里的 ID 原样保留，不擦成空白
        self.assertEqual(p._replace_uid_refs('[999999] 不存在', context), '[999999] 不存在')

    def test_render_daily_quote_sender_resolves_openid(self):
        p = self.plugin()
        context = {
            'day': DAY, 'chat_name': '测试群', 'message_count': 20, 'participant_count': 1,
            'emoji_count': 0, 'total_characters': 100, 'most_active_period': '20点 ~ 22点',
            'chart_data': [{'hour': h, 'count': 0, 'percentage': 0} for h in range(24)],
            'sample': [], 'avatars': {OPENID_UID: ''}, 'user_names': {OPENID_UID: '群友E809F8'},
            'user_stats': {},
        }
        analysis = {
            'topics': [], 'titles': [], 'quality': None,
            'quotes': [{'sender': '[%s]' % OPENID_UID, 'content': '这句真的很精辟，值得留下来。', 'reason': ''}],
        }
        html = p._render_daily(context, analysis, 'simple')
        self.assertIn('群友E809F8', html)
        self.assertNotIn(OPENID_UID, html)


if __name__ == '__main__':
    unittest.main()
