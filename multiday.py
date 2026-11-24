"""多天分析 / 周报核心算法与提示词工具（与宿主/IO 解耦，便于单测）。"""

from __future__ import annotations

from datetime import datetime
import math
import re
from typing import Any, Callable


STRATEGY_ARCHIVE_FIRST = "archive_first"
STRATEGY_PER_DAY_SUMMARY = "per_day_summary"
STRATEGY_STRATIFIED_SAMPLE = "stratified_sample"

STRATEGY_LABELS: dict[str, str] = {
    STRATEGY_ARCHIVE_FIRST: "存档优先",
    STRATEGY_PER_DAY_SUMMARY: "按天提炼",
    STRATEGY_STRATIFIED_SAMPLE: "分层抽样",
}

STRATEGY_ALIASES: dict[str, str] = {
    "archive_first": STRATEGY_ARCHIVE_FIRST,
    "存档优先": STRATEGY_ARCHIVE_FIRST,
    "c": STRATEGY_ARCHIVE_FIRST,
    "per_day_summary": STRATEGY_PER_DAY_SUMMARY,
    "按天提炼": STRATEGY_PER_DAY_SUMMARY,
    "a": STRATEGY_PER_DAY_SUMMARY,
    "stratified_sample": STRATEGY_STRATIFIED_SAMPLE,
    "分层抽样": STRATEGY_STRATIFIED_SAMPLE,
    "b": STRATEGY_STRATIFIED_SAMPLE,
}

WEEKDAY_NAMES: tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
WEEKDAY_MAP: dict[str, int] = {
    "周一": 0, "星期一": 0, "礼拜一": 0, "1": 0, "mon": 0, "monday": 0,
    "周二": 1, "星期二": 1, "礼拜二": 1, "2": 1, "tue": 1, "tuesday": 1,
    "周三": 2, "星期三": 2, "礼拜三": 2, "3": 2, "wed": 2, "wednesday": 2,
    "周四": 3, "星期四": 3, "礼拜四": 3, "4": 3, "thu": 3, "thursday": 3,
    "周五": 4, "星期五": 4, "礼拜五": 4, "5": 4, "fri": 4, "friday": 4,
    "周六": 5, "星期六": 5, "礼拜六": 5, "6": 5, "sat": 5, "saturday": 5,
    "周日": 6, "周天": 6, "星期日": 6, "星期天": 6, "0": 6, "7": 6, "sun": 6, "sunday": 6,
}

_LOW_INFO_RE = re.compile(
    r"^(?:[哈啊哦嗯哼嘿呀哇呜呃草艹69?？!！.。~～…+\-_=*/\\]+|[0-9]+|[a-zA-Z]{1,3}|收到|好的|好吧|行吧|可以|谢谢|确实|不错|牛逼|nb|666)$",
    re.IGNORECASE,
)
_MBTI_RE = re.compile(r"\b([EI][NS][FT][JP])\b", re.IGNORECASE)
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def normalize_strategy(raw: Any, allow_follow: bool = False) -> str:
    """把中文名或内部/旧代号归一化为内部标准策略码；未知值抛出 ValueError。"""
    text = str(raw or "").strip()
    if allow_follow and text in ("", "跟随多天默认", "default", "follow"):
        return ""
    key = text.casefold()
    if key in STRATEGY_ALIASES:
        return STRATEGY_ALIASES[key]
    if text in STRATEGY_ALIASES:
        return STRATEGY_ALIASES[text]
    raise ValueError(f"不支持的多天策略: {raw!r}")


def strategy_label(code: str) -> str:
    return STRATEGY_LABELS.get(code, code or "存档优先")


def strategy_chain(requested: str) -> list[str]:
    """硬性降级链：C -> A -> B；A -> B；B -> []。"""
    code = normalize_strategy(requested)
    if code == STRATEGY_ARCHIVE_FIRST:
        return [STRATEGY_ARCHIVE_FIRST, STRATEGY_PER_DAY_SUMMARY, STRATEGY_STRATIFIED_SAMPLE]
    if code == STRATEGY_PER_DAY_SUMMARY:
        return [STRATEGY_PER_DAY_SUMMARY, STRATEGY_STRATIFIED_SAMPLE]
    return [STRATEGY_STRATIFIED_SAMPLE]


def parse_weekday(raw: Any) -> int:
    text = str(raw or "").strip().casefold()
    if text in WEEKDAY_MAP:
        return WEEKDAY_MAP[text]
    raise ValueError(f"周报星期无效: {raw!r}")


