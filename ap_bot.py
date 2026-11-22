#!/usr/bin/env python3
"""维护 zh.minecraft.wiki 上的巡查豁免者时间线数据页。

数据页：https://zh.minecraft.wiki/w/User:Villager-bloc-h/ap.json
（``{{timeline|page=User:Villager-bloc-h/ap.json}}`` 所读取的 JSON）

本程序只做“增量”维护，不重建历史：

1. **在任核查（Special:ListUsers）**：用 ``list=allusers&augroup=autopatrol``
   （即 Special:ListUsers?group=autopatrol）取现任名单，与数据页中所有
    ``"end": "now"`` **或结束日期晚于今天**的条目比对。
   * 名单内 → 结束日期必须是 ``"now"``（被改成过去/未来日期时自动恢复）；
   * 不在名单 → 把 ``"now"`` 或未来日期改成实际离任日期（若属于静默到期，则取
     授权的到期日）；
   * 账号不存在但确实被改名 → 跟随改名日志更新标签；无法确认的账号只告警、不删除；
   * 在名单内但页面没有 → 取其最近一次获权日期补一条现任记录。

2. **新获权补录**：从 ``--since``（默认 2026-10-01）起扫描权限日志，把新增的
   ``autopatrol`` 授权（以及窗口内的撤销、续期）补进时间线；仍在任的结束日期写
   ``"now"``。

3. **列表页同步**：按时间线数据重排
   ``User:Villager-bloc-h/Autopatrolled user``（按年份、首次获权日期、用户名）。
   保留页首、年份以外的章节（如“导航”）、每个用户的链接写法与注记；离任且累计
   任职不超过 10 天的用户加斜体。时间线里合并为 ``Thaumic Copper`` 的条目在列表
   页仍使用 ``ThaumiCopper``（见 ``LIST_NAME_OVERRIDES``）。

日期一律取 **UTC** 日历日（与现有数据一致），只允许修改上述内容，其余
字段（legend / style / margins / 旧条目）原样保留；内容无变化时不会保存。

用法::

    python ap_bot.py --dry-run          # 只看会改什么
    python ap_bot.py                    # 实际保存（数据页 + 列表页）
    python ap_bot.py --skip-list        # 只维护数据页
    python ap_bot.py --list-members     # 只打印现任巡查豁免者名单
    python ap_bot.py --since 2026-10-01 # 指定新授权的回顾起点

依赖 pywikibot（见 requirements.txt）和同目录的 user-config.py。
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# 让 pywikibot 无论从哪个工作目录启动都能找到同目录的 user-config.py
os.environ.setdefault("PYWIKIBOT_DIR", str(Path(__file__).resolve().parent))

import pywikibot
from pywikibot.exceptions import (
    EditConflictError,
    Error as PywikibotError,
    OtherPageSaveError,
)

PAGE_TITLE = "User:Villager-bloc-h/ap.json"
LIST_PAGE_TITLE = "User:Villager-bloc-h/Autopatrolled_user"
DEFAULT_SINCE = "2026-10-01"
GROUP = "autopatrol"
COLOR = "var(--autopatrol)"
INFINITY = "infinity"
MAX_LOG_SCAN = 5000
SHORT_TENURE_DAYS = 10

# 时间线把 ThaumiCopper 与 Thaumic Copper 合并成一条，列表页仍沿用前者。
# 键为时间线条目名，值为列表页使用的账号名（链接与注记都按它查找）。
LIST_NAME_OVERRIDES = {
    "Thaumic Copper": "ThaumiCopper",
}

YEAR_HEADING_RE = re.compile(r"^==\s*(\d{4})\s*==\s*$")
LIST_LINE_RE = re.compile(
    r"^\*\s*(?:'')?(?P<link>\[\[[^\]]+\]\])(?:'')?(?P<note>.*?)\s*$")
LIST_NAME_RE = re.compile(r"^\[\[User:([^|\]]+)")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def utc_date(stamp: str) -> str:
    """把 ``2026-11-20T13:51:32Z`` 转成 UTC 日期 ``2026-11-20``。"""
    return stamp[:10]


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def label_of(bar: list[dict[str, Any]]) -> str:
    return bar[-1]["text"]


def is_current(bar: list[dict[str, Any]]) -> bool:
    return bar[-2]["end"] == "now"


def is_open(bar: list[dict[str, Any]], today: str) -> bool:
    """条目是否表示“仍在任”：结束日期为 ``now`` 或被误写成未来日期。"""
    end = bar[-2]["end"]
    return end == "now" or end > today


def validate_data(data: dict[str, Any]) -> None:
    """检查数据页结构，避免残缺条目导致下标错误或写坏页面。"""
    if not isinstance(data, dict) or not isinstance(data.get("bars"), list):
        raise ValueError("缺少 bars 数组")
    for index, bar in enumerate(data["bars"], 1):
        if not isinstance(bar, list) or len(bar) < 2:
            raise ValueError(
                f"第 {index} 个条目只有 {len(bar) if isinstance(bar, list) else '?'} "
                "个元素，缺少时间段或标签")
        label = bar[-1]
        if not isinstance(label, dict) or not isinstance(label.get("text"), str):
            raise ValueError(f"第 {index} 个条目缺少 text 标签")
        for segment in bar[:-1]:
            if (not isinstance(segment, dict)
                    or "start" not in segment or "end" not in segment):
                raise ValueError(f"第 {index} 个条目的时间段缺少 start/end")


def expiry_of(params: dict[str, Any]) -> str | None:
    """取本次日志中 autopatrol 的到期时间（无则 None）。"""
    for meta in params.get("newmetadata") or []:
        if meta.get("group") == GROUP:
            return meta.get("expiry")
    return None


def has_autopatrol(user: dict[str, Any]) -> bool:
    return GROUP in (user.get("groups") or [])


# --------------------------------------------------------------------------- #
# 站点查询
# --------------------------------------------------------------------------- #
def fetch_users(site: pywikibot.site.BaseSite,
                names: list[str]) -> dict[str, dict[str, Any]]:
    """批量查询账号信息（每批 40 个，远低于 API 的 50 上限）。"""
    result: dict[str, dict[str, Any]] = {}
    names = [n for n in dict.fromkeys(names) if n]
    for i in range(0, len(names), 40):
        for user in site.users(names[i:i + 40]):
            result[user["name"]] = user
    return result


def fetch_group_members(site: pywikibot.site.BaseSite,
                        group: str = GROUP) -> set[str]:
    """取当前用户组成员，等价于 Special:ListUsers?group=autopatrol。

    API：``list=allusers&augroup=autopatrol``（自动分页）。
    """
    try:
        return {user["name"] for user in site.allusers(group=group)}
    except PywikibotError:
        return set()


def resolve_rename(site: pywikibot.site.BaseSite, name: str) -> str | None:
    """沿着 renameuser 日志把旧用户名换成新用户名（最多 3 跳）。"""
    current = name
    for _ in range(3):
        try:
            entries = list(site.logevents(
                logtype="renameuser", page=f"User:{current}", total=5))
        except PywikibotError:
            return None
        new_name = None
        for entry in entries:
            params = entry.get("params") or {}
            if params.get("olduser") == current and params.get("newuser"):
                new_name = params["newuser"]
        if not new_name:
            return None
        current = new_name
    return current


def removal_date(site: pywikibot.site.BaseSite, name: str,
                 today: str) -> str | None:
    """查询某账号最近一次与 autopatrol 有关的权限变更，推断离任日期。

    返回 ``None`` 表示该账号的权限日志里完全没有 autopatrol 记录——通常意味着
    这个用户名已被他人重新注册，此时不应擅自改动原有条目。
    """
    try:
        entries = list(site.logevents(
            logtype="rights", page=f"User:{name}", total=10))
    except PywikibotError:
        return today

    saw_autopatrol = False
    date = today
    for entry in entries:
        params = entry.get("params") or {}
        old = params.get("oldgroups") or []
        new = params.get("newgroups") or []
        date = utc_date(entry.timestamp().isoformat())
        if GROUP in old or GROUP in new:
            saw_autopatrol = True
        if GROUP in old and GROUP not in new:      # 显式除权
            return date
        if GROUP in new:                           # 到期静默失效 / 续期
            expiry = expiry_of(params)
            if expiry and expiry != INFINITY and expiry[:10] <= today:
                return expiry[:10]
            return date
    return date if saw_autopatrol else None


def current_grant_start(site: pywikibot.site.BaseSite,
                        name: str) -> str | None:
    """查询某现任成员最近一次获得 autopatrol 的日期（当前任期的起点）。"""
    try:
        entries = list(site.logevents(
            logtype="rights", page=f"User:{name}", total=20))
    except PywikibotError:
        return None
    for entry in entries:
        params = entry.get("params") or {}
        old = params.get("oldgroups") or []
        new = params.get("newgroups") or []
        if GROUP in new and GROUP not in old:
            return utc_date(entry.timestamp().isoformat())
    return None


def recent_rights_events(site: pywikibot.site.BaseSite,
                         since: str) -> list[pywikibot.logentries.LogEntry]:
    """返回 since（含）之后的所有权限日志，按时间升序。"""
    events = []
    for entry in site.logevents(logtype="rights"):
        if utc_date(entry.timestamp().isoformat()) < since:
            break
        events.append(entry)
        if len(events) >= MAX_LOG_SCAN:
            break
    events.reverse()
    return events


# --------------------------------------------------------------------------- #
# 规则 1：在任核查
# --------------------------------------------------------------------------- #
def reconcile_holders(site: pywikibot.site.BaseSite,
                      data: dict[str, Any], today: str,
                      changes: list[str],
                      members: set[str]) -> None:
    """让页面中每条记录的“在任/离任”状态与 Special:ListUsers 一致。

    除了 ``"end": "now"`` 之外，**结束日期晚于今天**的条目也会被当作“仍在任”
    处理：这类日期要么是误编辑，要么是旧的临时授权写法，都需要与名单核对。
    """
    bars = data["bars"]
    known = {label_of(bar) for bar in bars}

    # 1) 不在名单中，但结束日期是 now 或未来日期 → 写入实际离任日期
    for bar in bars:
        if not is_open(bar, today):
            continue
        name = label_of(bar)
        if name in members:
            continue

        user = fetch_users(site, [name]).get(name)

        # 账号查不到：可能是改名，尝试跟随；无法确认就只告警
        if user is None or "missing" in user:
            new_name = resolve_rename(site, name)
            if not new_name:
                changes.append(f"警告：{name} 不在现任名单中且账号不存在，已跳过")
                continue
            if new_name in known and new_name != name:
                changes.append(f"警告：{name} 改名后与现有条目 {new_name} 冲突，已跳过")
                continue
            if new_name in members:
                bar[-1]["text"] = new_name
                known.discard(name)
                known.add(new_name)
                changes.append(f"改名：{name} → {new_name}")
                continue
            name = new_name
            user = fetch_users(site, [new_name]).get(new_name, {})

        # 用户名可能被他人重新注册：注册时间晚于首次获权，日志属于旧账号
        registered = (user.get("registration") or "")[:10] if user else ""
        if registered and registered > bar[0]["start"]:
            changes.append(
                f"警告：{name} 的注册日期（{registered}）晚于首次获权日期"
                f"（{bar[0]['start']}），疑似用户名被重新注册，已跳过")
            continue

        end = removal_date(site, name, today)
        if end is None:
            changes.append(
                f"警告：{name} 的权限日志中没有巡查豁免记录，"
                "疑似用户名被重新注册，已跳过")
            continue
        bar[-2]["end"] = end
        changes.append(f"离任：{name}（{end}）")

    # 2) 在名单中，但最后一段的结束日期不是 now（被改成过去/未来日期）→ 恢复
    for bar in bars:
        name = label_of(bar)
        if name not in members or bar[-2]["end"] == "now":
            continue

        start = current_grant_start(site, name)
        if not start:
            changes.append(
                f"警告：{name} 在现任名单中，但权限日志里找不到授权记录，已跳过")
            continue

        for segment in bar[:-1]:
            if segment["start"] == start:
                segment["end"] = "now"
                changes.append(f"恢复在任：{name}（{start} 起）")
                break
        else:
            bar.insert(-1, {"start": start, "end": "now", "color": COLOR})
            changes.append(f"补录在任：{name}（{start} 起）")


def add_missing_holders(site: pywikibot.site.BaseSite,
                        data: dict[str, Any], members: set[str],
                        changes: list[str]) -> None:
    """把在 Special:ListUsers 中、但页面尚未标为在任的用户补进时间线。"""
    page_now = {label_of(bar) for bar in data["bars"] if is_current(bar)}
    missing = sorted(members - page_now)
    if not missing:
        return

    segments: dict[str, list[list[str]]] = {}
    for name in missing:
        start = current_grant_start(site, name)
        if not start:
            changes.append(
                f"警告：{name} 在现任名单中，但权限日志里找不到授权记录，无法补录")
            continue
        segments[name] = [[start, "now"]]
    merge_segments(data, segments, changes)


# --------------------------------------------------------------------------- #
# 规则 3：同步“巡查豁免者列表”页面
# --------------------------------------------------------------------------- #
def parse_list_page(text: str) -> dict[str, Any]:
    """拆出列表页的页首、现有条目（链接/注记）和年份以外的章节。"""
    preamble: list[str] = []
    trailing: list[str] = []
    links: dict[str, str] = {}
    notes: dict[str, str] = {}
    mode = "preamble"

    for line in text.splitlines():
        if YEAR_HEADING_RE.match(line):
            mode = "year"
            continue
        if line.startswith("==") and line.rstrip().endswith("=="):
            mode = "trailing"
            trailing.append(line)
            continue

        if mode == "preamble":
            preamble.append(line)
        elif mode == "year":
            match = LIST_LINE_RE.match(line)
            if not match:
                continue
            link = match.group("link")
            name_match = LIST_NAME_RE.match(link)
            if not name_match:
                continue
            name = name_match.group(1)
            links[name] = link
            note = match.group("note").strip()
            if note:
                notes[name] = note
        else:
            trailing.append(line)

    return {
        "preamble": preamble,
        "trailing": trailing,
        "links": links,
        "notes": notes,
    }


def tenure_days(bar: list[dict[str, Any]], today: str) -> int:
    """累计任职天数（进行中的时间段算到 today）。"""
    total = 0
    for segment in bar[:-1]:
        start = date.fromisoformat(segment["start"])
        end = date.fromisoformat(
            today if segment["end"] == "now" else segment["end"])
        total += max(0, (end - start).days)
    return total


def render_list_page(data: dict[str, Any], parsed: dict[str, Any],
                     today: str) -> str:
    """按时间线数据重排列表页：按年份、首次获权日期、用户名排序。

    保留页首、年份以外的章节（如“导航”），以及每个用户的链接写法和注记
    （如“（前任协管员）”）；离任且累计任职不超过 10 天的用户用斜体。
    """
    entries = []
    for bar in data["bars"]:
        name = label_of(bar)
        first = bar[0]["start"]
        ongoing = any(segment["end"] == "now" for segment in bar[:-1])
        short = (not ongoing) and tenure_days(bar, today) <= SHORT_TENURE_DAYS
        entries.append((first, name, short))
    entries.sort(key=lambda item: (item[0], item[1]))

    blocks: list[str] = []
    lines = list(parsed["preamble"])
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        blocks.append("\n".join(lines))

    year = None
    section: list[str] = []
    for first, name, short in entries:
        if int(first[:4]) != year:
            if section:
                blocks.append("\n".join(section))
            year = int(first[:4])
            section = [f"== {year} =="]
        page_name = LIST_NAME_OVERRIDES.get(name, name)
        link = parsed["links"].get(page_name) or \
            f"[[User:{page_name}|{page_name}]]"
        if short:
            link = f"''{link}''"
        section.append(f"* {link}{parsed['notes'].get(page_name, '')}")
    if section:
        blocks.append("\n".join(section))

    trailing = list(parsed["trailing"])
    while trailing and not trailing[0].strip():
        trailing.pop(0)
    while trailing and not trailing[-1].strip():
        trailing.pop()
    if trailing:
        blocks.append("\n".join(trailing))

    return "\n\n".join(blocks).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# 规则 2：新获权 / 撤销 / 续期
# --------------------------------------------------------------------------- #
def collect_window_segments(
    site: pywikibot.site.BaseSite, since: str, today: str,
    changes: list[str],
) -> dict[str, list[list[str]]]:
    """扫描 since 之后的日志，返回 {用户名: [[start, end], ...]}。"""
    open_seg: dict[str, list[str]] = {}      # name -> [start, expiry]
    segments: dict[str, list[list[str]]] = {}

    for entry in recent_rights_events(site, since):
        title = entry.get("title") or ""
        if not title.startswith("User:"):
            continue
        name = title[len("User:"):]
        params = entry.get("params") or {}
        old = params.get("oldgroups") or []
        new = params.get("newgroups") or []
        has_old, has_new = GROUP in old, GROUP in new
        date = utc_date(entry.timestamp().isoformat())

        if has_new and not has_old:                     # 授权
            if name in open_seg:
                open_seg[name][1] = expiry_of(params)
            else:
                open_seg[name] = [date, expiry_of(params)]
        elif has_old and not has_new:                   # 除权
            if name in open_seg:
                start, _ = open_seg.pop(name)
                segments.setdefault(name, []).append([start, date])
        elif has_old and has_new:                       # 仅改到期时间
            if name in open_seg:
                open_seg[name][1] = expiry_of(params)

    # 仍未关闭的条目：按当前权限与到期时间决定结束日期
    info = fetch_users(site, list(open_seg))
    for name, (start, expiry) in open_seg.items():
        user = info.get(name, {})
        if "missing" in user:
            new_name = resolve_rename(site, name)
            if new_name:
                renamed = fetch_users(site, [new_name]).get(new_name, {})
                if "missing" not in renamed:
                    changes.append(f"改名：{name} → {new_name}")
                    name, user = new_name, renamed

        if has_autopatrol(user):
            end = "now"
        elif expiry and expiry != INFINITY and expiry[:10] <= today:
            end = expiry[:10]            # 到期静默失效
        else:
            end = today
        segments.setdefault(name, []).append([start, end])

    return segments


def merge_segments(data: dict[str, Any],
                   segments: dict[str, list[list[str]]],
                   changes: list[str]) -> None:
    bars = data["bars"]
    by_label = {label_of(bar): bar for bar in bars}

    for name, segs in segments.items():
        segs.sort(key=lambda seg: seg[0])
        bar = by_label.get(name)
        if bar is None:
            bar = [{"start": segs[0][0], "end": segs[0][1], "color": COLOR}]
            for start, end in segs[1:]:
                bar.append({"start": start, "end": end, "color": COLOR})
            bar.append({"at": segs[0][0], "text": name, "align": "right"})
            bars.append(bar)
            by_label[name] = bar
            changes.append(f"新任：{name}（{segs[0][0]}）")
            continue

        starts = {segment["start"] for segment in bar[:-1]}
        for start, end in segs:
            if start in starts:
                continue
            bar.insert(-1, {"start": start, "end": end, "color": COLOR})
            starts.add(start)
            changes.append(f"新任：{name}（{start}）")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="更新巡查豁免者时间线数据页，并同步巡查豁免者列表页。")
    parser.add_argument("--page", default=PAGE_TITLE,
                        help=f"数据页标题（默认 {PAGE_TITLE}）")
    parser.add_argument("--list-page", default=LIST_PAGE_TITLE,
                        help=f"巡查豁免者列表页标题（默认 {LIST_PAGE_TITLE}）")
    parser.add_argument("--skip-list", action="store_true",
                        help="不同步巡查豁免者列表页")
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help=f"新授权的扫描起点，含当天（默认 {DEFAULT_SINCE}）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要做的修改，不保存")
    parser.add_argument("--skip-current", action="store_true",
                        help="跳过在任核查")
    parser.add_argument("--skip-new", action="store_true",
                        help="跳过新授权扫描")
    parser.add_argument("--summary", default=None,
                        help="自定义编辑摘要")
    parser.add_argument("--no-bot", action="store_true",
                        help="不标记为机器人编辑")
    parser.add_argument("--list-members", action="store_true",
                        help="只打印 Special:ListUsers 中的现任巡查豁免者后退出")
    return parser.parse_args(argv)


def build_summary(changes: list[str]) -> str:
    added = sum(1 for c in changes if c.startswith("新任"))
    left = sum(1 for c in changes if c.startswith("离任"))
    renamed = sum(1 for c in changes if c.startswith("改名"))
    parts = []
    if added:
        parts.append(f"新任 {added}")
    if left:
        parts.append(f"离任 {left}")
    if renamed:
        parts.append(f"改名 {renamed}")
    return "机器人：更新巡查豁免时间线（{}）".format(
        "、".join(parts) if parts else "例行核查")


def show_diff(old: str, new: str, limit: int = 80) -> None:
    diff = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile="当前页面", tofile="更新后", lineterm="", n=2))
    for line in diff[:limit]:
        print(line)
    if len(diff) > limit:
        print(f"...（省略 {len(diff) - limit} 行）")


def ensure_login(site: pywikibot.site.BaseSite) -> None:
    """登录机器人账号。

    凭据来自 user-config.py + password.py（BotPassword 元组）；若没有配置，
    pywikibot 会在终端询问密码。登录成功后 cookie 会缓存到
    ``pywikibot-<用户名>.lwp``，供后续无人值守运行复用。
    """
    site.login()


JSON_RIGHT_HINT = (
    "当前账号/机器人密码缺少 JSON 编辑权限（editmyuserjson）。\n"
    "请到 Special:BotPasswords 编辑该机器人密码，勾选“编辑自己的用户 JSON”"
    "（或授予 edituserjson）后重试。")


def warn_missing_json_right(site: pywikibot.site.BaseSite,
                            page: pywikibot.Page) -> None:
    """保存前检查机器人密码是否具备编辑 JSON 页面的权限。"""
    if not page.title().lower().endswith(".json"):
        return
    rights = set(site.userinfo.get("rights") or [])
    if not rights & {"editmyuserjson", "edituserjson"}:
        print(f"警告：{JSON_RIGHT_HINT}", file=sys.stderr)


def save_page(page: pywikibot.Page, text: str, summary: str,
              bot: bool) -> None:
    page.text = text
    page.save(summary=summary, minor=True, bot=bot, watch="nochange",
              asynchronous=False)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        print("错误：--since 必须是 YYYY-MM-DD 格式", file=sys.stderr)
        return 1
    site = pywikibot.Site()

    if args.list_members:
        try:
            users = sorted(site.allusers(group=GROUP), key=lambda u: u["name"])
        except PywikibotError:
            users = []
        if not users:
            print("错误：无法从 Special:ListUsers 获取现任名单", file=sys.stderr)
            return 1
        print(f"现任巡查豁免者（Special:ListUsers，共 {len(users)} 人）：")
        for user in users:
            registered = str(user.get("registration") or "")[:10]
            edits = user.get("editcount", "?")
            print(f"  - {user['name']}（注册 {registered}，编辑 {edits}）")
        return 0

    page = pywikibot.Page(site, args.page)
    if not page.exists():
        print(f"错误：页面 {args.page} 不存在", file=sys.stderr)
        return 1

    old_text = page.text
    try:
        data = json.loads(old_text)
    except json.JSONDecodeError as exc:
        print(f"错误：页面内容不是合法 JSON：{exc}", file=sys.stderr)
        return 1

    try:
        validate_data(data)
    except ValueError as exc:
        print(f"错误：数据页结构异常：{exc}。请先修复页面后重试。",
              file=sys.stderr)
        return 1

    today = today_utc()
    changes: list[str] = []
    members: set[str] = set()

    if not args.skip_current:
        members = fetch_group_members(site)
        if not members:
            print("错误：无法从 Special:ListUsers 获取现任巡查豁免者名单",
                  file=sys.stderr)
            return 1
        reconcile_holders(site, data, today, changes, members)
        add_missing_holders(site, data, members, changes)
    if not args.skip_new:
        segments = collect_window_segments(site, args.since, today, changes)
        merge_segments(data, segments, changes)

    # 条目顺序：先按首次获权日期，再按用户名（与页面现有顺序一致）
    data["bars"].sort(key=lambda bar: (bar[0]["start"], label_of(bar)))

    if members:
        still = {label_of(bar) for bar in data["bars"] if is_open(bar, today)}
        extra = still - members
        absent = members - still
        if not extra and not absent:
            print(f"现任名单：{len(members)} 人（Special:ListUsers，与页面一致）")
        else:
            if absent:
                print("警告：以下现任者未写入页面：" + "、".join(sorted(absent)))
            if extra:
                print("警告：以下条目标为在任但不在名单中：" + "、".join(sorted(extra)))

    new_text = json.dumps(data, ensure_ascii=False, indent="\t")
    json_changed = new_text.rstrip("\n") != old_text.rstrip("\n")

    list_page = None
    list_text = None
    if not args.skip_list:
        list_page = pywikibot.Page(site, args.list_page)
        if not list_page.exists():
            changes.append(f"警告：列表页 {args.list_page} 不存在，已跳过")
        else:
            list_text = render_list_page(
                data, parse_list_page(list_page.text), today)
            if list_text.rstrip("\n") == list_page.text.rstrip("\n"):
                list_text = None
            else:
                changes.append("列表页：条目或排版有变化")

    if not json_changed and list_text is None:
        print(f"{today} 无变化（在任 {sum(1 for b in data['bars'] if is_current(b))} 人）")
        for change in changes:
            print("  -", change)
        return 0

    print(f"{today} 共 {len(changes)} 项修改：")
    for change in changes:
        print("  -", change)
    print()

    if json_changed:
        print("=== 数据页 ===")
        show_diff(old_text, new_text)
    if list_text is not None:
        print("=== 列表页 ===")
        show_diff(list_page.text, list_text)

    if args.dry_run:
        print("\n--dry-run：未保存。")
        return 0

    summary = args.summary or build_summary(changes)
    try:
        ensure_login(site)
        if json_changed:
            warn_missing_json_right(site, page)
            save_page(page, new_text, summary, bot=not args.no_bot)
        if list_text is not None:
            save_page(
                list_page, list_text,
                args.summary or "机器人：同步巡查豁免者列表",
                bot=not args.no_bot)
    except EditConflictError:
        print("保存冲突：页面在此期间被他人修改，请稍后重试。", file=sys.stderr)
        return 2
    except OtherPageSaveError as exc:
        if "mycustomjsonprotected" in str(exc):
            print(f"保存失败：{JSON_RIGHT_HINT}", file=sys.stderr)
        else:
            print(f"保存失败：{exc}", file=sys.stderr)
        return 1
    except PywikibotError as exc:
        print(f"保存失败：{exc}", file=sys.stderr)
        return 1

    print(f"已保存：{summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
