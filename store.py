"""每日额度与游玩历史的持久化。"""

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import asyncio
import json
import threading

_HISTORY_LIMIT = 30
# 每天的汇总留这么多天：周报看最近 7 天，游玩记录只留最近 30 次、一周都不够
_DAILY_DAYS = 35


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
    # app 是起始应用；apps 是这次实际去过的应用和大致停留秒数 [{"name", "package", "seconds"}]，
    # last_app 是结束时在用的。旧记录没有这两个字段，按只用了起始应用处理
    apps: List[Dict[str, Any]] = field(default_factory=list)
    last_app: str = ""
    # 这次的模型调用次数、token（宿主给出的才算）和各环节耗时，见 agent 的 SessionStats；旧记录没有
    stats: Dict[str, Any] = field(default_factory=dict)
    # 用的哪台设备（多设备时的名字）；旧记录没有
    device: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionRecord":
        """读旧版本、新版本写的记录：缺的字段用默认值，不认识的字段忽略，不当成文件损坏。"""
        names = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})

    def app_seconds(self) -> Dict[str, float]:
        """这次在各个应用里待了多少秒；旧记录没有 apps，整次都算在起始应用上。"""
        if not self.apps:
            seconds = (datetime.fromisoformat(self.ended_at) - datetime.fromisoformat(self.started_at)).total_seconds()
            return {self.app: max(0.0, seconds)}
        spent: Dict[str, float] = {}
        for item in self.apps:
            spent[item["name"]] = spent.get(item["name"], 0.0) + float(item["seconds"])
        return spent

    def places_text(self) -> str:
        """这次在哪些应用里玩的，例如「欢乐斗地主（约 3 分钟）、哔哩哔哩（约 1 分钟）」；只去了一个就只写名字。"""
        if len(self.apps) <= 1:
            return self.app
        return "、".join(f"{item['name']}（约 {max(1, round(item['seconds'] / 60))} 分钟）" for item in self.apps)


class UsageStore:
    """保存在插件数据目录里的 JSON：当天的次数、步数和最近的游玩记录。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._date = ""
        self._sessions = 0
        self._steps = 0
        # 当天点赞、评论、关注的次数
        self._interactions: Dict[str, int] = {}
        # 当天调用模型的次数（失败的也算）
        self._model_calls = 0
        self._history: List[SessionRecord] = []
        # 每天的汇总（周报用）：日期 → {sessions 次数, seconds 秒数, apps 各应用秒数, triggers 各来源次数}
        self._daily: Dict[str, Dict[str, Any]] = {}
        # 现在用哪台设备（多设备时的名字，空字符串是「设备」那一台），重启后还记得
        self.current_device = ""
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

    @property
    def daily(self) -> Dict[str, Dict[str, Any]]:
        """每天的汇总，见 _add_daily；返回副本。"""
        return json.loads(json.dumps(self._daily))

    def _read(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            date, sessions, steps = str(data["date"]), int(data["sessions"]), int(data["steps"])
            interactions = {str(kind): int(count) for kind, count in data.get("interactions", {}).items()}
            model_calls = int(data.get("model_calls", 0))
            history = [SessionRecord.from_dict(item) for item in data["history"]]
            current_device = str(data.get("current_device", ""))
            daily = {
                str(day): {
                    "sessions": int(entry["sessions"]),
                    "seconds": float(entry["seconds"]),
                    "apps": {str(name): float(seconds) for name, seconds in entry["apps"].items()},
                    "triggers": {str(label): int(count) for label, count in entry["triggers"].items()},
                }
                for day, entry in data.get("daily", {}).items()
            }
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            # 一个记录文件坏了不该让整个插件加载失败：留个备份，从空记录重新开始
            backup = self._path.with_name(f"{self._path.name}.broken-{datetime.now():%Y%m%d%H%M%S}")
            self._path.replace(backup)
            self.recovered = f"使用记录文件已损坏（{exc}），已备份为 {backup.name}，从空记录重新开始"
            return
        self._date, self._sessions, self._steps = date, sessions, steps
        self._interactions, self._history = interactions, history
        self._model_calls = model_calls
        self.current_device = current_device
        self._daily = daily
        if "daily" not in data:
            # 0.3.0 及以前没有每天的汇总：用还留着的游玩记录补上，第一次看周报不至于是空的
            for record in history:
                self._add_daily(record)

    def _snapshot(self) -> Dict[str, Any]:
        return {
            "date": self._date,
            "sessions": self._sessions,
            "steps": self._steps,
            "interactions": dict(self._interactions),
            "model_calls": self._model_calls,
            "history": [record.to_dict() for record in self._history],
            "current_device": self.current_device,
            "daily": self.daily,
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
            self._model_calls = 0

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

    def model_calls(self, today: str) -> int:
        self._roll(today)
        return self._model_calls

    def add_model_call(self, today: str) -> None:
        self._roll(today)
        self._model_calls += 1

    def add_record(self, record: SessionRecord) -> None:
        self._history = [*self._history, record][-_HISTORY_LIMIT:]
        self._add_daily(record)

    def _add_daily(self, record: SessionRecord) -> None:
        """把一次记录算进它开始那天的汇总，只留最近 _DAILY_DAYS 天。"""
        entry = self._daily.setdefault(
            record.started_at[:10], {"sessions": 0, "seconds": 0.0, "apps": {}, "triggers": {}}
        )
        entry["sessions"] += 1
        for name, seconds in record.app_seconds().items():
            entry["seconds"] += seconds
            entry["apps"][name] = entry["apps"].get(name, 0.0) + seconds
        entry["triggers"][record.trigger] = entry["triggers"].get(record.trigger, 0) + 1
        for day in sorted(self._daily)[:-_DAILY_DAYS]:
            del self._daily[day]