def normalize_weekday_name(raw: Any) -> str:
    return WEEKDAY_NAMES[parse_weekday(raw)]


def parse_hhmm(raw: Any) -> tuple[int, int]:
    text = str(raw or "").strip()
    m = _TIME_RE.fullmatch(text)
    if not m:
        raise ValueError(f"周报时间需为 HH:MM（00:00~23:59），收到 {raw!r}")
    return int(m.group(1)), int(m.group(2))


def normalize_hhmm(raw: Any) -> str:
    h, m = parse_hhmm(raw)
    return f"{h:02d}:{m:02d}"


def build_rolling_slices(end_ts: float, days: int) -> list[dict[str, Any]]:
    """按触发时刻向前 N*24h 切片，每天一个 [start_ts, end_ts) 分片。"""
    days = max(1, min(90, int(days)))
    start_ts = end_ts - days * 86400.0
    slices: list[dict[str, Any]] = []
    seen_days: set[str] = set()
    for idx in range(days):
        s_ts = start_ts + idx * 86400.0
        e_ts = start_ts + (idx + 1) * 86400.0
        mid_dt = datetime.fromtimestamp((s_ts + e_ts) / 2.0)
        day_label = mid_dt.strftime("%Y-%m-%d")
        if day_label in seen_days:
            day_label = datetime.fromtimestamp(e_ts - 1.0).strftime("%Y-%m-%d")
        seen_days.add(day_label)
        slices.append({
            "index": idx,
            "day": day_label,
            "start_ts": s_ts,
            "end_ts": e_ts,
        })
    return slices


def score_message(text: str, reply_flag: bool = False, multi_speaker_nearby: bool = False) -> float:
    """信息量评分：长度适中、多参与者互动、非纯灌水/表情优先。"""
    clean = str(text or "").strip()
    length = len(clean)
    if length == 0:
        return -10.0
    if _LOW_INFO_RE.fullmatch(clean):
        return 0.2
    score = 1.0
    if 12 <= length <= 120:
        score += 3.0
    elif 6 <= length < 12:
        score += 1.5
    elif 120 < length <= 260:
        score += 2.0
    elif length <= 3:
        score += 0.3
    else:
        score += 1.0
    if reply_flag:
        score += 1.5
    if multi_speaker_nearby:
        score += 1.0
    if "@" in clean or "？" in clean or "?" in clean:
        score += 0.5
    # 重复单字灌水惩罚（如连续 5 个以上相同字符）
    if re.search(r"(.)\1{4,}", clean):
        score -= 1.2
    return score


