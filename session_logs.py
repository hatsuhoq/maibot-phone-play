"""每局玩手机的操作记录：边玩边写进插件数据目录，插件重载、MaiBot 重启后也翻得到。

内存里只留最近 60 条给 WebUI 看，一局玩久了前面的就被挤掉，插件一重载就全没了：实测想回头查横屏那两局
有没有遇到转屏，记录已经没了。这里一局一个 JSON Lines 文件（按开始时间命名）：开局写一行这局的信息，
每条操作记录追加一行，结束再写一行结果；只留最近 ``max_sessions`` 局。
存的就是 WebUI「操作记录」里那些文字，不存截图，也不存模型的完整请求。
"""

from collections import deque
from datetime import datetime
from logging import Logger
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import asyncio
import json

_SUFFIX = ".jsonl"


class SessionLogStore:
    """操作记录文件。写文件都在线程里做；排着的行由一个后台任务按调用顺序写出，不会写乱。"""

    def __init__(self, folder: Path, logger: Logger, max_sessions: int = 30) -> None:
        self._folder = folder
        self._logger = logger
        self._max_sessions = max_sessions
        self._pending: Deque[Tuple[str, Dict[str, Any]]] = deque()
        self._writer: Optional[asyncio.Task[None]] = None

    @property
    def folder(self) -> Path:
        return self._folder

    def begin(self, started_at: datetime, info: Dict[str, Any]) -> str:
        """开一局，返回这局记录的名字；``info`` 是一开局就知道的（怎么开始的、哪台手机、想做什么）。"""
        name = started_at.strftime("%Y%m%d-%H%M%S")
        self.add(name, {"type": "session", "started_at": started_at.isoformat(timespec="seconds"), **info})
        return name

    def add(self, name: str, line: Dict[str, Any]) -> None:
        """给这局追加一行；不等写完，按调用顺序在后台写出。"""
        self._pending.append((name, line))
        if self._writer is None or self._writer.done():
            self._writer = asyncio.get_running_loop().create_task(self._drain(), name="phone_play_session_log")

    async def flush(self) -> None:
        """等排着的行都写完；插件卸载前调用，不然最后几行（这局怎么结束的）会丢。"""
        if self._writer is not None:
            await self._writer

    async def _drain(self) -> None:
        while self._pending:
            batch = list(self._pending)
            self._pending.clear()
            try:
                await asyncio.to_thread(self._write, batch)
            except OSError as exc:
                # 操作记录只是方便事后查，写不进去不影响玩；照实记下原因，这一批丢掉
                self._logger.warning(f"操作记录写不进 {self._folder}：{exc}")

    def _write(self, batch: List[Tuple[str, Dict[str, Any]]]) -> None:
        self._folder.mkdir(parents=True, exist_ok=True)
        started = False
        for name, line in batch:
            started = started or line.get("type") == "session"
            with (self._folder / f"{name}{_SUFFIX}").open("a", encoding="utf-8") as file:
                file.write(json.dumps(line, ensure_ascii=False) + "\n")
        if started:
            for path in sorted(self._folder.glob(f"*{_SUFFIX}"))[: -self._max_sessions]:
                path.unlink(missing_ok=True)
