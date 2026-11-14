"""跨会话的内容记忆：看过的小说和章节、歌，看过的视频，对方说过的兴趣和要求，上次没做完的事。

来源分清楚，不把猜的当事实：
- screen：她从画面上认出来的，只有模型确认画面上清楚写着名字时才记（见 actions.Seen）；
- user：对方在聊天里亲口说的；
- system：插件自己记的（上次因为时间到了、被叫停等没做完）。
下次决定玩什么时作为参考；WebUI 里能看、能删。

看过的视频单独一类（watched）：数量多、只用来不重复看，不和小说、兴趣挤同一个上限，过一阵子就忘。
"""

from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncio
import json
import re
import threading
import unicodedata
import uuid

from .actions import SEEN_MEDIA, VIDEO_KINDS, Seen
from .store import place_and_goal

_MAX_ITEMS = 60
# 看过的视频另算：刷视频一次就是十几条，和兴趣挤在一起会把对方说过的话挤掉
_MAX_WATCHED = 300
_WATCHED_SECONDS = 30 * 86400
# 上次没做完的事过了这么久就不提了
_UNFINISHED_SECONDS = 3 * 86400
# 给规划看的最多几条；看过的视频只在最后提最近几个
_PROMPT_ITEMS = 8
_PROMPT_WATCHED = 5
# 标题比对：短标题（比如只有歌名「春日影」）只认完全一样的，免得把同名的其它视频都当成看过
_MATCH_MIN_LENGTH = 8
_SOURCE_LABELS: Dict[str, str] = {"screen": "画面上看到的", "user": "对方说过", "system": "上次没做完"}
_PROGRESS_LABELS: Dict[str, str] = {"opened": "只点开过", "finished": "看完了"}
# 给规划看的看了一部分的长视频最多几个
_PROMPT_RESUMABLE = 3
# 对方让她有空看的：给规划看最多几个；对方说的名字常常不全（「春日影」「那个猫猫视频」），
# 去掉标点后至少这么多个字、和画面上的标题互相包含才算同一个
_PROMPT_LATER = 3
_LATER_MATCH_MIN = 3
_LATER_LABELS: Dict[str, str] = {"": "还没看", "partial": "看了一部分", "finished": "看完了"}


def normalize_title(text: str) -> str:
    """比对标题用：只留文字和数字、统一小写，去掉【】《》、标点、空格这些不同写法的差别。"""
    return "".join(char for char in text.lower() if unicodedata.category(char)[0] in "LN")


def titles_match(first: str, second: str) -> bool:
    """两个标题是不是同一个视频。界面元素的文字会被截短，所以够长时一个包含另一个也算。"""
    a, b = normalize_title(first), normalize_title(second)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= _MATCH_MIN_LENGTH and shorter in longer