def parse_day_messages(
    messages: list[dict[str, Any]],
    *,
    is_notify_fn: Callable[[Any], bool],
    is_forward_fn: Callable[[Any, str], bool],
    display_name_fn: Callable[[str, str], str],
    night_start: int = 0,
    night_end: int = 6,
    sample_text_max: int = 200,
    quote_min_len: int = 15,
    quote_max_len: int = 300,
) -> dict[str, Any]:
    """解析单天分片消息列表（统一过滤通知/转发/命令，统计用户/24h桶并构造分层候选池）。"""
    users: dict[str, dict[str, Any]] = {}
    hours = [0] * 24
    total = 0
    chat_meta: dict[str, str] = {}
    quotes: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    prev_uid = ""

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if is_notify_fn(msg):
            continue
        info = msg.get("message_info") or {}
        user_info = info.get("user_info") or {}
        user_id = str(user_info.get("user_id") or "").strip()
        if not user_id:
            continue
        for key in ("platform", "account_id", "scope"):
            if not chat_meta.get(key):
                val = str(msg.get(key) or "").strip()
                if val:
                    chat_meta[key] = val
        nick = str(user_info.get("user_cardname") or "").strip()
        if not nick:
            nick = str(user_info.get("user_nickname") or user_info.get("nickname") or "").strip()
        nickname = display_name_fn(user_id, nick)
        try:
            ts = float(msg.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        dt = datetime.fromtimestamp(ts) if ts > 0 else datetime.now()
        text = str(msg.get("processed_plain_text") or "").strip()
        segments = msg.get("raw_message")
        sticker_count = 0
        reply_flag = False
        if isinstance(segments, list):
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                seg_type = seg.get("type")
                if seg_type == "emoji":
                    sticker_count += 1
                elif seg_type == "reply":
                    reply_flag = True
        if is_forward_fn(segments, text):
            continue

        user = users.setdefault(
            user_id,
            {"n": nickname, "msg": 0, "char": 0, "sticker": 0, "night": 0, "reply": 0},
        )
        user["n"] = nickname
        user["msg"] += 1
        user["char"] += len(text)
        user["sticker"] += sticker_count
        if reply_flag:
            user["reply"] = int(user.get("reply", 0)) + 1
        if night_start <= dt.hour < night_end:
            user["night"] += 1
        total += 1
        hour = min(23, max(0, dt.hour))
        hours[hour] += 1

        if sticker_count == 0 and text and not text.startswith(("[", "/")):
            clean_text = text[:sample_text_max] if len(text) > sample_text_max else text
            multi_nearby = bool(prev_uid and prev_uid != user_id)
            candidates.append({
                "u": user_id,
                "n": nickname,
                "t": clean_text,
                "tm": dt.strftime("%H:%M"),
                "dt": dt.strftime("%m-%d %H:%M"),
                "ts": ts,
                "hour": hour,
                "bucket": hour // 4,  # 6 个 4 小时桶
                "score": score_message(clean_text, reply_flag=reply_flag, multi_speaker_nearby=multi_nearby),
            })
            if quote_min_len <= len(text) <= quote_max_len and not any(q.get("t") == text for q in quotes):
                quotes.append({"u": nickname, "uid": user_id, "t": text, "l": len(text)})
                quotes.sort(key=lambda q: q["l"], reverse=True)
                del quotes[5:]
        prev_uid = user_id

    return {
        "total": total,
        "users": users,
        "hours": hours,
        "quotes": quotes,
        "candidates": candidates,
        "chat_meta": chat_meta,
    }


def _dedupe_key(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


def stratify_day_candidates(
    candidates: list[dict[str, Any]],
    quota: int,
    *,
    seen_global: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    """日内 6 时段分层 + 发言者轮询（单人 <= 当日名额 20%）。

    返回 (selected, unique_total, was_cropped)。
    """
    if not candidates or quota <= 0:
        return [], 0, False

    deduped: list[dict[str, Any]] = []
    local_seen: set[str] = set()
    for item in candidates:
        key = _dedupe_key(item.get("t", ""))
        if not key or key in local_seen:
            continue
        if seen_global is not None and key in seen_global:
            continue
        local_seen.add(key)
        deduped.append(item)

    if not deduped:
        return [], 0, False

    unique_total = len(deduped)
    target = min(quota, unique_total)
    speaker_cap = max(1, int(quota * 0.20))

    # 6 个时段桶，按桶内消息量比例分名额
    buckets: dict[int, list[dict[str, Any]]] = {b: [] for b in range(6)}
    for idx, item in enumerate(deduped):
        b = int(item.get("bucket", int(item.get("hour", 0)) // 4))
        b = min(5, max(0, b))
        row = dict(item)
        row["_seq"] = idx
        buckets[b].append(row)

    active_buckets = [b for b in range(6) if buckets[b]]
    bucket_quotas: dict[int, int] = {b: 0 for b in active_buckets}
    total_avail = sum(len(buckets[b]) for b in active_buckets)
    for b in active_buckets:
        share = max(1, round(target * len(buckets[b]) / max(1, total_avail)))
        bucket_quotas[b] = min(len(buckets[b]), share)

    # 调整桶配额总和严格等于 target
    while sum(bucket_quotas.values()) > target:
        largest = max(active_buckets, key=lambda b: bucket_quotas[b])
        if bucket_quotas[largest] <= 1:
            break
        bucket_quotas[largest] -= 1
    while sum(bucket_quotas.values()) < target:
        candidates_b = [b for b in active_buckets if bucket_quotas[b] < len(buckets[b])]
        if not candidates_b:
            break
        pick_b = max(candidates_b, key=lambda b: len(buckets[b]) - bucket_quotas[b])
        bucket_quotas[pick_b] += 1

    # 桶内按发言者分组并按分数降序，轮询取样
    selected: list[dict[str, Any]] = []
    selected_seqs: set[int] = set()
    speaker_counts: dict[str, int] = {}

    for b in active_buckets:
        need = bucket_quotas[b]
        if need <= 0:
            continue
        by_speaker: dict[str, list[dict[str, Any]]] = {}
        for row in buckets[b]:
            by_speaker.setdefault(str(row["u"]), []).append(row)
        for rows in by_speaker.values():
            rows.sort(key=lambda r: (-float(r.get("score", 0.0)), int(r["_seq"])))
        speaker_order = sorted(
            by_speaker.keys(),
            key=lambda uid: (-float(by_speaker[uid][0].get("score", 0.0)), uid),
        )
        taken = 0
        made_progress = True
        while taken < need and made_progress:
            made_progress = False
            for uid in speaker_order:
                if taken >= need:
                    break
                if speaker_counts.get(uid, 0) >= speaker_cap:
                    continue
                queue = by_speaker[uid]
                while queue and int(queue[0]["_seq"]) in selected_seqs:
                    queue.pop(0)
                if not queue:
                    continue
                chosen = queue.pop(0)
                selected.append(chosen)
                selected_seqs.add(int(chosen["_seq"]))
                speaker_counts[uid] = speaker_counts.get(uid, 0) + 1
                taken += 1
                made_progress = True

    # 若部分桶因发言者上限未取满名额，从全天剩余高分且未触及单人上限的候选中补位
    if len(selected) < target:
        rem = [
            row
            for b in active_buckets
            for row in buckets[b]
            if int(row["_seq"]) not in selected_seqs
        ]
        rem.sort(key=lambda r: (-float(r.get("score", 0.0)), int(r["_seq"])))
        for row in rem:
            if len(selected) >= target:
                break
            uid = str(row["u"])
            if speaker_counts.get(uid, 0) >= speaker_cap:
                continue
            selected.append(row)
            selected_seqs.add(int(row["_seq"]))
            speaker_counts[uid] = speaker_counts.get(uid, 0) + 1

    # 极端兜底：仅当所有候选都被跳过（不会发生）时保底 1 条
    if not selected and deduped:
        best = max(deduped, key=lambda r: float(r.get("score", 0.0)))
        selected.append(dict(best, _seq=0))

    selected.sort(key=lambda r: (float(r.get("ts", 0.0)), int(r.get("_seq", 0))))
    for row in selected:
        row.pop("_seq", None)
    if seen_global is not None:
        for row in selected:
            seen_global.add(_dedupe_key(row.get("t", "")))
    was_cropped = len(selected) < unique_total
    return selected, unique_total, was_cropped


def allocate_multiday_samples(
    day_slices: list[dict[str, Any]],
    *,
    total_budget: int = 1000,
    per_day_cap_cfg: int = 0,
    chunk_chars: int = 16000,
    max_chunks: int = 4,
) -> dict[str, Any]:
    """跨天配额分配 + 跨天去重 + 日内分层 + 字符上限保护。

    输入 day_slices: [{"day": "YYYY-MM-DD", "candidates": [...], "total": int}, ...]
    返回 {
        "samples_by_day": {day: [...]},
        "flat_samples": [...],
        "budget_notes": [...],
        "cropped": bool,
        "total_selected": int,
        "total_unique_candidates": int,
    }
    """
    total_budget = max(100, min(5000, int(total_budget or 1000)))
    window_days = max(1, len(day_slices))
    auto_cap = min(1000, max(50, math.ceil(total_budget / window_days * 1.8)))
    day_cap = max(50, min(1000, int(per_day_cap_cfg))) if per_day_cap_cfg and int(per_day_cap_cfg) >= 50 else auto_cap
    base_quota = max(1, total_budget // window_days)

    # 先对每天做跨天全局去重统计（按日期从新到旧或时间顺序保留首次出现？按时间正序保留首次出现）
    seen_global: set[str] = set()
    unique_by_day: dict[str, list[dict[str, Any]]] = {}
    for sl in day_slices:
        day = str(sl["day"])
        cands = sl.get("candidates") or []
        day_list: list[dict[str, Any]] = []
        for item in cands:
            k = _dedupe_key(item.get("t", ""))
            if not k or k in seen_global:
                continue
            seen_global.add(k)
            day_list.append(item)
        unique_by_day[day] = day_list

    non_empty_days = [str(sl["day"]) for sl in day_slices if unique_by_day.get(str(sl["day"]))]
    quotas: dict[str, int] = {day: 0 for day in non_empty_days}

    if non_empty_days:
        pool = total_budget
        # 第一轮：每有效日先按 base_quota 与 day_cap 分配（若预算极小仍保证 >=1 条）
        for day in non_empty_days:
            avail = len(unique_by_day[day])
            alloc = min(avail, base_quota, day_cap)
            if alloc == 0 and avail > 0:
                alloc = 1
            quotas[day] = alloc
            pool -= alloc
        # 防止极端情况超出 total_budget
        if pool < 0:
            while pool < 0:
                max_d = max(non_empty_days, key=lambda d: quotas[d])
                if quotas[max_d] <= 1:
                    break
                quotas[max_d] -= 1
                pool += 1
        # 第二轮：把空闲天余量按剩余需求补贴给未满单日上限的天
        while pool > 0:
            needers = [
                d for d in non_empty_days
                if quotas[d] < min(len(unique_by_day[d]), day_cap)
            ]
            if not needers:
                break
            progressed = False
            for d in needers:
                if pool <= 0:
                    break
                if quotas[d] < min(len(unique_by_day[d]), day_cap):
                    quotas[d] += 1
                    pool -= 1
                    progressed = True
            if not progressed:
                break

    samples_by_day: dict[str, list[dict[str, Any]]] = {}
    day_cropped_notes: list[str] = []
    total_unique_candidates = sum(len(v) for v in unique_by_day.values())
    cropped_any = False

    for sl in day_slices:
        day = str(sl["day"])
        cands = unique_by_day.get(day) or []
        if not cands:
            samples_by_day[day] = []
            continue
        chosen, uniq_cnt, was_cropped = stratify_day_candidates(cands, quotas.get(day, 1))
        samples_by_day[day] = chosen
        if was_cropped:
            cropped_any = True
            day_cropped_notes.append(f"{day[5:]} 保留 {len(chosen)}/{uniq_cnt} 条")

    # 字符预算硬保护：保证每个有消息的覆盖日至少保留 1 条代表
    max_total_chars = max(4000, int(chunk_chars or 16000)) * max(1, int(max_chunks or 4))

    def _row_line_len(row: dict[str, Any]) -> int:
        return len(f"[{row.get('dt') or row.get('tm', '--:--')}] [{row.get('u', '')}]: {row.get('t', '')}\n")

    current_chars = sum(_row_line_len(r) for rows in samples_by_day.values() for r in rows)
    char_cropped = False
    if current_chars > max_total_chars:
        char_cropped = True
        cropped_any = True
        # 优先从最旧天、且样本 >1 条的日期里砍掉最低分消息
        ordered_days = [str(sl["day"]) for sl in day_slices]
        while current_chars > max_total_chars:
            removable: list[tuple[float, int, str, int]] = []
            for day_idx, d in enumerate(ordered_days):
                rows = samples_by_day.get(d) or []
                if len(rows) <= 1:
                    continue
                for r_idx, row in enumerate(rows):
                    removable.append((float(row.get("score", 0.0)), day_idx, d, r_idx))
            if not removable:
                break
            removable.sort(key=lambda t: (t[0], t[1]))
            _, _, drop_day, drop_idx = removable[0]
            dropped = samples_by_day[drop_day].pop(drop_idx)
            current_chars -= _row_line_len(dropped)

    flat_samples: list[dict[str, Any]] = []
    for sl in day_slices:
        day = str(sl["day"])
        for row in samples_by_day.get(day) or []:
            item = dict(row)
            item.setdefault("day", day)
            flat_samples.append(item)

    budget_notes: list[str] = []
    if day_cropped_notes:
        shown = "、".join(day_cropped_notes[:4])
        if len(day_cropped_notes) > 4:
            shown += f" 等 {len(day_cropped_notes)} 天"
        budget_notes.append(
            f"因总条数上限（{total_budget} 条），已分层精选 {len(flat_samples)}/{total_unique_candidates} 条（{shown}）"
        )
    if char_cropped:
        budget_notes.append(
            f"因单次分段字符上限（{max_total_chars} 字），已自动裁剪较早低信息量发言并保证每天至少保留 1 条代表"
        )

    return {
        "samples_by_day": samples_by_day,
        "flat_samples": flat_samples,
        "budget_notes": budget_notes,
        "cropped": cropped_any,
        "total_selected": len(flat_samples),
        "total_unique_candidates": total_unique_candidates,
    }


def format_coverage_note(
    slices: list[dict[str, Any]],
    covered_days: list[str],
    missing_days: list[str],
) -> str:
    if not slices:
        return ""
    first_md = str(slices[0]["day"])[5:]
    last_md = str(slices[-1]["day"])[5:]
    if missing_days:
        miss_md = "、".join(d[5:] for d in missing_days)
        return f"覆盖 {first_md} ~ {last_md}（缺 {miss_md}）"
    return f"覆盖 {first_md} ~ {last_md}（共 {len(covered_days)} 天）"


def format_users_summary_text(user_stats: dict[str, dict[str, Any]], min_msg: int = 5, limit: int = 40) -> str:
    def _ratio(part: Any, whole: Any) -> str:
        try:
            return str(round(int(part) / max(1, int(whole)) * 100)) + "%"
        except (TypeError, ValueError):
            return "0%"

    rows = []
    for uid, info in user_stats.items():
        msg = int(info.get("msg", 0) or 0)
        if msg < min_msg:
            continue
        avg_chars = round(int(info.get("char", 0) or 0) / max(1, msg), 1)
        rows.append((msg, uid, info, avg_chars))
    if not rows:
        for uid, info in user_stats.items():
            msg = int(info.get("msg", 0) or 0)
            if msg <= 0:
                continue
            avg_chars = round(int(info.get("char", 0) or 0) / max(1, msg), 1)
            rows.append((msg, uid, info, avg_chars))
    rows.sort(key=lambda r: r[0], reverse=True)
    return "\n".join(
        f"- [{uid}] {info.get('n', uid)}: 发言 {msg} 条, 平均 {avg} 字/条, "
        f"表情包 {info.get('sticker', 0)} 张(占 {_ratio(info.get('sticker', 0), msg)}), "
        f"深夜发言 {info.get('night', 0)} 条(占 {_ratio(info.get('night', 0), msg)}), "
        f"回复他人 {info.get('reply', 0)} 次(占 {_ratio(info.get('reply', 0), msg)})"
        for msg, uid, info, avg in rows[:limit]
    )


def format_samples_text(samples: list[dict[str, Any]], max_chars: int = 64000) -> str:
    lines = "\n".join(
        f"[{u.get('dt') or (str(u.get('day') or '')[5:] + ' ' + str(u.get('tm', '--:--'))).strip()}] [{u['u']}]: {u['t']}"
        for u in samples
    )
    if len(lines) > max_chars:
        lines = lines[-max_chars:]
        nl = lines.find("\n")
        if nl != -1:
            lines = lines[nl + 1:]
        lines = "[更早样本已自动裁剪]\n" + lines
    return lines


def build_day_digest_prompt(
    day: str,
    messages_text: str,
    users_text: str,
    persona_prefix: str = "",
) -> str:
    """策略 A：单天轻量提炼提示词（输出精简话题/金句/当日印象）。"""
    prefix = f"{persona_prefix}\n\n" if persona_prefix else ""
    return (
        f"{prefix}请对 {day} 这一天的群聊记录做轻量提炼，为后续多天汇总提供结构化素材。\n"
        "请只返回纯 JSON 对象（不要 markdown 代码块），包含以下字段：\n"
        "- topics: 1~4 个当日主要话题数组，每项 {\"topic\": \"8字内标题\", \"contributors\": [\"用户ID\"], \"detail\": \"1~2句事实概述，提及群友用 [用户ID]\"}\n"
        "- quotes: 1~2 句当日金句数组，每项 {\"content\": \"原话\", \"sender\": \"[用户ID]\", \"reason\": \"简短亮点\"}\n"
        "- summary: 1 句话总结当天群聊氛围\n\n"
        f"当日活跃用户统计：\n{users_text or '暂无'}\n\n"
        f"当日精选聊天记录：\n{messages_text}"
    )


def build_multiday_merge_prompt(
    *,
    report_title: str,
    coverage_note: str,
    users_text: str,
    archive_digests: list[dict[str, Any]] | None,
    samples_text: str,
    max_topics: int,
    max_titles: int,
    max_quotes: int,
    enable_topics: bool,
    enable_titles: bool,
    enable_quotes: bool,
    enable_quality: bool,
    persona_prefix: str = "",
) -> str:
    """C/A/B 共用：一次合并/主分析提示词，产出统一风格的多天 JSON 分析结果。"""
    prefix = (
        f"{persona_prefix}\n\n"
        "【风格硬要求】请完全以你当前人设的口吻与视角撰写所有点评和描述；如果历史日报摘要里带有以前不同的口吻，请忽略旧风格，统一重写为当前人设风格。\n\n"
        if persona_prefix
        else "请统一使用自然、生动且一致的语气撰写全部板块。\n\n"
    )

    sections_req: list[str] = []
    if enable_topics:
        sections_req.append(
            f'- "topics": 合并跨天重复话题后保留 1~{max_topics} 个全窗口最热话题，按热度降序。'
            '每项包含 {"topic": "10字内标题", "contributors": ["用户ID"], "detail": "合并多天前因后果的完整描述，提及用户必须用 [用户ID]", "mention_days": 涉及天数整数}。'
        )
    else:
        sections_req.append('- "topics": 返回 []')

    if enable_titles:
        sections_req.append(
            f'- "titles": 为全窗口最突出的 1~{max_titles} 位群友授予称号（每人最多一个）。'
            '每项包含 {"name": "昵称", "user_id": "用户ID", "title": "称号", "mbti": "4字母MBTI如INTJ", "reason": "40字以上结合多天数据与言行的具体理由"}。'
        )
    else:
        sections_req.append('- "titles": 返回 []')

    if enable_quotes:
        sections_req.append(
            f'- "quotes": 按「内容质量 + 时间新近」精选 1~{max_quotes} 句金句。'
            '每项包含 {"content": "金句原文", "sender": "[用户ID]", "reason": "选择理由，提及用户用 [用户ID]"}。'
        )
    else:
        sections_req.append('- "quotes": 返回 []')

    if enable_quality:
        sections_req.append(
            '- "quality": {"title": "报告主题标题", "subtitle": "副标题", "summary": "一句总结金句", '
            '"dimensions": [{"name": "2-6字抽象维度", "percentage": 0-100整数, "comment": "符合当前人设的锐评"}]}（3~6个维度）。'
        )
    else:
        sections_req.append('- "quality": null')

    digest_block = ""
    if archive_digests:
        lines: list[str] = []
        for item in archive_digests:
            d = item.get("day", "")
            src = item.get("source", "archive")
            lines.append(f"### {d}（来源：{'日报存档' if src == 'archive' else '当日提炼'}，消息数 {item.get('message_count', 0)}）")
            if item.get("topics"):
                lines.append("话题：" + "；".join(
                    f"{t.get('topic', '')}({','.join(str(c) for c in (t.get('contributors') or [])[:4])}): {t.get('detail', '')[:120]}"
                    for t in item["topics"][:6] if isinstance(t, dict)
                ))
            if item.get("titles"):
                lines.append("称号：" + "；".join(
                    f"[{t.get('user_id', '')}]{t.get('name', '')}-{t.get('title', '')}"
                    for t in item["titles"][:5] if isinstance(t, dict)
                ))
            if item.get("quotes"):
                lines.append("金句：" + "；".join(
                    f"{q.get('sender', '')}: {str(q.get('content', ''))[:100]}"
                    for q in item["quotes"][:4] if isinstance(q, dict)
                ))
            if item.get("summary"):
                lines.append("小结：" + str(item["summary"])[:160])
        digest_block = "【各天汇总素材】\n" + "\n".join(lines) + "\n\n"

    sample_block = f"【分层抽样聊天记录】\n{samples_text}\n\n" if samples_text else ""

    return (
        f"{prefix}"
        f"请为群聊生成「{report_title}」（{coverage_note}）。\n\n"
        "## 输出字段要求（严格返回单个纯 JSON 对象，不要 markdown 代码块）\n"
        + "\n".join(sections_req)
        + "\n\n"
        + f"【全窗口活跃群友统计】\n{users_text or '暂无足够统计'}\n\n"
        + digest_block
        + sample_block
        + "注意：只输出标准 JSON 对象，使用英文双引号，不得在 JSON 外添加说明文字。"
    )


def build_window_topic_pool(topics: list[dict[str, Any]], max_items: int = 12) -> list[dict[str, Any]]:
    """构造全窗口候选话题池，按热度（跨天频次 + 参与者数 + 详情完整度）降序去重。"""
    merged: dict[str, dict[str, Any]] = {}
    for raw in topics:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("topic") or "").strip()
        if not name:
            continue
        norm_key = re.sub(r"\s+", "", name).casefold()[:16]
        if not norm_key:
            continue
        contribs = [str(c).strip() for c in (raw.get("contributors") or []) if str(c or "").strip()]
        detail = str(raw.get("detail") or "").strip()
        try:
            mention_days = max(1, int(raw.get("mention_days") or 1))
        except (TypeError, ValueError):
            mention_days = 1
        existing = merged.get(norm_key)
        if existing is None:
            merged[norm_key] = {
                "topic": name,
                "contributors": list(dict.fromkeys(contribs))[:8],
                "detail": detail,
                "mention_days": mention_days,
            }
        else:
            existing["contributors"] = list(dict.fromkeys([*existing["contributors"], *contribs]))[:8]
            existing["mention_days"] = max(existing["mention_days"], mention_days) + 1
            if len(detail) > len(existing["detail"]):
                existing["detail"] = detail

    pool = list(merged.values())
    pool.sort(
        key=lambda t: (
            int(t.get("mention_days") or 1) * 3 + len(t.get("contributors") or []) * 2,
            len(str(t.get("detail") or "")),
        ),
        reverse=True,
    )
    return pool[:max_items]


def normalize_multiday_analysis(
    raw: Any,
    context: dict[str, Any],
    *,
    max_topics: int = 3,
    max_titles: int = 3,
    max_quotes: int = 3,
    enable_topics: bool = True,
    enable_titles: bool = True,
    enable_quotes: bool = True,
    enable_quality: bool = True,
    extra_topics: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """清洗多天合并 LLM 返回；关键结构完全无效时返回 None 以触发降级链。"""
    if not isinstance(raw, dict):
        return None
    names = context.get("user_names") or {}

    topics: list[dict[str, Any]] = []
    if enable_topics and isinstance(raw.get("topics"), list):
        for item in raw["topics"]:
            if not isinstance(item, dict):
                continue
            topic = str(item.get("topic") or "").strip()
            if not topic:
                continue
            contribs = [str(c).strip() for c in (item.get("contributors") or []) if str(c or "").strip()][:5]
            topics.append({
                "topic": topic[:40],
                "contributors": contribs,
                "detail": str(item.get("detail") or "").strip(),
                "mention_days": int(item.get("mention_days") or 1) if str(item.get("mention_days") or "").isdigit() else 1,
            })

    titles: list[dict[str, Any]] = []
    if enable_titles and isinstance(raw.get("titles"), list):
        used_uids: set[str] = set()
        for item in raw["titles"]:
            if not isinstance(item, dict):
                continue
            title_text = str(item.get("title") or "").strip()
            if not title_text:
                continue
            uid = str(item.get("user_id") or "").strip().strip("[]")
            name = str(item.get("name") or "").strip()
            if uid and uid in names:
                name = names[uid]
            if not uid and name:
                uid = next((k for k, v in names.items() if v == name), "")
            if uid and uid in used_uids:
                continue
            if uid:
                used_uids.add(uid)
            mbti_raw = str(item.get("mbti") or "").strip().upper()
            m = _MBTI_RE.search(mbti_raw)
            mbti = m.group(1).upper() if m else "INTJ"
            titles.append({
                "name": name or uid or "群友",
                "user_id": uid,
                "title": title_text[:30],
                "mbti": mbti,
                "reason": str(item.get("reason") or "").strip(),
            })

    quotes: list[dict[str, Any]] = []
    if enable_quotes and isinstance(raw.get("quotes"), list):
        for item in raw["quotes"]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            quotes.append({
                "content": content,
                "sender": str(item.get("sender") or "").strip() or "群友",
                "reason": str(item.get("reason") or "").strip(),
            })

    quality = None
    if enable_quality and isinstance(raw.get("quality"), dict):
        qraw = raw["quality"]
        dims = []
        for d in (qraw.get("dimensions") or []):
            if not isinstance(d, dict):
                continue
            dname = str(d.get("name") or "").strip()
            if not dname:
                continue
            try:
                pct = max(0, min(100, int(d.get("percentage", 0))))
            except (TypeError, ValueError):
                pct = 0
            dims.append({
                "name": dname[:16],
                "percentage": pct,
                "comment": str(d.get("comment") or "").strip(),
            })
        if dims:
            quality = {
                "title": str(qraw.get("title") or "").strip()[:40],
                "subtitle": str(qraw.get("subtitle") or "").strip()[:80],
                "summary": str(qraw.get("summary") or "").strip(),
                "dimensions": dims[:6],
            }

    # 若启用板块却全空，视为无效响应触发降级
    if (enable_topics and not topics) and (enable_titles and not titles) and (enable_quotes and not quotes) and not quality:
        return None

    if enable_quotes and not quotes:
        sample = context.get("sample") or []
        quotes = [
            {
                "sender": u.get("n") or "群友",
                "content": u.get("t") or "",
                "reason": "\n" + "跨天精选代表发言，先收进宝藏瓶！",
            }
            for u in sorted(sample, key=lambda x: len(str(x.get("t") or "")), reverse=True)[:2]
            if u.get("t")
        ]

    pool_input = [*topics, *(extra_topics or [])]
    topic_pool = build_window_topic_pool(pool_input, max_items=max(12, max_topics * 2))

    return {
        "topics": topics[:max_topics] if enable_topics else [],
        "titles": titles[:max_titles] if enable_titles else [],
        "quotes": quotes[:max_quotes] if enable_quotes else [],
        "quality": quality if enable_quality else None,
        "topic_pool": topic_pool,
    }
