"""多天分析 / 定时与手动周报 / 多天漫画 / LLM 工具总开关验收测试（全离线）。"""

import asyncio
import base64
from datetime import date, datetime, timedelta
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SPEC = spec = importlib.util.spec_from_file_location(
    "daily_analysis_multiday", ROOT / "plugin.py", submodule_search_locations=[str(ROOT)]
)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)

multiday_mod = importlib.import_module(f"{SPEC.name.rsplit('.', 1)[0]}.multiday") if "." in SPEC.name else None
if multiday_mod is None:
    mspec = importlib.util.spec_from_file_location("multiday_direct", ROOT / "multiday.py")
    multiday_mod = importlib.util.module_from_spec(mspec)
    mspec.loader.exec_module(multiday_mod)

TODAY = date.today().isoformat()


def png_b64() -> str:
    out = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode("ascii")


def make_msg(uid: str, nick: str, text: str, ts: float, *, notify: bool = False, forward: bool = False) -> dict:
    raw = [{"type": "forward", "data": {}}] if forward else [{"type": "text", "data": text}]
    msg = {
        "session_id": "stream-1",
        "timestamp": ts,
        "platform": "qq",
        "account_id": "bot-1",
        "scope": "group",
        "is_notify": notify,
        "message_info": {
            "user_info": {"user_id": uid, "user_nickname": nick},
            "group_info": {"group_id": "123", "group_name": "测试大群"},
            "additional_config": {"self_id": "bot-1"},
        },
        "processed_plain_text": "【合并转发消息】" if forward else text,
        "raw_message": raw,
    }
    return msg


class FakeDrawpicApi:
    def __init__(self):
        self.calls = []
        self.blob = base64.b64decode(png_b64())

    async def get(self, name, *, version=""):
        if name in ("maimai-drawpic-plugin.drawpic_submit", "maimai-drawpic-plugin.drawpic_request") and version == "":
            return {"api": name}
        return None

    async def call(self, name, *, version="", **kwargs):
        self.calls.append((name, kwargs))
        op = name.split("drawpic_")[-1]
        if op == "submit":
            return {"accepted": True, "task_id": "t-1", "request_id": kwargs["request_id"]}
        if op == "task":
            return {"task_id": "t-1", "status": "completed", "error": None}
        if op == "image":
            return {
                "data": base64.b64encode(self.blob).decode("ascii"),
                "offset": 0,
                "next_offset": len(self.blob),
                "size": len(self.blob),
                "done": True,
                "mime": "image/png",
            }
        return {}


class MultiDayWeeklyAcceptanceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        self.p = module.GroupDailyAnalysisPlugin()
        self.p._plugin_config_instance = module.DailyAnalysisConfig()
        self.p._config_toml_path = str(self.data_dir / "config.toml")
        self.p._config_save_lock = asyncio.Lock()
        self.p._schedule_wakeup = asyncio.Event()
        self.p._report_store = module.ReportStore(self.data_dir)
        self.p._comic_book = module.ComicRequestBook(self.data_dir / "comic_requests.json")
        self.p._jobs = module.JobManager(self.p._spawn, Mock())
        self.p._tasks = []
        self.p._drawpic_probe = None
        self.p._live_persona_cache = None
        self.p._live_persona_ts = 0.0
        self.p._stats_loaded = True
        self.p._stats = {
            "chats": {
                "stream-1": {
                    "group_id": "123",
                    "name": "测试大群",
                    "platform": "qq",
                    "account_id": "bot-1",
                    "scope": "group",
                    "last_daily": "",
                    "days": {
                        TODAY: {
                            "total": 20,
                            "users": {"10001": {"n": "甲", "msg": 12, "char": 200, "sticker": 0, "night": 0, "reply": 2},
                                      "10002": {"n": "乙", "msg": 8, "char": 150, "sticker": 0, "night": 0, "reply": 1}},
                            "hours": [1 if h == 20 else 0 for h in range(24)],
                            "sample": [{"u": "10001", "n": "甲", "t": "今天测试消息一条用于凑够最小样本", "tm": "20:00"}],
                            "quotes": [],
                        }
                    },
                }
            }
        }
        self.draw_api = FakeDrawpicApi()
        self.cap_mock = AsyncMock(side_effect=self._default_capability)
        self.p._ctx = SimpleNamespace(
            logger=Mock(),
            paths=SimpleNamespace(data_dir=self.data_dir),
            send=SimpleNamespace(text=AsyncMock(), image=AsyncMock(return_value=True)),
            api=self.draw_api,
            call_capability=self.cap_mock,
        )
        self.p._render_report_image = AsyncMock(side_effect=lambda html_text, html_lite, acfg: (png_b64(), html_text))
        self.p._attach_avatars = AsyncMock()
        self.p._prefetch_profile_images = AsyncMock()

    async def asyncTearDown(self):
        for t in list(self.p._tasks):
            t.cancel()
        await asyncio.gather(*self.p._tasks, return_exceptions=True)

    async def _default_capability(self, capability, **kwargs):
        if capability == "message.get_by_time_in_chat":
            return []
        if capability == "llm.generate":
            return {"success": True, "response": "{}", "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        return {"success": True}

    async def _wait_jobs(self):
        for _ in range(80):
            if self.p._jobs.active_count() == 0:
                return
            await asyncio.sleep(0.02)

    def _seed_daily_archive(self, day_str: str, topic_name: str = "日常热点") -> dict:
        ctx = {
            "day": day_str, "chat_name": "测试大群", "message_count": 30, "participant_count": 3,
            "emoji_count": 2, "total_characters": 400, "most_active_period": "20点 ~ 22点",
            "chart_data": [{"hour": h, "count": 3 if h == 20 else 0, "percentage": 100 if h == 20 else 0} for h in range(24)],
            "sample": [{"u": "10001", "n": "甲", "t": f"{day_str} 的核心消息", "tm": "20:00"}],
            "avatars": {"10001": ""}, "user_names": {"10001": "甲", "10002": "乙"},
            "user_stats": {"10001": {"n": "甲", "msg": 18, "char": 260, "sticker": 1, "night": 0, "reply": 2},
                           "10002": {"n": "乙", "msg": 12, "char": 140, "sticker": 1, "night": 0, "reply": 1}},
        }
        analysis = {
            "topics": [{"topic": topic_name, "contributors": ["10001", "10002"], "detail": f"{day_str} 大家聊了 {topic_name}"}],
            "titles": [{"name": "甲", "user_id": "10001", "title": "群聊先锋", "mbti": "INTJ", "reason": "发言极其积极，带动了全群讨论氛围和日常交流进度。"}],
            "quotes": [{"content": f"{day_str} 金句在此", "sender": "[10001]", "reason": "很有意思"}],
            "quality": {"title": "观察", "subtitle": "活跃", "summary": "挺热闹", "dimensions": [{"name": "氛围", "percentage": 80, "comment": "不错"}]},
        }
        return self.p._report_store.save("123", day_str, png_b64(), "<html>daily</html>", {
            "group_name": "测试大群", "theme": "atri", "message_count": 30, "participant_count": 3,
            "analysis": analysis,
        }, ctx)

    async def test_1_multiday_7d_slices_per_day_and_preserves_daily_archive(self):
        daily_meta = self._seed_daily_archive(TODAY, "今日1天话题")
        slices = multiday_mod.build_rolling_slices(time.time(), 7)
        db_calls = []
        llm_calls = []

        async def cap(name, **kwargs):
            if name == "message.get_by_time_in_chat":
                db_calls.append((kwargs["start_time"], kwargs["end_time"]))
                idx = len(db_calls)
                mid = (kwargs["start_time"] + kwargs["end_time"]) / 2
                return [
                    make_msg("10001", "甲", f"第{idx}天讨论话题 Alpha 很深入", mid),
                    make_msg("10002", "乙", f"第{idx}天讨论话题 Beta 也很棒", mid + 60),
                ]
            if name == "llm.generate":
                llm_calls.append(kwargs["prompt"])
                return {
                    "success": True,
                    "prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300,
                    "response": json.dumps({
                        "topics": [{"topic": "七天热点", "contributors": ["10001", "10002"], "detail": "七天讨论非常热烈"}],
                        "titles": [{"name": "甲", "user_id": "10001", "title": "周度龙王", "mbti": "ENTP", "reason": "七天连续在线活跃发言输出观点，带动群友讨论热情。"}],
                        "quotes": [{"content": "第7天讨论话题 Alpha 很深入", "sender": "[10001]", "reason": "代表性发言"}],
                        "quality": {"title": "七日观察", "subtitle": "活跃", "summary": "热闹", "dimensions": [{"name": "活跃度", "percentage": 85, "comment": "好"}]},
                    }, ensure_ascii=False),
                }
            return {"success": True}

        self.cap_mock.side_effect = cap
        self.p._cfg().multi_day.multi_day_strategy = "分层抽样"
        self.p._cfg().analysis.min_messages_threshold = 5

        handled, reason, _ = await self.p.cmd_daily_report(
            stream_id="stream-1", user_id="10001",
            message={"message_info": {"group_info": {"group_id": "123"}}},
            matched_groups={"args": "7"},
        )
        self.assertTrue(handled)
        self.assertEqual(reason, "多天报告生成中")
        await self._wait_jobs()

        # 按天独立查库 7 次，不能一次性拉 7 天
        self.assertEqual(len(db_calls), 7)
        for s, e in db_calls:
            self.assertAlmostEqual(e - s, 86400.0, delta=2.0)
        self.assertEqual(len(llm_calls), 1)
        # 独立槽位保存，不覆盖 1 天日报
        self.assertEqual(self.p._report_store.detail("123", TODAY)["revision"], daily_meta["revision"])
        slot_day = f"{TODAY}~7d"
        md_meta = self.p._report_store.detail("123", slot_day)
        self.assertEqual(md_meta["report_kind"], "multiday")
        self.assertEqual(md_meta["window_days"], 7)
        self.assertEqual(len(md_meta["covered_days"]), 7)
        html_bytes = self.p._report_store.read_bytes("123", slot_day, "html").decode("utf-8")
        self.assertIn(f"覆盖 {slices[0]['day'][5:]} ~ {slices[-1]['day'][5:]}", html_bytes)
        self.assertIn("分层抽样", html_bytes)

    async def test_2_missing_days_and_slice_failure_still_succeed_and_note_missing(self):
        slices = multiday_mod.build_rolling_slices(time.time(), 5)
        call_idx = 0

        async def cap(name, **kwargs):
            nonlocal call_idx
            if name == "message.get_by_time_in_chat":
                call_idx += 1
                if call_idx == 2:
                    return []  # 缺天：0 消息
                if call_idx == 4:
                    raise RuntimeError("DB timeout")  # 缺天：单日查询异常
                mid = (kwargs["start_time"] + kwargs["end_time"]) / 2
                return [
                    make_msg("10001", "甲", f"有效发言内容 {call_idx} 号，讨论技术方案", mid),
                    make_msg("10002", "乙", f"补充观点内容 {call_idx} 号，一起研究问题", mid + 30),
                ]
            if name == "llm.generate":
                return {
                    "success": True,
                    "response": json.dumps({
                        "topics": [{"topic": "缺天测试", "contributors": ["10001"], "detail": "正常出报告"}],
                        "titles": [], "quotes": [], "quality": None,
                    }, ensure_ascii=False),
                }
            return {"success": True}

        self.cap_mock.side_effect = cap
        self.p._cfg().multi_day.multi_day_strategy = "分层抽样"
        self.p._cfg().analysis.min_messages_threshold = 4

        job = {"stage": ""}
        res = await self.p._generate_multiday_and_send(job, "stream-1", "123", 5, "atri")
        self.assertEqual(res, "ok")
        meta = self.p._report_store.detail("123", f"{TODAY}~5d")
        miss_expected = [slices[1]["day"], slices[3]["day"]]
        self.assertEqual(meta["missing_days"], miss_expected)
        self.assertIn(f"缺 {miss_expected[0][5:]}、{miss_expected[1][5:]}", meta["coverage_note"])

    async def test_3_strategy_call_counts_and_hard_degradation_chain(self):
        slices = multiday_mod.build_rolling_slices(time.time(), 7)
        # 3a: C 有完整 7 天存档 -> 0 次查库 + 1 次 LLM 调用
        for i, sl in enumerate(slices):
            self._seed_daily_archive(sl["day"], f"话题{i}")
        db_count = 0
        llm_count = 0

        async def cap_c_full(name, **kwargs):
            nonlocal db_count, llm_count
            if name == "message.get_by_time_in_chat":
                db_count += 1
                return []
            if name == "llm.generate":
                llm_count += 1
                return {"success": True, "response": json.dumps({"topics": [{"topic": "汇总话题", "contributors": ["10001"], "detail": "合并"}], "titles": [], "quotes": [], "quality": None}, ensure_ascii=False)}
            return {"success": True}

        self.cap_mock.side_effect = cap_c_full
        res = await self.p._execute_multiday_analysis({"stage": ""}, "stream-1", "123", 7, strategy_raw="存档优先", end_ts=slices[-1]["end_ts"])
        self.assertEqual(res["status"], "ok")
        self.assertEqual(db_count, 0)
        self.assertEqual(llm_count, 1)
        self.assertEqual(res["context"]["strategy_used"], "archive_first")

        # 3b: A 7 天有消息 -> N+1 = 8 次 LLM 调用
        llm_count = 0

        async def cap_a(name, **kwargs):
            nonlocal llm_count
            if name == "message.get_by_time_in_chat":
                mid = (kwargs["start_time"] + kwargs["end_time"]) / 2
                return [make_msg("10001", "甲", f"消息 {mid:.0f} 讨论具体细节问题", mid)]
            if name == "llm.generate":
                llm_count += 1
                if llm_count <= 7:
                    return {"success": True, "response": json.dumps({"topics": [{"topic": f"日话题{llm_count}", "contributors": ["10001"], "detail": "日详情"}], "quotes": [], "summary": "日总结"}, ensure_ascii=False)}
                return {"success": True, "response": json.dumps({"topics": [{"topic": "总话题", "contributors": ["10001"], "detail": "汇总"}], "titles": [], "quotes": [], "quality": None}, ensure_ascii=False)}
            return {"success": True}

        self.cap_mock.side_effect = cap_a
        self.p._cfg().analysis.min_messages_threshold = 5
        res_a = await self.p._execute_multiday_analysis({"stage": ""}, "stream-1", "123", 7, strategy_raw="按天提炼")
        self.assertEqual(res_a["status"], "ok")
        self.assertEqual(llm_count, 8)
        self.assertEqual(res_a["context"]["strategy_used"], "per_day_summary")

        # 3c: 硬降级链 C(无存档) -> A(单日全失败) -> B(成功)
        with tempfile.TemporaryDirectory() as empty_dir:
            self.p._report_store = module.ReportStore(Path(empty_dir))
            a_failed_calls = 0
            db_queries = 0

            async def cap_c_to_a_to_b(name, **kwargs):
                nonlocal a_failed_calls, db_queries
                if name == "message.get_by_time_in_chat":
                    db_queries += 1
                    mid = (kwargs["start_time"] + kwargs["end_time"]) / 2
                    return [make_msg("10001", "甲", f"消息 {db_queries} 研究如何升级架构", mid)]
                if name == "llm.generate":
                    # A 阶段前 3 天（这里用 3 天测更快）全返回非法 JSON
                    if "轻量提炼" in kwargs["prompt"]:
                        a_failed_calls += 1
                        return {"success": True, "response": "not-json"}
                    return {"success": True, "response": json.dumps({"topics": [{"topic": "B兜底话题", "contributors": ["10001"], "detail": "降级成功"}], "titles": [], "quotes": [], "quality": None}, ensure_ascii=False)}
                return {"success": True}

            self.cap_mock.side_effect = cap_c_to_a_to_b
            self.p._cfg().analysis.llm_retries = 0
            self.p._cfg().analysis.min_messages_threshold = 2
            job = {"stage": ""}
            res_deg = await self.p._execute_multiday_analysis(job, "stream-1", "123", 3, strategy_raw="存档优先")
            self.assertEqual(res_deg["status"], "ok")
            self.assertEqual(db_queries, 3)  # A 与 B 共享缓存，不重复查库
            self.assertEqual(a_failed_calls, 3)
            self.assertEqual(res_deg["context"]["strategy_used"], "stratified_sample")
            self.assertEqual(job["strategy_label"], "分层抽样")

            # 3d: B 也失败 -> 降级回 1 天日报
            self.p._generate_daily_and_send = AsyncMock(return_value="ok")

            async def cap_all_fail(name, **kwargs):
                if name == "message.get_by_time_in_chat":
                    mid = (kwargs["start_time"] + kwargs["end_time"]) / 2
                    return [make_msg("10001", "甲", f"唯一消息内容 {mid:.0f}", mid)]
                if name == "llm.generate":
                    return {"success": True, "response": "bad"}
                return {"success": True}

            self.cap_mock.side_effect = cap_all_fail
            job2 = {"stage": ""}
            outcome = await self.p._generate_multiday_and_send(job2, "stream-1", "123", 3, "atri", strategy_raw="分层抽样")
            self.assertEqual(outcome, "ok")
            self.p._generate_daily_and_send.assert_awaited_once()
            self.assertEqual(job2["strategy_used"], "fallback_1d")

    def test_4_soft_budget_cropping_speaker_cap_and_min_one_per_day(self):
        # 7 天，每天 350 条，其中 u1 刷屏 250 条，其余 10 人各 10 条
        slices = multiday_mod.build_rolling_slices(time.time(), 7)
        day_inputs = []
        for d_idx, sl in enumerate(slices):
            cands = []
            for m_idx in range(250):
                cands.append({
                    "u": "spammer", "n": "话痨", "t": f"天{d_idx}话痨发言第{m_idx}条长一点测试",
                    "tm": "12:00", "dt": "11-20 12:00", "ts": sl["start_ts"] + m_idx * 10,
                    "hour": (m_idx % 24), "bucket": (m_idx % 24) // 4, "score": 3.0,
                })
            for u_idx in range(10):
                for k in range(10):
                    cands.append({
                        "u": f"u{u_idx}", "n": f"群友{u_idx}", "t": f"天{d_idx}群友{u_idx}讨论{k}",
                        "tm": "18:00", "dt": "11-20 18:00", "ts": sl["start_ts"] + 3000 + u_idx * 20 + k,
                        "hour": (u_idx * 2) % 24, "bucket": ((u_idx * 2) % 24) // 4, "score": 4.0,
                    })
            day_inputs.append({"day": sl["day"], "candidates": cands, "total": len(cands)})

        alloc = multiday_mod.allocate_multiday_samples(day_inputs, total_budget=1000, per_day_cap_cfg=0)
        self.assertLessEqual(alloc["total_selected"], 1000)
        self.assertTrue(alloc["cropped"])
        self.assertTrue(alloc["budget_notes"])
        for sl in slices:
            day_rows = alloc["samples_by_day"][sl["day"]]
            self.assertGreaterEqual(len(day_rows), 1)
            spam_cnt = sum(1 for r in day_rows if r["u"] == "spammer")
            # 单人占比 <= 当日配额 20%
            self.assertLessEqual(spam_cnt, max(1, int((1000 // 7) * 0.20)) + 5)

        # 小上下文字符上限裁剪：仍保证每天至少 1 条
        small = multiday_mod.allocate_multiday_samples(
            day_inputs, total_budget=1000, per_day_cap_cfg=0, chunk_chars=4000, max_chunks=1,
        )
        for sl in slices:
            self.assertGreaterEqual(len(small["samples_by_day"][sl["day"]]), 1)

    def test_5_notify_and_forward_filtered_in_multiday(self):
        now = time.time()
        msgs = [
            make_msg("10001", "甲", "戳一戳通知", now - 10, notify=True),
            {"session_id": "s1", "timestamp": now - 9,
             "message_info": {"user_info": {"user_id": "10002", "user_nickname": "乙"},
                              "additional_config": {"napcat_notice_payload": {"post_type": "notice", "sub_type": "poke"}}},
             "processed_plain_text": "乙 戳了戳 甲", "raw_message": []},
            make_msg("10003", "丙", "合并转发", now - 8, forward=True),
            make_msg("10004", "丁", "这是一条正常的群聊讨论消息", now - 7),
        ]
        parsed = multiday_mod.parse_day_messages(
            msgs,
            is_notify_fn=module._is_notify_message,
            is_forward_fn=module._is_forward_message,
            display_name_fn=module._display_name,
        )
        self.assertEqual(parsed["total"], 1)
        self.assertEqual(list(parsed["users"].keys()), ["10004"])
        self.assertEqual(len(parsed["candidates"]), 1)

    async def test_6_commands_weekly_vs_multiday_and_reject_override_flags(self):
        msg_ctx = {"stream_id": "stream-1", "user_id": "10001",
                   "message": {"message_info": {"group_info": {"group_id": "123"}}}}

        # 拒绝单次覆盖参数
        h, reason, _ = await self.p.cmd_daily_report(**msg_ctx, matched_groups={"args": "7 不替代"})
        self.assertTrue(h)
        self.assertEqual(reason, "不支持单次覆盖参数")

        # /周报 独立存档且不置 last_daily
        self.p._generate_multiday_and_send = AsyncMock(return_value="ok")
        h2, reason2, _ = await self.p.cmd_weekly_report(**msg_ctx, matched_groups={"theme": ""})
        self.assertEqual((h2, reason2), (True, "周报生成中"))
        await self._wait_jobs()
        self.assertEqual(self.p._generate_multiday_and_send.await_args.kwargs["report_kind"], "weekly")
        self.assertFalse(self.p._generate_multiday_and_send.await_args.kwargs["mark_sent"])

        # /日报 7 遵循 manual_marks_daily_sent
        self.p._cfg().report.manual_marks_daily_sent = True
        await self.p.cmd_daily_report(**msg_ctx, matched_groups={"args": "7"})
        await self._wait_jobs()
        self.assertTrue(self.p._generate_multiday_and_send.await_args.kwargs["mark_sent"])

        self.p._cfg().report.manual_marks_daily_sent = False
        await self.p.cmd_daily_report(**msg_ctx, matched_groups={"args": "7"})
        await self._wait_jobs()
        self.assertFalse(self.p._generate_multiday_and_send.await_args.kwargs["mark_sent"])

    async def test_7_weekly_schedule_skips_daily_wakes_on_edit_and_falls_back_on_fail(self):
        today_name = multiday_mod.WEEKDAY_NAMES[date.today().weekday()]
        self.p._cfg().multi_day.weekly_enabled = True
        self.p._cfg().multi_day.weekly_day = today_name
        self.p._cfg().multi_day.weekly_time = "08:00"
        self.p._cfg().analysis.stagger_seconds = 0

        # 周报所在日，定时日报必须跳过
        self.p._generate_daily_and_send = AsyncMock(return_value="ok")
        await self.p._generate_daily_all()
        self.p._generate_daily_and_send.assert_not_awaited()

        # 改星期/时间即刻唤醒（无需重启）
        self.p._schedule_wakeup.clear()
        other_day = multiday_mod.WEEKDAY_NAMES[(date.today().weekday() + 1) % 7]
        res_save = await self.p.webui_config_save("multi_day", "weekly_day", other_day)
        self.assertTrue(res_save["saved"])
        self.assertTrue(self.p._schedule_wakeup.is_set())

        # 周报失败自动补发当日 1 天日报
        self.p._cfg().multi_day.weekly_day = today_name
        self.p._generate_multiday_and_send = AsyncMock(return_value="failed")
        self.p._rebuild_day_from_db = AsyncMock()
        self.p._generate_daily_and_send = AsyncMock(return_value="ok")
        await self.p._generate_weekly_all()
        self.p._generate_daily_and_send.assert_awaited_once()
        self.assertEqual(self.p._stats["chats"]["stream-1"]["last_daily"], TODAY)

    async def test_8_multiday_comic_reuses_window_topics_and_pads_panels(self):
        # 存一份只有 2 个话题的 7 天报告，分镜数设为 5，验证不查库、选中最热 2 话题并补满 5 格
        self.p._cfg().comic.enabled = True
        self.p._cfg().comic.storyboard_panels = 5
        slot_day = f"{TODAY}~7d"
        self.p._report_store.save("123", slot_day, png_b64(), "<html>7d</html>", {
            "group_name": "测试大群", "theme": "atri", "message_count": 300, "participant_count": 8,
            "report_kind": "multiday", "window_days": 7,
            "analysis": {
                "topics": [
                    {"topic": "最热话题一", "contributors": ["10001", "10002"], "detail": "很热闹"},
                    {"topic": "较热话题二", "contributors": ["10001"], "detail": "也不错"},
                ],
                "topic_pool": [
                    {"topic": "冷门话题", "contributors": [], "detail": "短", "mention_days": 1},
                    {"topic": "最热话题一", "contributors": ["10001", "10002", "10003"], "detail": "连续三天都在讨论的大事件", "mention_days": 3},
                    {"topic": "较热话题二", "contributors": ["10001"], "detail": "不错", "mention_days": 2},
                ],
                "titles": [], "quotes": [{"content": "全窗口金句", "sender": "[10001]", "reason": "棒"}], "quality": None,
            },
        })
        db_called = False

        async def cap_comic(name, **kwargs):
            nonlocal db_called
            if name == "message.get_by_time_in_chat":
                db_called = True
            if name == "llm.generate":
                # 仅返回 2 格，验证 pad_to_count 自动补足到 5 格
                return {"success": True, "response": json.dumps({
                    "title": "七日漫画",
                    "panels": [{"scene": "第一格讨论", "caption": "其一", "dialogue": "冲"},
                               {"scene": "第二格吐槽", "caption": "其二", "dialogue": "好"}],
                }, ensure_ascii=False)}
            return {"success": True}

        self.cap_mock.side_effect = cap_comic
        handled, reason, _ = await self.p.cmd_comic(
            stream_id="stream-1", user_id="10001",
            message={"message_info": {"group_info": {"group_id": "123"}}},
            matched_groups={"days": "7"},
        )
        self.assertEqual((handled, reason), (True, "多天漫画生成中"))
        await self._wait_jobs()
        self.assertFalse(db_called)
        cmeta = self.p._report_store.comic_detail("123", slot_day)
        self.assertEqual(cmeta["window_days"], 7)
        self.assertEqual(cmeta["panel_count"], 5)
        self.assertEqual(len(cmeta["storyboard"]["panels"]), 5)
        self.assertEqual(cmeta["selected_topics"][0], "最热话题一")

    async def test_9_llm_tools_toggle_old_host_compat_and_weekly_lookup(self):
        # 存一份周报，验证工具可检索
        weekly_slot = f"{TODAY}~weekly"
        self.p._report_store.save("123", weekly_slot, png_b64(), "<html>w</html>", {
            "group_name": "测试大群", "theme": "atri", "message_count": 210, "participant_count": 6,
            "report_kind": "weekly", "window_days": 7, "strategy_label": "存档优先",
            "coverage_note": "覆盖 11-17 ~ 11-24（共 7 天）",
            "analysis": {"topics": [{"topic": "本周高光", "contributors": ["10001"], "detail": "很棒"}],
                         "titles": [], "quotes": [], "quality": None},
        })
        md = await self.p.tool_get_report(group="123", date="周报")
        self.assertIn("周报", md)
        self.assertIn("本周高光", md)
        self.assertIn("存档优先", md)

        # 切换总开关：调 component.disable / component.enable
        cap_calls = []

        async def cap_comp(capability, **kwargs):
            cap_calls.append((capability, kwargs.get("name")))
            return {"success": True}

        self.cap_mock.side_effect = cap_comp
        await self.p.webui_config_save("multi_day", "llm_tools_enabled", "false")
        self.assertIn(("component.disable", "group_daily_report_lookup"), cap_calls)
        self.assertIn(("component.disable", "group_daily_analysis_trigger"), cap_calls)
        blocked_msg = await self.p.tool_get_report(group="123", date="周报")
        self.assertIn("已在插件设置中关闭", blocked_msg)

        # 老宿主缺失 component.* 能力时不抛异常
        self.cap_mock.side_effect = RuntimeError("capability not found")
        await self.p._sync_llm_tools_state()

    async def test_10_settings_copywriting_audit_webui_preview_stats_and_n1_regression(self):
        schema = self.p.get_webui_config_schema()
        md_sec = schema["sections"]["multi_day"]
        adv_sec = schema["sections"]["multi_day_advanced"]
        self.assertFalse(md_sec["collapsed"])
        self.assertTrue(adv_sec["collapsed"])

        forbidden_codes = ("archive_first", "per_day_summary", "stratified_sample")
        for sec in (md_sec, adv_sec):
            for fname, f in sec["fields"].items():
                self.assertTrue(f["label"], f"{fname} 缺少中文 label")
                self.assertTrue(f["hint"], f"{fname} 缺少详细 hint")
                self.assertIn("默认", f["hint"], f"{fname} hint 须写明默认值")
                for code in forbidden_codes:
                    self.assertNotIn(code, f["label"])
                    self.assertNotIn(code, f["hint"])
                    if f.get("choices"):
                        self.assertNotIn(code, f["choices"])

        # 静态 Pydantic Schema 同样符合 §8.1 中文标签与无内部代号要求
        from maibot_sdk.config import generate_plugin_config_schema
        static_sc = generate_plugin_config_schema(module.DailyAnalysisConfig)
        for sec_key in ("multi_day", "multi_day_advanced"):
            for fname, f in static_sc["sections"][sec_key]["fields"].items():
                self.assertTrue(f["label"], f"static {fname} 缺少 label")
                self.assertTrue(f["hint"], f"static {fname} 缺少 hint")
                for code in forbidden_codes:
                    self.assertNotIn(code, f["label"])
                    self.assertNotIn(code, f["hint"])

        # WebUI 约束 & 预览/统计接口
        wcfg = json.loads((ROOT / "webui.json").read_text(encoding="utf-8"))
        for page in wcfg["pages"]:
            self.assertLessEqual(len(page.get("queries", {})), 10)
            self.assertLessEqual(len(page.get("actions", {})), 20)
            html_txt = (ROOT / page["entry"]).read_text(encoding="utf-8")
            self.assertNotIn("<form", html_txt)
            self.assertNotIn('type="submit"', html_txt)
        lib_html = (ROOT / "webui/report_library.html").read_text(encoding="utf-8")
        self.assertIn("window.confirm", lib_html)

        self._seed_daily_archive(TODAY)
        self.p._report_store.save("123", f"{TODAY}~weekly", png_b64(), "<html>w</html>", {
            "group_name": "测试大群", "theme": "atri", "message_count": 100, "participant_count": 4,
            "report_kind": "weekly", "window_days": 7, "analysis": {"topics": [], "titles": [], "quotes": [], "quality": None},
        })
        pv = await self.p.webui_multiday_preview("123", days=7, strategy="存档优先")
        self.assertTrue(pv["ok"])
        self.assertEqual(pv["estimated_calls"], 1)
        st = await self.p.webui_stats(range_days=7)
        self.assertEqual(st["by_kind"]["daily"], 1)
        self.assertEqual(st["by_kind"]["weekly"], 1)
        evs = await self.p.webui_events(limit=20)
        self.assertTrue(any(e["kind"] == "周报" for e in evs["events"]))

        # N=1 回归：/日报 仍走 1 天路径且不注入 multiday banner
        self.p._generate_daily_and_send = AsyncMock(return_value="ok")
        self.p._rebuild_day_from_db = AsyncMock()
        h, r, _ = await self.p.cmd_daily_report(
            stream_id="stream-1", user_id="10001",
            message={"message_info": {"group_info": {"group_id": "123"}}},
            matched_groups={"args": "scrapbook"},
        )
        self.assertEqual((h, r), (True, "日报生成中"))
        await self._wait_jobs()
        self.p._generate_daily_and_send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
