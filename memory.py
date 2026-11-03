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
import threading
import unicodedata
import uuid

from .actions import SEEN_MEDIA, VIDEO_KINDS, Seen

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

    kind：content 看过的小说、歌，watched 看过的视频，interest 对方说过的兴趣或要求，unfinished 上次没做完的事。
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
    # 看过的视频：short、long 或空（没说），以及大概看了多少秒
    form: str = ""
    seconds: int = 0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["source_label"] = _SOURCE_LABELS.get(self.source, self.source)
        return data

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
            return f"{where}看过{VIDEO_KINDS.get(self.form, '视频')}《{self.title}》"
        if self.kind == "unfinished":
            where = f"在{self.app}里" if self.app else ""
            return (
                f"上次没做完：{where}{self.title}（{self.detail}）"
                if self.detail
                else f"上次没做完：{where}{self.title}"
            )
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
        self._trim(now)
        return item

    def remember_watched(self, seen: Seen, app: str, form: str, now: datetime) -> MemoryItem:
        """记下看过的视频；同一个应用里标题对得上的算同一个，只更新时间、集数和长短。"""
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
            )
            self._items.append(item)
        item.detail = seen.detail or item.detail
        item.form = form or item.form
        item.updated_at = stamp
        self._trim(now)
        return item

    def add_watched_seconds(self, item_id: str, seconds: float) -> None:
        for item in self._items:
            if item.id == item_id:
                item.seconds += round(seconds)

    def find_watched(self, app: str, text: str, now: datetime) -> Optional[MemoryItem]:
        """这个应用里看过的、标题和 ``text`` 对得上的视频（太久以前的不算）。"""
        return next(
            (item for item in self._watched(app, now) if titles_match(item.title, text)),
            None,
        )

    def watched_titles(self, app: str, now: datetime, limit: int) -> List[str]:
        """这个应用里最近看过的视频标题，最近的在前。"""
        return [item.title for item in self._watched(app, now)[:limit]]

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
        """给规划看的几条：上次没做完的（没过期才提）放最前，再是最近看过的和对方说过的，最后一行是最近看过的视频。"""
        lines: List[str] = []
        watched: List[str] = []
        for item in self.items():
            if item.kind == "unfinished":
                if _expired(item, now, _UNFINISHED_SECONDS):
                    continue
                lines.insert(0, item.describe())
            elif item.kind == "watched":
                if not _expired(item, now, _WATCHED_SECONDS):
                    watched.append(f"《{item.title}》")
            else:
                lines.append(item.describe())
        lines = lines[: _PROMPT_ITEMS - 1 if watched else _PROMPT_ITEMS]
        if watched:
            lines.append(f"最近看过的视频（不用再专门找来看）：{'、'.join(watched[:_PROMPT_WATCHED])}")
        return lines

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

    def _write(self, version: int, snapshot: List[Dict[str, Any]]) -> None:
        with self._lock:
            if version <= self._written_version:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._path.with_suffix(".tmp")
            temp_path.write_text(json.dumps({"items": snapshot}, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self._path)
            self._written_version = version


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _expired(item: MemoryItem, now: datetime, seconds: float) -> bool:
    return (now - datetime.fromisoformat(item.updated_at)).total_seconds() >= seconds
