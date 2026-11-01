"""每日额度与游玩历史的持久化。"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import asyncio
import json
import threading

_HISTORY_LIMIT = 30


@dataclass(frozen=True)
class SessionRecord:
    """一次玩手机的记录。"""

    started_at: str
    ended_at: str
    trigger: str
    app: str
    goal: str
    summary: str
    steps: int
    end_reason: str
    # 和对方在这台手机上一起玩的那次；旧记录没有这个字段
    together: bool = False
    # 这次用自己的账号做的互动，例如「评论「这只猫好圆」」；旧记录没有这个字段
    interactions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class UsageStore:
    """保存在插件数据目录里的 JSON：当天的次数、步数和最近的游玩记录。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._date = ""
        self._sessions = 0
        self._steps = 0
        # 当天点赞、评论、关注的次数
        self._interactions: Dict[str, int] = {}
        self._history: List[SessionRecord] = []
        # 加载时发现记录文件损坏、已备份重来的说明，没出问题是空字符串
        self.recovered = ""
        # 每步都会保存。写文件的线程停不下来：保存被取消（叫停时）后它还在写，
        # 所以按序号排先后，线程里加锁一次只写一个，旧的快照晚到也不会盖掉新的
        self._write_lock = threading.Lock()
        self._saved_version = 0
        self._written_version = 0

    @property
    def history(self) -> List[SessionRecord]:
        return list(self._history)

    def _read(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            date, sessions, steps = str(data["date"]), int(data["sessions"]), int(data["steps"])
            interactions = {str(kind): int(count) for kind, count in data.get("interactions", {}).items()}
            history = [SessionRecord(**item) for item in data["history"]]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            # 一个记录文件坏了不该让整个插件加载失败：留个备份，从空记录重新开始
            backup = self._path.with_name(f"{self._path.name}.broken-{datetime.now():%Y%m%d%H%M%S}")
            self._path.replace(backup)
            self.recovered = f"使用记录文件已损坏（{exc}），已备份为 {backup.name}，从空记录重新开始"
            return
        self._date, self._sessions, self._steps = date, sessions, steps
        self._interactions, self._history = interactions, history

    def _snapshot(self) -> Dict[str, Any]:
        return {
            "date": self._date,
            "sessions": self._sessions,
            "steps": self._steps,
            "interactions": dict(self._interactions),
            "history": [record.to_dict() for record in self._history],
        }

    def _write(self, version: int, payload: Dict[str, Any]) -> None:
        with self._write_lock:
            if version <= self._written_version:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self._path)
            self._written_version = version

    async def load(self) -> None:
        await asyncio.to_thread(self._read)

    async def save(self) -> None:
        # 快照在事件循环里取，线程里不碰正在变的数据
        self._saved_version += 1
        await asyncio.to_thread(self._write, self._saved_version, self._snapshot())

    def _roll(self, today: str) -> None:
        if self._date != today:
            self._date = today
            self._sessions = 0
            self._steps = 0
            self._interactions = {}

    def usage(self, today: str) -> Tuple[int, int]:
        """返回今天的（游玩次数，操作步数）。"""
        self._roll(today)
        return self._sessions, self._steps

    def add_session(self, today: str) -> None:
        self._roll(today)
        self._sessions += 1

    def add_step(self, today: str) -> None:
        self._roll(today)
        self._steps += 1

    def interactions(self, today: str) -> Dict[str, int]:
        """返回今天各类互动（like、comment、follow）的次数。"""
        self._roll(today)
        return dict(self._interactions)

    def add_interaction(self, today: str, kind: str) -> None:
        self._roll(today)
        self._interactions[kind] = self._interactions.get(kind, 0) + 1

    def add_record(self, record: SessionRecord) -> None:
        self._history = [*self._history, record][-_HISTORY_LIMIT:]
