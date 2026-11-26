"""她自己想玩、手机上没装的游戏：先问主人，主人同意了她自己去应用商店装；装好的记下来，和白名单里的应用一样能玩。

存在插件数据目录的 games.json：问过主人的游戏（带状态）和她自己装的游戏（名字、包名、哪台手机）。
不改用户的 config.toml；WebUI 里能看、能移除。插件不卸载应用，要卸载请自己在手机上删。

规矩：同一时间只有一个在问或在装的；一天最多问一次；主人说不装的同一款 30 天内不再问；主人一直没回，过一天就算了。
"""

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncio
import json
import threading
import uuid

from .memory import normalize_title
from .settings import AllowedApp

WISH_ASKING = "asking"
WISH_APPROVED = "approved"
WISH_DECLINED = "declined"
WISH_EXPIRED = "expired"
WISH_INSTALLED = "installed"
WISH_FAILED = "failed"
WISH_LABELS: Dict[str, str] = {
    WISH_ASKING: "在等主人回",
    WISH_APPROVED: "主人同意了，正在装",
    WISH_DECLINED: "主人说不装",
    WISH_EXPIRED: "主人没回，算了",
    WISH_INSTALLED: "装好了",
    WISH_FAILED: "没装上",
}
_WISH_WAIT = timedelta(days=1)
_DECLINED_DAYS = timedelta(days=30)
_WISH_KEEP = 30


class WishRefused(Exception):
    """主人的回答没能办成：没有在等回答的游戏，或者现在去装不了。"""


@dataclass
class GameWish:
    """问过主人能不能装的一款游戏。"""

    id: str
    name: str
    reason: str
    device: str
    asked_at: str
    status: str = WISH_ASKING
    # 去哪个聊天问的（主人的私聊）；主人在别的聊天里回也认，这里只用来在那个聊天里提醒她在等回答
    stream_id: str = ""
    answered_at: str = ""
    # 没装上的原因、装好后的包名
    note: str = ""

    @property
    def label(self) -> str:
        return WISH_LABELS.get(self.status, self.status)


@dataclass
class OwnGame:
    """她自己装的一款游戏。"""

    name: str
    package: str
    device: str
    installed_at: str

    def as_app(self) -> AllowedApp:
        return AllowedApp(
            package=self.package,
            name=self.name,
            note=f"你自己装的游戏《{self.name}》（主人同意后装的）",
            devices=[self.device] if self.device else [],
        )


