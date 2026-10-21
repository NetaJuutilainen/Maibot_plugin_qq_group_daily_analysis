"""Run with python -m unittest discover -s tests; no bot/LLM/network startup."""
from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import unittest

spec = spec_from_file_location('daily_analysis', Path(__file__).resolve().parents[1] / 'plugin.py')
module = module_from_spec(spec)
spec.loader.exec_module(module)
NOW = datetime(2026, 10, 21, 21, 0).timestamp()
DAY = '2026-10-21'


def message(uid, text, nickname):
    return {'session_id': 'group-session', 'timestamp': NOW - 60,
            'message_info': {'user_info': {'user_id': uid, 'user_nickname': nickname},
                             'group_info': {'group_id': 'group-1', 'group_name': 'Test group'},
                             'additional_config': {'self_id': 'bot-1'}},
            'processed_plain_text': text, 'raw_message': [{'type': 'text', 'data': text}]}


class BotMessagesTest(unittest.IsolatedAsyncioTestCase):
    def plugin(self, enabled=False):
        p = module.GroupDailyAnalysisPlugin()
        p._plugin_config_instance = module.DailyAnalysisConfig()
        p._plugin_config_instance.analysis.include_bot_messages = enabled
        p._stats = {'chats': {}}
        p._dirty = False
        p._ctx = SimpleNamespace(logger=Mock(), call_capability=AsyncMock())
        return p

    async def test_disabled_by_default_and_receive_hook_respects_toggle(self):
        self.assertFalse(module.AnalysisConfig().include_bot_messages)
        p = self.plugin()
        own = message('bot-1', '机器人也说了一句足够长的话，用于日报采样。', 'Test bot')
        with patch.object(p, '_whitelist_ids', return_value=None):
            await p.collect(message=own)
            self.assertEqual(p._stats['chats'], {})
            p._cfg().analysis.include_bot_messages = True
            await p.collect(message=own)
        bucket = p._stats['chats']['group-session']['days'][DAY]
        self.assertEqual(bucket['users']['bot-1']['msg'], 1)
        self.assertEqual(bucket['sample'][0]['n'], 'Test bot')

    async def test_rebuild_toggle_covers_stats_samples_and_quotes_without_double_counting(self):
        bot = message('bot-1', '这是一段足够长的机器人回复，应该出现在日报的候选内容里。', 'Test bot')
        human = message('human-1', '这是一段来自群友的消息，也应该保留在日报分析中。', 'Test human')
        p = self.plugin()
        async def fetch(capability, **kwargs):
            self.assertEqual(capability, 'message.get_by_time_in_chat')
            self.assertTrue(kwargs['filter_command'])
            self.assertEqual(kwargs['end_time'] - kwargs['start_time'], 24 * 3600)
            rows = [human] if kwargs['filter_mai'] else [human, bot]
            return {'success': True, 'messages': rows}
        p.ctx.call_capability.side_effect = fetch
        with patch.object(module.time, 'time', return_value=NOW):
            await p._rebuild_day_from_db('group-session', DAY)
            data = p._stats['chats']['group-session']['days'][DAY]
            self.assertEqual(set(data['users']), {'human-1'})
            p._cfg().analysis.include_bot_messages = True
            for _ in range(2):
                await p._rebuild_day_from_db('group-session', DAY)
                data = p._stats['chats']['group-session']['days'][DAY]
                self.assertEqual(data['total'], 2)
                self.assertEqual(sum(data['hours']), 2)
                self.assertEqual(set(data['users']), {'human-1', 'bot-1'})
                self.assertEqual({s['u'] for s in data['sample']}, {'human-1', 'bot-1'})
                self.assertIn('Test bot', {q['u'] for q in data['quotes']})
            p._cfg().analysis.include_bot_messages = False
            await p._rebuild_day_from_db('group-session', DAY)
            self.assertNotIn('bot-1', p._stats['chats']['group-session']['days'][DAY]['users'])

    async def test_list_envelope_and_failed_fetch_preserve_data(self):
        p = self.plugin(True)
        p.ctx.call_capability.return_value = [message('bot-1', 'Bot reply.', 'Test bot')]
        await p._rebuild_day_from_db('group-session', DAY)
        before = p._stats['chats']['group-session']['days'][DAY]
        self.assertEqual(before['total'], 1)
        p.ctx.call_capability.side_effect = RuntimeError('test fetch failure')
        await p._rebuild_day_from_db('group-session', DAY)
        self.assertIs(before, p._stats['chats']['group-session']['days'][DAY])
        p.ctx.call_capability.side_effect = None
        p.ctx.call_capability.return_value = []
        await p._rebuild_day_from_db('group-session', DAY)
        self.assertIs(before, p._stats['chats']['group-session']['days'][DAY])


if __name__ == '__main__':
    unittest.main()