@dataclass
class MemoryItem:
    """一条记忆。

    kind：content 看过的小说、歌，watched 看过的视频，interest 对方说过的兴趣或要求，unfinished 上次没做完的事，
    later 对方让她有空看的（progress 空是还没看、partial 看了一部分、finished 看完了，verdict cancelled 是不用看了）。
    """

    id: str
    kind: str
    title: str
    detail: str = ""
    media: str = ""
    app: str = ""
    source: str = ""
    created_at: str = ""
    updated_at: str = ""
    # 看过的视频：short、long 或空（没说），以及大概看了多少秒（停在视频上的时间，不是播放进度）
    form: str = ""
    seconds: int = 0
    # 看过的视频看到哪一步：opened 只点开过、partial 看了一部分、finished 看完了（画面上看得出播完）；
    # 空是 0.3.1 及以前的旧记录，只知道看过，不往「看完了」上猜
    progress: str = ""
    # 不感兴趣、划走或退出的视频是 skipped；和看到哪一步分开记
    verdict: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["source_label"] = _SOURCE_LABELS.get(self.source, self.source)
        data["watch_state"] = self.watch_state()
        return data

    @property
    def waiting(self) -> bool:
        """对方让她有空看、还没看完也没说不用看的。"""
        return self.kind == "later" and self.progress != "finished" and self.verdict != "cancelled"

    @property
    def resumable(self) -> bool:
        """只点开过或看了一部分、也没说不感兴趣的长视频：下次可以接着看，刷到了不拦。短视频划走了就不再回去看。"""
        return (
            self.kind == "watched"
            and self.form == "long"
            and self.progress in ("opened", "partial")
            and self.verdict != "skipped"
        )

    def watch_state(self) -> str:
        """看过的视频看到哪了，例如「看了一部分，约 3 分钟」「看完了」「不感兴趣」；不是视频或旧记录时是空字符串。

        常被放进括号里（「《……》（看了一部分，约 3 分钟）」），所以自己不再带括号。
        """
        if self.kind == "later":
            return "不用看了" if self.verdict == "cancelled" else _LATER_LABELS.get(self.progress, "")
        if self.kind != "watched":
            return ""
        if self.verdict == "skipped":
            return "不感兴趣"
        if self.progress == "partial":
            spent = f"约 {round(self.seconds / 60)} 分钟" if self.seconds >= 60 else f"{self.seconds} 秒"
            return f"看了一部分，{spent}"
        return _PROGRESS_LABELS.get(self.progress, "")

    def describe(self) -> str:
        """给规划看的一行，例如「番茄小说：在看小说《田园小当家》，看到第12章（画面上看到的）」。"""
        label = _SOURCE_LABELS.get(self.source, self.source)
        if self.kind == "content":
            where = f"{self.app}：" if self.app else ""
            detail = f"，看到{self.detail}" if self.detail else ""
            verb = "在听" if self.media == "music" else "在看"
            return f"{where}{verb}{SEEN_MEDIA.get(self.media, '')}《{self.title}》{detail}（{label}）"
        if self.kind == "watched":
            where = f"在{self.app}" if self.app else ""
            state = self.watch_state()
            return f"{where}看过{VIDEO_KINDS.get(self.form, '视频')}《{self.title}》" + (
                f"（{state}）" if state else ""
            )
        if self.kind == "later":
            where = f"{self.app}，" if self.app else ""
            return f"对方让你有空看：{self.title}（{where}{self.watch_state()}）"
        if self.kind == "unfinished":
            done = place_and_goal(self.app, self.title)
            return f"上次没做完：{done}（{self.detail}）" if self.detail else f"上次没做完：{done}"
        return f"{label}：{self.title}"


