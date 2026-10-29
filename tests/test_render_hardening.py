"""v1.3.0 渲染链加固的回归测试（issue #2 + #3）：外链摘除、字体注入、页面自静止、BlueArchive 本地化。

Run with python -m unittest discover -s tests; no bot/LLM/network startup.
成品 HTML 断言全部走真实 _render_daily + _inline_static_assets 管线（模板里是 Jinja 变量，
静态 grep 模板看不到 fonts.loli.net，必须渲染后验）。
"""
import asyncio
import re
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import unittest

spec = spec_from_file_location('daily_analysis', Path(__file__).resolve().parents[1] / 'plugin.py')
module = module_from_spec(spec)
spec.loader.exec_module(module)

FAKE_FONT_URI = 'data:font/woff2;base64,QUFBQQ=='
CONTEXT = {
    'day': '2026-10-30', 'chat_name': '测试群', 'message_count': 42,
    'participant_count': 2, 'emoji_count': 0, 'total_characters': 500,
    'most_active_period': '20点 ~ 22点',
    'chart_data': [{'hour': h, 'count': h % 5, 'percentage': min(100, (h % 5) * 20)} for h in range(24)],
    'sample': [{'u': '1', 'n': 'user1', 't': '这是一条足够长的测试消息内容，用于占位。'}],
    'avatars': {'1': 'https://q1.qlogo.cn/g?b=qq&nk=1&s=100'},
    'user_names': {'1': 'user1'},
    'user_stats': {},
}
ANALYSIS = {'topics': [], 'titles': [], 'quotes': [], 'quality': None}


