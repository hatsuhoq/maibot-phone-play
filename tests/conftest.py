from io import BytesIO
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from PIL import Image, ImageDraw

import sys

# 让测试可以用 phone_play.xxx 的方式导入插件模块（与宿主加载插件包的方式一致）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from phone_play.devices import (  # noqa: E402
    AppNotInstalledError,
    DeviceError,
    DeviceNeedsUserError,
    KeyName,
    PhoneDevice,
)
from phone_play.ui_tree import UIElement  # noqa: E402


def make_png(width: int = 540, height: int = 1200, color: str = "#336699") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def scene_png(*boxes: Tuple[Tuple[int, int, int, int], str]) -> bytes:
    """在 make_png 的底色上画几个色块，当作按钮、弹窗、视频画面。"""
    image = Image.new("RGB", (540, 1200), "#336699")
    draw = ImageDraw.Draw(image)
    for bounds, color in boxes:
        draw.rectangle(bounds, fill=color)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


class FakeDevice(PhoneDevice):
    """记录所有操作的假手机；能锁屏（像 iPhone），测安卓时把 supports_lock 设成 False。"""

    supports_lock = True

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
        # 锁着屏时下一次 unlock_if_locked 解开；unlock_fails 为真时模拟设了锁屏密码、解不开
        self.locked = False
        self.unlock_fails = False

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

    async def unlock_if_locked(self) -> bool:
        if not self.locked:
            return False
        if self.unlock_fails:
            raise DeviceNeedsUserError("测试手机锁着屏，自动解锁没成功", "needs_unlock")
        self.locked = False
        self.calls.append(("unlock",))
        return True

    async def lock(self) -> bool:
        self.calls.append(("lock",))
        self.locked = True
        return True

    async def close(self) -> None:
        self.calls.append(("close",))

    def actions(self) -> List[Tuple]:
        """去掉截图和连接记录，只看真正改变手机状态的操作。"""
        return [call for call in self.calls if call[0] not in ("screenshot", "ready", "close")]


def _elements(rows: Sequence[Tuple[str, str, bool, Tuple[int, int, int, int]]]) -> List[UIElement]:
    return [
        UIElement(index=index, label=label, kind=kind, clickable=clickable, bounds=bounds)
        for index, (label, kind, clickable, bounds) in enumerate(rows, start=1)
    ]


# 下面三组是 2026 年 11 月在 iPhone 14 上的 B 站读到的界面元素（截取一部分），屏幕 1170x2532
BILI_SCREEN = (1170, 2532)
# 搜索「春日影」的结果：卡片文字就是标题，看过的卡片上有 B 站自己标的「最近看过」
BILI_SEARCH = _elements(
    [
        ("搜索", "Button", True, (978, 153, 1170, 261)),
        ("春日影", "TextField", True, (114, 156, 978, 258)),
        ("【中字4K·HiRes】「春日影」- MyGO!!!!!｜M", "Cell", True, (0, 393, 1170, 768)),
        ("最近看过", "StaticText", False, (645, 570, 765, 612)),
        ("共2集 5:13", "StaticText", False, (390, 678, 534, 720)),
        ("邦多利历史上最出圈的歌曲，MyGO版本的神曲春日影初登场的l", "Cell", True, (0, 768, 1170, 1131)),
        ("5:02", "StaticText", False, (471, 1041, 534, 1083)),
    ]
)
# 从搜索结果点开的视频详情页：上面播放器，下面「简介」「评论」，标题在播放、弹幕数那一行上面
BILI_DETAIL = _elements(
    [
        ("进度", "ProgressIndicator", False, (0, 762, 1170, 831)),
        ("广告, 限时1元抢iQOO13", "Other", True, (0, 798, 1170, 1011)),
        ("简介", "Button", True, (24, 1008, 252, 1131)),
        ("评论", "Button", True, (252, 1008, 549, 1131)),
        ("发布弹幕，输入框，轻点两下编辑", "Button", True, (720, 1023, 1014, 1116)),
        ("up主koiのkoi，1.4万粉丝167视频", "Cell", True, (0, 1128, 1170, 1293)),
        ("【中字4K·HiRes】「春日影」- MyGO!!!!!｜M", "Cell", True, (0, 1290, 1170, 1395)),
        ("16.4万播放,132条弹幕,发布时间2025年11月21日", "Cell", True, (0, 1392, 1170, 1479)),
        ("点赞", "Button", True, (36, 1500, 258, 1623)),
        ("已选定,「春日影」,共2集", "Cell", True, (0, 1650, 1170, 1881)),
        ("【中字4K·HiRes】「迷星叫」- MyGO!!!!!｜D", "Cell", True, (0, 2334, 1170, 2532)),
    ]
)
# 从首页推荐点开的竖屏播放器：整屏一格（文字是搜索推荐），UP 主、关注、标题几个元素的文字是占位名
BILI_PLAYER = _elements(
    [
        ("当我不停的欺负小猫时", "Cell", True, (0, 0, 1170, 2532)),
        ("返回", "Button", True, (0, 141, 132, 273)),
        ("1人正在看", "StaticText", False, (207, 180, 366, 234)),
        ("name", "Button", True, (174, 1929, 702, 1992)),
        ("follow", "Button", True, (726, 1947, 909, 2019)),
        ("fans", "Button", True, (174, 1992, 339, 2037)),
        ("title", "Other", True, (36, 2067, 972, 2133)),
    ]
)
# MuMu 安卓上的 B 站，首页点开「竖版视频」进的竖屏播放器：没有整屏那一格和占位名，有「详情页」按钮，标题读得到
BILI_ANDROID_SCREEN = (1440, 2560)
BILI_PLAYER_ANDROID = _elements(
    [
        ("返回", "ImageView", True, (1, 97, 185, 273)),
        ("51人正在看", "TextView", False, (202, 156, 521, 213)),
        ("点赞", "Button", True, (1215, 919, 1439, 1151)),
        ("评论", "ViewGroup", True, (1215, 1191, 1439, 1423)),
        ("-比奇堡的比奇", "TextView", True, (246, 1725, 629, 1809)),
        ("关注", "View", True, (661, 1749, 908, 1845)),
        ("离谱爆料之 爱音 将要加入sumimi？ ‎2.2万播放", "TextView", True, (49, 1935, 1214, 2094)),
        ("详情页", "ImageView", True, (1023, 2383, 1215, 2560)),
        ("全屏", "ImageView", True, (1247, 2383, 1439, 2560)),
        ("发弹幕", "TextView", True, (49, 2411, 756, 2539)),
    ]
)
