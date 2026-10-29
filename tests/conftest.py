from io import BytesIO
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from PIL import Image

import sys

# 让测试可以用 phone_play.xxx 的方式导入插件模块（与宿主加载插件包的方式一致）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from phone_play.devices import AppNotInstalledError, DeviceError, KeyName, PhoneDevice  # noqa: E402
from phone_play.ui_tree import UIElement  # noqa: E402


def make_png(width: int = 540, height: int = 1200, color: str = "#336699") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


class FakeDevice(PhoneDevice):
    """记录所有操作的假手机。"""

    def __init__(
        self,
        *,
        installed: Optional[Set[str]] = None,
        foregrounds: Optional[Iterable[str]] = None,
        elements: Optional[Sequence[UIElement]] = None,
        home: Optional[Set[str]] = None,
        ready_error: str = "",
        missing: Optional[Set[str]] = None,
        input_focus: Optional[bool] = None,
    ) -> None:
        self.calls: List[Tuple] = []
        # 非空时模拟连不上手机
        self.ready_error = ready_error
        self.installed = installed if installed is not None else {"tv.danmaku.bili", "com.xingin.xhs"}
        # 模拟 iPhone：查的时候说装了，打开时才发现没装
        self.missing = missing or set()
        # 模拟能不能查焦点输入框：None=查不了；True=写的字都进了输入框；False=没点开输入框，字没写进去
        self.input_focus = input_focus
        self.typed = ""
        self._foregrounds = list(foregrounds or [])
        self.default_foreground = "tv.danmaku.bili"
        self.elements = list(elements or [])
        self.home = home if home is not None else {"com.android.launcher3"}
        self.png = make_png()

    @property
    def description(self) -> str:
        return "测试手机"

    async def ensure_ready(self) -> None:
        self.calls.append(("ready",))
        if self.ready_error:
            raise DeviceError(self.ready_error)

    async def screenshot(self) -> bytes:
        self.calls.append(("screenshot",))
        return self.png

    async def tap(self, x: int, y: int) -> None:
        self.calls.append(("tap", x, y))

    async def long_press(self, x: int, y: int, duration_ms: int) -> None:
        self.calls.append(("long_press", x, y))

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self.calls.append(("swipe", x1, y1, x2, y2))

    async def input_text(self, text: str) -> None:
        self.calls.append(("text", text))
        self.typed = text

    async def press_key(self, key: KeyName) -> None:
        self.calls.append(("key", key))

    async def launch_app(self, package: str) -> None:
        self.calls.append(("launch", package))
        if package in self.missing:
            raise AppNotInstalledError(package)

    async def stop_app(self, package: str) -> None:
        self.calls.append(("stop", package))

    async def foreground_package(self) -> str:
        return self._foregrounds.pop(0) if self._foregrounds else self.default_foreground

    async def home_packages(self) -> Set[str]:
        return set(self.home)

    async def installed_packages(self, packages: Sequence[str]) -> Set[str]:
        return set(self.installed) & set(packages)

    async def ui_elements(self, width: int, height: int, limit: int) -> List[UIElement]:
        return self.elements[:limit]

    async def focused_input_text(self) -> Optional[str]:
        if self.input_focus is None:
            return None
        return self.typed if self.input_focus else ""

    async def close(self) -> None:
        self.calls.append(("close",))

    def actions(self) -> List[Tuple]:
        """去掉截图和连接记录，只看真正改变手机状态的操作。"""
        return [call for call in self.calls if call[0] not in ("screenshot", "ready", "close")]