class RenderHardeningTest(unittest.IsolatedAsyncioTestCase):
    def plugin(self):
        p = module.GroupDailyAnalysisPlugin()
        p._plugin_config_instance = module.DailyAnalysisConfig()
        p._ctx = SimpleNamespace(logger=Mock(), call_capability=Mock())
        return p

    def rendered(self, p, theme):
        """走真实渲染管线：_render_daily（含 emoji/中文字体/自静止注入）→ _inline_static_assets。"""
        raw = p._render_daily(dict(CONTEXT), dict(ANALYSIS), theme)
        return raw, p._inline_static_assets(raw)

    def test_loli_and_blueaka_links_removed_preconnect_kept(self):
        p = self.plugin()
        # 管线外直接喂一段带各种 link 的成品 HTML，验正则本身的行为边界
        html = (
            '<head>'
            '<link rel="preconnect" href="https://fonts.loli.net">'
            '<link rel="stylesheet" href="https://fonts.loli.net/css2?family=Noto+Sans+SC&display=swap">'
            '<link rel="stylesheet"\n  href="https://fonts.loli.net/css2?family=Noto+Serif+SC&display=swap">'
            '<link rel="stylesheet" href="//cdn.xcnahida.cn/fonts/bluearchive/Blueaka/Blueaka.css">'
            '</head>'
        )
        out = p._inline_static_assets(html)
        self.assertNotIn('css2', out)
        self.assertNotIn('xcnahida', out)
        self.assertIn('rel="preconnect"', out)

    def test_all_twelve_themes_pipeline_has_zero_blocking_links(self):
        p = self.plugin()
        p._font_sub_uri = FAKE_FONT_URI
        for theme in module.THEME_REGISTRY:
            raw, final = self.rendered(p, theme)
            blocking = re.findall(r'<link[^>]*fonts\.loli\.net/css2', final, flags=re.S | re.I)
            self.assertEqual(blocking, [], f'{theme}: 成品 HTML 仍含阻塞外链字体样式表')
            self.assertNotIn('xcnahida', final, f'{theme}: 成品 HTML 仍含 Blueaka 外链')
            # 自静止脚本必须注入
            self.assertIn('animation-play-state:paused', final, f'{theme}: 缺少页面自静止脚本')

    def test_preconnect_survives_pipeline(self):
        p = self.plugin()
        raw, final = self.rendered(p, 'simple')
        self.assertGreater(raw.count('rel="preconnect"'), 0, '渲染后应存在 preconnect（否则断言对象不存在）')
        self.assertEqual(raw.count('rel="preconnect"'), final.count('rel="preconnect"'))

    def test_bluearchive_remote_images_localized(self):
        p = self.plugin()
        raw, final = self.rendered(p, 'bluearchive')
        self.assertIn('img.dkdun.cn', raw, '渲染后 BlueArchive 应仍引用远程图（本地化发生在内联管线）')
        self.assertNotIn('img.dkdun.cn', final)
        self.assertIn('data:image/png;base64,', final)
        # 立绘文件真实打进包内且非空
        for rel in module.GroupDailyAnalysisPlugin._REMOTE_ASSET_FILES.values():
            path = module.GroupDailyAnalysisPlugin._THEME_ASSETS_ROOT / rel.removeprefix('assets/')
            self.assertTrue(path.is_file() and path.stat().st_size > 1024, f'{rel} 应为真实打包素材')

    def test_packaged_asset_missing_file_falls_back_to_blank_pixel(self):
        p = self.plugin()
        uri = p._packaged_asset_uri('assets/NoTheme/missing.png')
        self.assertEqual(uri, module.GroupDailyAnalysisPlugin._BLANK_PIXEL_URI)

    def test_lxgw_face_injection_toggle(self):
        # 直接验 _inject_emoji_font 输出：注入块带 FAKE_FONT_URI；atri 模板自带同名 face，
        # 不能用 "font-family: 'LXGW WenKai'" 字符串判存亡（模板里本来就有）
        for theme, expect_face in [('bluearchive', True), ('hatsunemiku', True), ('atri', False)]:
            p = self.plugin()
            p._font_sub_uri = FAKE_FONT_URI
            raw = p._render_daily(dict(CONTEXT), dict(ANALYSIS), theme)
            injected = p._inject_emoji_font(raw, theme)
            self.assertEqual(FAKE_FONT_URI in injected, expect_face, f'{theme}: LXGW @font-face 注入状态不符')
        # 注入块必须能穿过 _inline_static_assets 的 Medium/Mono 清理正则（D2 前提）
        p = self.plugin()
        p._font_sub_uri = FAKE_FONT_URI
        raw, final = self.rendered(p, 'bluearchive')
        self.assertRegex(final, r"@font-face \{ font-family: 'LXGW WenKai';[^}]*\}")
        # 关闭开关 → 非 atri 主题也不注入（但外链摘除仍生效，D2 约束 1）
        p = self.plugin()
        p._plugin_config_instance.analysis.inline_cjk_font = False
        p._font_sub_uri = FAKE_FONT_URI
        raw = p._render_daily(dict(CONTEXT), dict(ANALYSIS), 'bluearchive')
        injected = p._inject_emoji_font(raw, 'bluearchive')
        self.assertNotIn(FAKE_FONT_URI, injected)
        self.assertNotIn('fonts.loli.net/css2', p._inline_static_assets(injected))

    def _patched_send_path(self, p, html_text, html_lite):
        return [
            patch.object(p, '_daily_context', return_value=dict(CONTEXT)),
            patch.object(p, '_sample_messages_text', return_value=''),
            patch.object(p, '_render_daily', side_effect=[html_text, html_lite]),
            patch.object(p, '_inline_static_assets', side_effect=lambda h: h),
            patch.object(p, '_inline_avatars', side_effect=lambda h, c: h),
            patch.object(p, '_send_daily_html', side_effect=lambda *a, **k: False),
            patch.object(p, '_prefetch_profile_images', side_effect=lambda *_a: None),
            patch.object(p, '_upload_report_to_album', side_effect=lambda *_a, **_k: None),
        ]

    def test_render_tiers_use_settle_config_offline_and_lite_fallback(self):
        """发送路径契约：三档本地渲染都断网、都用 render_settle_ms、第三档是精简档 HTML。"""
        p = self.plugin()
        p._plugin_config_instance.analysis.render_settle_ms = 1234
        captured = []

        async def fake_capability(cap, **kwargs):
            captured.append(kwargs)
            return {'image_base64': ''}  # 全部返回空图 → 走完全部三档

        p.ctx.call_capability = fake_capability

        class _Send:
            async def text(self, *_a, **_k):
                pass

            async def image(self, *_a, **_k):
                pass

        p.ctx.send = _Send()

        async def run():
            patches = self._patched_send_path(p, 'FULL_HTML', 'LITE_HTML')
            for ctx in patches:
                ctx.start()
            try:
                with self.assertRaises(RuntimeError):  # 三档全空 → 最后 raise RuntimeError
                    await p._generate_daily_and_send('sid', '2026-10-30', 'atri', mark_sent=False)
            finally:
                for ctx in patches:
                    ctx.stop()

        asyncio.run(run())
        self.assertEqual(len(captured), 3, '应有三档本地渲染调用（云端默认关）')
        for kwargs in captured:
            self.assertFalse(kwargs['allow_network'], '本地渲染必须断网')
            self.assertEqual(kwargs['wait_for_timeout_ms'], 1234, '三档都应使用 render_settle_ms')
        self.assertEqual(captured[2]['html'], 'LITE_HTML', '第三档必须是精简档 HTML')

    def test_render_timeout_skips_middle_tier(self):
        """R1 超时 → 跳过稳定档直落精简档（issue #3 降级链止损）。"""
        p = self.plugin()
        captured = []

        async def fake_capability(cap, **kwargs):
            captured.append(kwargs['html'])
            if len(captured) == 1:
                raise RuntimeError('Page.set_content: Timeout 100000ms exceeded.')
            return {'image_base64': 'aGk='}

        p.ctx.call_capability = fake_capability

        class _Send:
            async def text(self, *_a, **_k):
                pass

            async def image(self, *_a, **_k):
                pass

        p.ctx.send = _Send()

        async def run():
            patches = self._patched_send_path(p, 'FULL_HTML', 'LITE_HTML')
            for ctx in patches:
                ctx.start()
            try:
                await p._generate_daily_and_send('sid', '2026-10-30', 'atri', mark_sent=False)
            finally:
                for ctx in patches:
                    ctx.stop()

        asyncio.run(run())
        self.assertEqual(captured, ['FULL_HTML', 'LITE_HTML'],
                         '全量档超时后应直落精简档，不再烧 1.0x 全量稳定档')


if __name__ == '__main__':
    unittest.main()
