"""失败步骤的诊断记录：当时的画面（缩小，并标出要点的位置或滑动方向）、计划动作、执行结果和失败原因。

只留最近 ``max_items`` 条、最多 ``max_age_seconds``，存在插件数据目录里，WebUI 里能看能清理。
不存完整的模型请求和原始推理。
"""

from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import json
import threading
import uuid

from .imaging import encode_jpeg

# 要点的位置画一个圈；滑动画起点到终点的线
Mark = Tuple[int, ...]

_IMAGE_EDGE = 480
_IMAGE_QUALITY = 60
_MARK_COLOR = "#ff2d55"
_INDEX_NAME = "index.json"


class DiagnosticStore:
    """诊断记录。所有文件读写都在线程里做，并用锁串行，避免同时写坏索引。"""

    def __init__(self, folder: Path, max_items: int = 30, max_age_seconds: float = 3 * 86400) -> None:
        self._folder = folder
        self._max_items = max_items
        self._max_age_seconds = max_age_seconds
        self._lock = threading.Lock()

    async def add(self, image: Image.Image, mark: Optional[Mark], info: Dict[str, Any]) -> str:
        """存一条诊断，返回它的编号。``info`` 里是给人看的文字：步骤、应用、动作、目标、结果、原因。"""
        jpeg = await asyncio.to_thread(_render, image, mark)
        return await asyncio.to_thread(self._add, jpeg, info)

    async def entries(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._entries)

    async def image(self, diagnostic_id: str) -> Optional[bytes]:
        return await asyncio.to_thread(self._image, diagnostic_id)

    async def clear(self) -> int:
        """删掉全部诊断记录，返回删了几条。"""
        return await asyncio.to_thread(self._clear)

    async def prune(self) -> None:
        await asyncio.to_thread(self._prune_locked)

    # ===== 线程里执行的部分 =====

    def _index_path(self) -> Path:
        return self._folder / _INDEX_NAME

    def _read_index(self) -> List[Dict[str, Any]]:
        path = self._index_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            # 索引坏了：诊断只是辅助信息，丢掉重来，图片文件下次整理时清掉
            return []
        return [item for item in data if isinstance(item, dict) and "id" in item] if isinstance(data, list) else []

    def _write_index(self, entries: List[Dict[str, Any]]) -> None:
        self._folder.mkdir(parents=True, exist_ok=True)
        temp_path = self._index_path().with_suffix(".tmp")
        temp_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self._index_path())

    def _add(self, jpeg: bytes, info: Dict[str, Any]) -> str:
        diagnostic_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._folder.mkdir(parents=True, exist_ok=True)
            (self._folder / f"{diagnostic_id}.jpg").write_bytes(jpeg)
            entries = self._read_index()
            entries.append({"id": diagnostic_id, "created_at": datetime.now().isoformat(timespec="seconds"), **info})
            self._write_index(self._prune(entries))
        return diagnostic_id

    def _prune(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """只留最近的、没过期的；多出来的图片文件一并删掉。"""
        now = datetime.now()
        kept = [
            entry
            for entry in entries
            if (now - datetime.fromisoformat(entry["created_at"])).total_seconds() < self._max_age_seconds
        ][-self._max_items :]
        keep_ids = {entry["id"] for entry in kept}
        for path in self._folder.glob("*.jpg"):
            if path.stem not in keep_ids:
                path.unlink(missing_ok=True)
        return kept

    def _prune_locked(self) -> None:
        with self._lock:
            if self._folder.exists():
                self._write_index(self._prune(self._read_index()))

    def _entries(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(reversed(self._read_index()))

    def _image(self, diagnostic_id: str) -> Optional[bytes]:
        # 编号只来自本插件生成的十六进制串；不认识的格式直接当作没有，免得拼出别的路径
        if not diagnostic_id.isalnum():
            return None
        path = self._folder / f"{diagnostic_id}.jpg"
        return path.read_bytes() if path.exists() else None

    def _clear(self) -> int:
        with self._lock:
            entries = self._read_index()
            for path in self._folder.glob("*.jpg"):
                path.unlink(missing_ok=True)
            if self._folder.exists():
                self._write_index([])
            return len(entries)


def _render(image: Image.Image, mark: Optional[Mark]) -> bytes:
    """在截图副本上标出目标，再缩小编码；同步 CPU 计算，由调用方放进线程。"""
    marked = image.copy()
    if mark:
        draw = ImageDraw.Draw(marked)
        width = max(4, min(marked.size) // 120)
        if len(mark) == 2:
            radius = max(18, min(marked.size) // 22)
            x, y = mark
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=_MARK_COLOR, width=width)
        else:
            x1, y1, x2, y2 = mark
            draw.line((x1, y1, x2, y2), fill=_MARK_COLOR, width=width)
            draw.ellipse((x2 - width * 3, y2 - width * 3, x2 + width * 3, y2 + width * 3), fill=_MARK_COLOR)
    return encode_jpeg(marked, _IMAGE_EDGE, _IMAGE_QUALITY)