class MemoryStore:
    """保存在插件数据目录里的 memory.json。写文件放进线程，按序号排先后，旧快照晚到不会盖掉新的。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._items: List[MemoryItem] = []
        self._lock = threading.Lock()
        self._saved_version = 0
        self._written_version = 0
        # 加载时发现文件损坏、已备份重来的说明
        self.recovered = ""

    def items(self) -> List[MemoryItem]:
        """最近更新的在前。"""
        return sorted(self._items, key=lambda item: item.updated_at, reverse=True)

    def remember_content(self, seen: Seen, app: str, now: datetime) -> MemoryItem:
        """记下她从画面认出来的内容；同一个应用里的同一部作品只留一条，更新看到哪了。"""
        item = next(
            (
                item
                for item in self._items
                if item.kind == "content" and item.media == seen.media and item.title == seen.title and item.app == app
            ),
            None,
        )
        stamp = now.isoformat(timespec="seconds")
        if item is None:
            item = MemoryItem(
                id=_new_id(),
                kind="content",
                title=seen.title,
                media=seen.media,
                app=app,
                source="screen",
                created_at=stamp,
            )
            self._items.append(item)
        # 这次没认出章节就保留上次的，不用空值盖掉
        item.detail = seen.detail or item.detail
        item.updated_at = stamp
        # 在看、在听对方让她有空看的那本、那首：算看了一部分
        self._follow_later(item.title, "partial", now)
        self._trim(now)
        return item

    def remember_watched(self, seen: Seen, app: str, form: str, now: datetime) -> MemoryItem:
        """记下看过的视频；同一个应用里标题对得上的算同一个，只更新时间、集数、长短和看到哪一步。

        新记下的是「只点开过」，停下来看了才算看了一部分（见 add_watched_seconds），画面上看得出播完才算看完；
        旧记录（看到哪一步是空的）没有新的依据就不改。不感兴趣单独记，之后再看也不会抹掉。
        """
        item = self.find_watched(app, seen.title, now)
        stamp = now.isoformat(timespec="seconds")
        if item is None:
            item = MemoryItem(
                id=_new_id(),
                kind="watched",
                title=seen.title,
                media="video",
                app=app,
                source="screen",
                created_at=stamp,
                progress="opened",
            )
            self._items.append(item)
        item.detail = seen.detail or item.detail
        item.form = form or item.form
        if seen.finished:
            item.progress = "finished"
        if seen.skipped:
            item.verdict = "skipped"
        item.updated_at = stamp
        if item.progress == "finished":
            self._follow_later(item.title, "finished", now)
        self._trim(now)
        return item

    def add_watched_seconds(self, item_id: str, seconds: float, now: Optional[datetime] = None) -> None:
        """停在这个视频上看了多久；只点开过的看了一会儿就算看了一部分（对方让她有空看的那个也跟着算）。"""
        for item in self._items:
            if item.id == item_id:
                item.seconds += round(seconds)
                if item.progress == "opened" and item.seconds > 0:
                    item.progress = "partial"
                if item.progress == "partial" and now is not None:
                    self._follow_later(item.title, "partial", now)

    def add_later(self, text: str, app: str, now: datetime) -> MemoryItem:
        """记下对方让她有空看的视频、小说、歌；同样的只留一条，之前说过不用看的重新算上。"""
        title = " ".join(text.split())[:80]
        stamp = now.isoformat(timespec="seconds")
        item = next(
            (
                item
                for item in self._items
                if item.kind == "later" and normalize_title(item.title) == normalize_title(title)
            ),
            None,
        )
        if item is None:
            item = MemoryItem(id=_new_id(), kind="later", title=title, source="user", created_at=stamp)
            self._items.append(item)
        item.app = app.strip() or item.app
        item.verdict = ""
        item.updated_at = stamp
        self._trim(now)
        return item

    def cancel_later(self, text: str, now: datetime) -> Optional[MemoryItem]:
        """对方说那个不用看了：标记最近那条对得上的；没有对得上的返回 None。"""
        item = next((item for item in self.items() if item.waiting and _later_matches(item.title, text)), None)
        if item is not None:
            item.verdict = "cancelled"
            item.updated_at = now.isoformat(timespec="seconds")
        return item

    def later_items(self) -> List[MemoryItem]:
        """对方让她有空看、还没看完的，最近的在前。"""
        return [item for item in self.items() if item.waiting]

    def _catch_up_later(self, seen: MemoryItem, progress: str) -> None:
        """加载时补算：记下「有空要看」之后才看的、看完的，跟着更新（看之前就看过的不算）。"""
        for item in self._items:
            if item.waiting and seen.updated_at >= item.created_at and _later_matches(item.title, seen.title):
                if progress == "finished" or item.progress == "":
                    item.progress = progress
                    item.updated_at = max(item.updated_at, seen.updated_at)

    def _follow_later(self, title: str, progress: str, now: datetime) -> None:
        """她在手机上看到、看完了对方让她有空看的那个：跟着更新看到哪了（看完的不会退回看了一部分）。"""
        for item in self._items:
            if not item.waiting or not _later_matches(item.title, title):
                continue
            if progress == "finished" or item.progress == "":
                item.progress = progress
                item.updated_at = now.isoformat(timespec="seconds")

    def find_watched(self, app: str, text: str, now: datetime) -> Optional[MemoryItem]:
        """这个应用里看过的、标题和 ``text`` 对得上的视频（太久以前的不算）。"""
        return next(
            (item for item in self._watched(app, now) if titles_match(item.title, text)),
            None,
        )

    def watched_items(self, app: str, now: datetime) -> List[MemoryItem]:
        """这个应用里最近看过的视频，最近的在前。"""
        return self._watched(app, now)

    def resumable(self, now: datetime, limit: int) -> List[MemoryItem]:
        """所有应用里看了一部分、可以接着看的长视频，最近的在前。"""
        return [item for item in self.items() if item.resumable and not _expired(item, now, _WATCHED_SECONDS)][:limit]

    def _watched(self, app: str, now: datetime) -> List[MemoryItem]:
        return [
            item
            for item in self.items()
            if item.kind == "watched" and item.app == app and not _expired(item, now, _WATCHED_SECONDS)
        ]

    def remember_interest(self, text: str, now: datetime) -> MemoryItem:
        """记下对方在聊天里亲口说的兴趣或要求；同样的话只留一条。"""
        title = " ".join(text.split())[:80]
        stamp = now.isoformat(timespec="seconds")
        item = next((item for item in self._items if item.kind == "interest" and item.title == title), None)
        if item is None:
            item = MemoryItem(id=_new_id(), kind="interest", title=title, source="user", created_at=stamp)
            self._items.append(item)
        item.updated_at = stamp
        self._trim(now)
        return item

    def set_unfinished(self, goal: str, app: str, reason: str, now: datetime) -> None:
        """记下这次没做完的事；只留最近的一件。"""
        self.clear_unfinished()
        stamp = now.isoformat(timespec="seconds")
        self._items.append(
            MemoryItem(
                id=_new_id(),
                kind="unfinished",
                title=goal,
                detail=reason,
                app=app,
                source="system",
                created_at=stamp,
                updated_at=stamp,
            )
        )

    def clear_unfinished(self) -> None:
        self._items = [item for item in self._items if item.kind != "unfinished"]

    def delete(self, item_id: str) -> bool:
        count = len(self._items)
        self._items = [item for item in self._items if item.id != item_id]
        return len(self._items) < count

    def clear(self, kind: str = "") -> int:
        """全部删掉，或者只删某一类（比如只清空看过的视频）；返回删了几条。"""
        count = len(self._items)
        self._items = [item for item in self._items if kind and item.kind != kind]
        return count - len(self._items)

    def prompt_lines(self, now: datetime) -> List[str]:
        """给规划看的几条：上次没做完的（没过期才提）放最前，再是最近看过的和对方说过的，
        最后是看了一部分、可以接着看的长视频和最近看过的视频。"""
        lines: List[str] = []
        watched: List[str] = []
        resumable: List[str] = []
        later: List[str] = []
        for item in self.items():
            if item.kind == "later":
                if item.waiting:
                    where = f"{item.app}，" if item.app else ""
                    later.append(f"{item.title}（{where}{item.watch_state()}）")
                continue
            if item.kind == "unfinished":
                if _expired(item, now, _UNFINISHED_SECONDS):
                    continue
                lines.insert(0, item.describe())
            elif item.kind == "watched":
                if _expired(item, now, _WATCHED_SECONDS):
                    continue
                if item.resumable:
                    resumable.append(f"《{item.title}》（{item.app}，{item.watch_state() or '只点开过'}）")
                else:
                    watched.append(f"《{item.title}》")
            else:
                lines.append(item.describe())
        if later:
            # 对方专门交代的，放在上次没做完的后面、别的前面
            at = 1 if lines and lines[0].startswith("上次没做完") else 0
            lines.insert(at, f"对方让你有空看的（还没看完，挑一个去看对方会高兴）：{'、'.join(later[:_PROMPT_LATER])}")
        tail: List[str] = []
        if resumable:
            tail.append(f"看了一部分的长视频（想看可以接着看）：{'、'.join(resumable[:_PROMPT_RESUMABLE])}")
        if watched:
            tail.append(f"最近看过的视频（不用再专门找来看）：{'、'.join(watched[:_PROMPT_WATCHED])}")
        return lines[: _PROMPT_ITEMS - len(tail)] + tail

    def _trim(self, now: datetime) -> None:
        """看过的视频去掉太久以前的、超过上限时删最旧的；其它的超过上限时也删最旧的，上次没做完的那条一直留着。"""
        self._items = [
            item for item in self._items if item.kind != "watched" or not _expired(item, now, _WATCHED_SECONDS)
        ]
        drop: set = set()
        for limit, group in (
            (_MAX_WATCHED, [item for item in self._items if item.kind == "watched"]),
            (_MAX_ITEMS, [item for item in self._items if item.kind not in ("watched", "unfinished")]),
        ):
            extra = len(group) - limit
            if extra > 0:
                drop.update(item.id for item in sorted(group, key=lambda item: item.updated_at)[:extra])
        if drop:
            self._items = [item for item in self._items if item.id not in drop]

    async def load(self) -> None:
        await asyncio.to_thread(self._read)

    async def save(self) -> None:
        self._saved_version += 1
        snapshot = [asdict(item) for item in self._items]
        await asyncio.to_thread(self._write, self._saved_version, snapshot)

    def _read(self) -> None:
        if not self._path.exists():
            return
        names = {item.name for item in fields(MemoryItem)}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            items = [MemoryItem(**{key: value for key, value in raw.items() if key in names}) for raw in data["items"]]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            # 和使用记录一样：坏了就留个备份，从空记忆重新开始，不让插件加载失败
            backup = self._path.with_name(f"{self._path.name}.broken-{datetime.now():%Y%m%d%H%M%S}")
            self._path.replace(backup)
            self.recovered = f"内容记忆文件已损坏（{exc}），已备份为 {backup.name}，从空记忆重新开始"
            return
        for item in items:
            # 早先的版本把视频也记成 content；现在看过的视频单独一类
            if item.kind == "content" and item.media == "video":
                item.kind = "watched"
        self._items = items
        # 以前比对不上（比如记成了一整句）的，记下以后已经看过的补上
        for item in sorted(items, key=lambda item: item.updated_at):
            if item.kind == "watched" and item.progress in ("partial", "finished"):
                self._catch_up_later(item, item.progress)
            elif item.kind == "content":
                self._catch_up_later(item, "partial")

    def _write(self, version: int, snapshot: List[Dict[str, Any]]) -> None:
        with self._lock:
            if version <= self._written_version:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._path.with_suffix(".tmp")
            temp_path.write_text(json.dumps({"items": snapshot}, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self._path)
            self._written_version = version


def _later_matches(wanted: str, title: str) -> bool:
    """对方说的名字和画面上的标题是不是同一个：标题对得上，或者去掉标点后一个包含另一个（够长才算）。

    聊天里的她常把一整句记下来（「主人让我有空看B站上《春日影》的现场版」），这时只拿书名号里的名字去比对，
    不然和画面上「【中字4K】「春日影」- MyGO!!!!!」这样前后都带字的标题谁也不包含谁。
    """
    names = re.findall(r"《([^》]+)》", wanted) or [wanted]
    for name in names:
        if titles_match(name, title):
            return True
        a, b = normalize_title(name), normalize_title(title)
        shorter, longer = sorted((a, b), key=len)
        if len(shorter) >= _LATER_MATCH_MIN and shorter in longer:
            return True
    return False


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _expired(item: MemoryItem, now: datetime, seconds: float) -> bool:
    return (now - datetime.fromisoformat(item.updated_at)).total_seconds() >= seconds