class GameStore:
    """games.json。写文件放进线程，按序号排先后，旧快照晚到不会盖掉新的。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._wishes: List[GameWish] = []
        self._games: List[OwnGame] = []
        self._lock = threading.Lock()
        self._saved_version = 0
        self._written_version = 0
        # 加载时发现文件损坏、已备份重来的说明
        self.recovered = ""

    def wishes(self) -> List[GameWish]:
        """问过主人的游戏，最近问的在前。"""
        return sorted(self._wishes, key=lambda wish: wish.asked_at, reverse=True)

    def games(self) -> List[OwnGame]:
        """她自己装的游戏，最近装的在前。"""
        return sorted(self._games, key=lambda game: game.installed_at, reverse=True)

    def _expire(self, now: datetime) -> None:
        for wish in self._wishes:
            if wish.status == WISH_ASKING and now - datetime.fromisoformat(wish.asked_at) >= _WISH_WAIT:
                wish.status, wish.answered_at = WISH_EXPIRED, now.isoformat(timespec="seconds")

    def open_wish(self, now: datetime) -> Optional[GameWish]:
        """还在等主人回答的那个；过了一天没回的算了。"""
        self._expire(now)
        return next((wish for wish in self._wishes if wish.status == WISH_ASKING), None)

    def wish(self, wish_id: str) -> Optional[GameWish]:
        return next((wish for wish in self._wishes if wish.id == wish_id), None)

    def can_wish(self, now: datetime) -> bool:
        """现在能不能再问一款：没有在问、在装的，今天也还没问过。"""
        self._expire(now)
        if any(wish.status in (WISH_ASKING, WISH_APPROVED) for wish in self._wishes):
            return False
        return not any(datetime.fromisoformat(wish.asked_at).date() == now.date() for wish in self._wishes)

    def declined_names(self, now: datetime) -> List[str]:
        """30 天内主人说过不装的游戏，规划时告诉她别再问。"""
        return [
            wish.name
            for wish in self._wishes
            if wish.status == WISH_DECLINED and now - datetime.fromisoformat(wish.answered_at) < _DECLINED_DAYS
        ]

    def is_declined(self, name: str, now: datetime) -> bool:
        key = normalize_title(name)
        return any(normalize_title(declined) == key for declined in self.declined_names(now))

    def add_wish(self, name: str, reason: str, device: str, now: datetime) -> GameWish:
        wish = GameWish(
            id=uuid.uuid4().hex[:12],
            name=name.strip()[:40],
            reason=reason.strip()[:80],
            device=device,
            asked_at=now.isoformat(timespec="seconds"),
        )
        self._wishes.append(wish)
        # 只留最近几十条问过的
        self._wishes = sorted(self._wishes, key=lambda item: item.asked_at)[-_WISH_KEEP:]
        return wish

    def answer(self, wish: GameWish, approved: bool, now: datetime) -> None:
        wish.status = WISH_APPROVED if approved else WISH_DECLINED
        wish.answered_at = now.isoformat(timespec="seconds")

    def finish(self, wish: GameWish, package: str, device: str, note: str, now: datetime) -> Optional[OwnGame]:
        """这次下载结束：认出了装好的包名就记进她在 ``device`` 上的游戏，不然记成没装上和原因。

        装在哪台以下载时用的那台为准：问的时候和主人同意时可能已经换了手机。
        """
        if not package:
            wish.status, wish.note = WISH_FAILED, note
            return None
        wish.status, wish.note, wish.device = WISH_INSTALLED, package, device
        return self.add_game(wish.name, package, device, now)

    def add_game(self, name: str, package: str, device: str, now: datetime) -> OwnGame:
        game = next((item for item in self._games if item.package == package and item.device == device), None)
        if game is None:
            game = OwnGame(name=name, package=package, device=device, installed_at=now.isoformat(timespec="seconds"))
            self._games.append(game)
        return game

    def remove_game(self, package: str) -> bool:
        """从她的游戏里去掉（手机上的应用不删）。"""
        kept = [game for game in self._games if game.package != package]
        removed = len(kept) != len(self._games)
        self._games = kept
        return removed

    def apps_for(self, device: str) -> List[AllowedApp]:
        """这台手机上她自己装的游戏，当作白名单里的应用用。"""
        return [game.as_app() for game in self.games() if game.as_app().available_on(device)]

    async def load(self) -> None:
        await asyncio.to_thread(self._read)

    async def save(self) -> None:
        self._saved_version += 1
        snapshot = {"wishes": [asdict(wish) for wish in self._wishes], "games": [asdict(game) for game in self._games]}
        await asyncio.to_thread(self._write, self._saved_version, snapshot)

    def _read(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._wishes = [_build(GameWish, raw) for raw in data.get("wishes", [])]
            self._games = [_build(OwnGame, raw) for raw in data.get("games", [])]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            # 和内容记忆一样：坏了就留个备份，从空的重新开始，不让插件加载失败
            backup = self._path.with_name(f"{self._path.name}.broken-{datetime.now():%Y%m%d%H%M%S}")
            self._path.replace(backup)
            self._wishes, self._games = [], []
            self.recovered = f"游戏记录文件已损坏（{exc}），已备份为 {backup.name}，从空的重新开始"

    def _write(self, version: int, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            if version <= self._written_version:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self._path)
            self._written_version = version


def _build(kind: Any, raw: Dict[str, Any]) -> Any:
    names = {item.name for item in fields(kind)}
    return kind(**{key: value for key, value in raw.items() if key in names})
