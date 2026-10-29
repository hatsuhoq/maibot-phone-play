"""每日额度与游玩历史的持久化。"""

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

import asyncio
import json

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

    @property
    def history(self) -> List[SessionRecord]:
        return list(self._history)

    def _read(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"使用记录文件已损坏：{self._path}（{exc.msg}），删除该文件后重试") from exc
        self._date = str(data["date"])
        self._sessions = int(data["sessions"])
        self._steps = int(data["steps"])
        self._interactions = {str(kind): int(count) for kind, count in data.get("interactions", {}).items()}
        self._history = [SessionRecord(**item) for item in data["history"]]

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": self._date,
            "sessions": self._sessions,
            "steps": self._steps,
            "interactions": self._interactions,
            "history": [record.to_dict() for record in self._history],
        }
        temp_path = self._path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self._path)

    async def load(self) -> None:
        await asyncio.to_thread(self._read)

    async def save(self) -> None:
        await asyncio.to_thread(self._write)

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
