from typing import Any, Dict, List, Optional, Tuple

import base64
import httpx
import json
import pytest

from phone_play.devices import AppNotInstalledError, DeviceError, WdaDevice
from phone_play.settings import DeviceSettings

from conftest import make_png

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="哔哩哔哩" x="0" y="0" width="390" height="844">
  <XCUIElementTypeButton type="XCUIElementTypeButton" label="搜索" traits="Button"
      x="340" y="50" width="40" height="40"/>
</XCUIElementTypeApplication>"""
# iOS 18 打开没装的应用时 WDA 返回的错误（真机抓到的，原文是一整行）
NOT_FOUND = (
    'Error Domain=FBSOpenApplicationServiceErrorDomain Code=4 "The request to open \\"com.foo.bar\\" failed." '
    "UserInfo={BSErrorCodeDescription=InvalidRequest, NSLocalizedDescription=The request to open "
    '\\"com.foo.bar\\" failed., NSUnderlyingError=0x1059e2520 {Error Domain=FBSOpenApplicationErrorDomain '
    'Code=4 \\"Application info provider (FBSApplicationLibrary) returned nil for \\"com.foo.bar\\"\\" '
    "UserInfo={BSErrorCodeDescription=NotFound}}}"
)


class FakeWda:
    """模拟 WebDriverAgent 的 HTTP 接口（iPhone 14：390x844 点，3 倍屏），记录收到的请求。"""

    def __init__(self) -> None:
        self.requests: List[Tuple[str, str, Any]] = []
        self.queries: List[str] = []
        self.sessions = 0
        self.session_id = ""
        # 依次返回的锁屏状态，最后一个一直沿用
        self.locked = [False]
        self.screenshot = JPEG
        self.active = "com.apple.springboard"
        self.source = SOURCE
        # 路由 -> (错误码, 错误信息)，只生效一次
        self.errors: Dict[str, Tuple[str, str]] = {}
        # 获得焦点的输入框里的文字；None 表示键盘没弹出来，没有获得焦点的元素
        self.focused: Optional[str] = None
        self.down = False

    def _reply(self, value: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json={"value": value, "sessionId": self.session_id})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        self.queries.append(str(request.url.query, "ascii"))
        if path == "/status":
            return self._reply({"ready": True})
        if path == "/session":
            self.sessions += 1
            self.session_id = f"s{self.sessions}"
            return self._reply({"sessionId": self.session_id, "capabilities": {}})
        route = path
        if path.startswith("/session/"):
            _, _, session_id, rest = path.split("/", 3)
            if session_id != self.session_id:
                return self._reply({"error": "invalid session id", "message": "Session does not exist"}, 404)
            route = "/" + rest
        if route in self.errors:
            code, message = self.errors.pop(route)
            return self._reply({"error": code, "message": message, "traceback": "at FBRoute"}, 500)
        if route == "/appium/settings":
            return self._reply(body["settings"])
        if route == "/wda/screen":
            return self._reply({"scale": 3, "screenSize": {"width": 390, "height": 844}})
        if route == "/wda/locked":
            return self._reply(self.locked.pop(0) if len(self.locked) > 1 else self.locked[0])
        if route == "/screenshot":
            return self._reply(base64.b64encode(self.screenshot).decode("ascii"))
        if route == "/wda/activeAppInfo":
            return self._reply({"bundleId": self.active, "name": "", "pid": 1})
        if route == "/source":
            return self._reply(self.source)
        if route == "/element/active":
            if self.focused is None:
                return self._reply({"error": "no such element", "message": "unable to find an element"}, 404)
            return self._reply({"ELEMENT": "e1", "element-6066-11e4-a52e-4f735466cecf": "e1"})
        if route == "/element/e1/attribute/value":
            return self._reply(self.focused or None)
        return self._reply(None)

    def bodies(self, route: str) -> List[Any]:
        return [body for _, path, body in self.requests if path.endswith(route)]

    def paths(self) -> List[str]:
        return [path for _, path, _ in self.requests]


def make_device(fake: FakeWda) -> WdaDevice:
    return WdaDevice(DeviceSettings(backend="ios_wda"), transport=httpx.MockTransport(fake))


async def test_first_use_creates_session_and_taps_in_points():
    fake = FakeWda()
    device = make_device(fake)
    await device.tap(300, 900)
    await device.tap(30, 60)
    assert fake.sessions == 1
    assert fake.bodies("/appium/settings") == [
        {"settings": {"screenshotQuality": 1, "waitForIdleTimeout": 0, "animationCoolOffTimeout": 0}}
    ]
    assert fake.bodies("/wda/tap") == [{"x": 100.0, "y": 300.0}, {"x": 10.0, "y": 20.0}]
    assert fake.paths()[-1] == "/session/s1/wda/tap"
    assert device.description == "iPhone · http://127.0.0.1:8100"


async def test_long_press_and_swipe_convert_to_points_and_speed():
    fake = FakeWda()
    device = make_device(fake)
    await device.long_press(30, 60, 800)
    await device.swipe(600, 1800, 600, 600, 400)
    assert fake.bodies("/wda/touchAndHold") == [{"x": 10.0, "y": 20.0, "duration": 0.8}]
    drag = fake.bodies("/wda/pressAndDragWithVelocity")[0]
    assert (drag["fromX"], drag["fromY"], drag["toX"], drag["toY"]) == (200.0, 600.0, 200.0, 200.0)
    # 400 点用 0.4 秒划完
    assert drag["velocity"] == pytest.approx(1000)


async def test_back_is_edge_swipe_and_home_enter_use_wda():
    fake = FakeWda()
    device = make_device(fake)
    await device.press_key("back")
    drag = fake.bodies("/wda/pressAndDragWithVelocity")[0]
    assert drag["fromX"] < 5
    assert drag["toX"] == pytest.approx(292.5)
    assert drag["fromY"] == drag["toY"] == pytest.approx(422)
    await device.press_key("home")
    assert fake.paths()[-1] == "/wda/homescreen"
    await device.press_key("enter")
    assert fake.bodies("/wda/keys") == [{"value": ["\n"]}]


async def test_input_text_supports_chinese_and_explains_failure():
    fake = FakeWda()
    device = make_device(fake)
    await device.input_text("猫猫")
    assert fake.bodies("/wda/keys") == [{"value": ["猫猫"]}]
    fake.errors["/wda/keys"] = ("invalid element state", "Keyboard is not present\nFBTypeText failed")
    with pytest.raises(DeviceError, match="可能还没点开输入框") as raised:
        await device.input_text("猫猫")
    assert "Keyboard is not present" in str(raised.value)
    assert "FBTypeText" not in str(raised.value)


async def test_screenshot_returns_image_and_resets_session_on_wrong_format():
    fake = FakeWda()
    device = make_device(fake)
    assert await device.screenshot() == JPEG
    fake.screenshot = b"\x00\x00\x00\x18ftypheic"
    with pytest.raises(DeviceError, match="截图格式不对"):
        await device.screenshot()
    fake.screenshot = make_png(39, 84)
    assert await device.screenshot() == make_png(39, 84)
    # 重新建了会话，重新设置了截图方式
    assert fake.sessions == 2
    assert len(fake.bodies("/appium/settings")) == 2


async def test_expired_session_is_recreated_once():
    fake = FakeWda()
    device = make_device(fake)
    await device.tap(3, 3)
    fake.session_id = "restarted"
    await device.tap(6, 6)
    assert fake.sessions == 2
    assert fake.paths()[-1] == "/session/s2/wda/tap"


async def test_closed_device_reconnects_on_next_use():
    fake = FakeWda()
    device = make_device(fake)
    await device.tap(3, 3)
    await device.close()
    await device.close()
    await device.tap(6, 6)
    assert fake.bodies("/wda/tap") == [{"x": 1.0, "y": 1.0}, {"x": 2.0, "y": 2.0}]


async def test_unreachable_wda_is_explained():
    fake = FakeWda()
    fake.down = True
    with pytest.raises(DeviceError, match="连不上 iPhone 上的 WebDriverAgent"):
        await make_device(fake).ensure_ready()


async def test_locked_phone_is_unlocked_or_reported():
    fake = FakeWda()
    fake.locked = [True, False]
    await make_device(fake).ensure_ready()
    assert fake.paths().count("/wda/unlock") == 1

    fake = FakeWda()
    fake.locked = [True]
    with pytest.raises(DeviceError, match="自动解锁没成功"):
        await make_device(fake).ensure_ready()


async def test_apps_open_close_and_missing_ones_are_remembered():
    fake = FakeWda()
    device = make_device(fake)
    packages = ["tv.danmaku.bilianime", "com.foo.bar", "com.xingin.discover"]
    # WDA 分不出装没装，先全当装了，也不发请求
    assert await device.installed_packages(packages) == set(packages)
    assert fake.requests == []
    assert await device.home_packages() == {"com.apple.springboard"}
    fake.active = "tv.danmaku.bilianime"
    assert await device.foreground_package() == "tv.danmaku.bilianime"

    await device.launch_app("tv.danmaku.bilianime")
    await device.stop_app("tv.danmaku.bilianime")
    assert fake.bodies("/wda/apps/activate") == [{"bundleId": "tv.danmaku.bilianime"}]
    assert fake.bodies("/wda/apps/terminate") == [{"bundleId": "tv.danmaku.bilianime"}]
    fake.errors["/wda/apps/activate"] = ("invalid argument", NOT_FOUND)
    with pytest.raises(AppNotInstalledError, match="手机上没有安装 com.foo.bar"):
        await device.launch_app("com.foo.bar")
    assert await device.installed_packages(packages) == {"tv.danmaku.bilianime", "com.xingin.discover"}
    # 其它原因打不开时照常报错，不当成没装
    fake.errors["/wda/apps/activate"] = ("unknown error", "Timed out while launching the application")
    with pytest.raises(DeviceError, match="打开 com.xingin.discover 失败") as raised:
        await device.launch_app("com.xingin.discover")
    assert not isinstance(raised.value, AppNotInstalledError)
    assert await device.installed_packages(packages) == {"tv.danmaku.bilianime", "com.xingin.discover"}


async def test_switching_from_another_app_goes_home_first():
    # 直接从抖音切到 B 站，iOS 会在左上角显示「◀ 抖音」，先回桌面再打开就没有
    fake = FakeWda()
    fake.active = "com.ss.iphone.ugc.Aweme"
    device = make_device(fake)
    await device.launch_app("com.bilibili.inter")
    paths = fake.paths()
    assert paths.index("/wda/homescreen") < paths.index(f"/session/{fake.session_id}/wda/apps/activate")

    # 已经在桌面或就在这个应用里时不用多回一次桌面
    for active in ("com.apple.springboard", "com.bilibili.inter"):
        fake = FakeWda()
        fake.active = active
        await make_device(fake).launch_app("com.bilibili.inter")
        assert "/wda/homescreen" not in fake.paths()


async def test_focused_input_text_tells_whether_text_went_in():
    fake = FakeWda()
    device = make_device(fake)
    # 键盘没弹出来（比如开着应用自己的表情面板）
    assert await device.focused_input_text() == ""
    fake.focused = ""
    assert await device.focused_input_text() == ""
    fake.focused = "这只橘猫也太圆了"
    assert await device.focused_input_text() == "这只橘猫也太圆了"


async def test_ui_elements_are_in_screenshot_pixels():
    fake = FakeWda()
    elements = await make_device(fake).ui_elements(1170, 2532, 10)
    assert [(element.label, element.bounds) for element in elements] == [("搜索", (1020, 150, 1140, 270))]
    # 不让 WDA 算可见性，否则长列表要等好几秒
    assert fake.queries[-1] == "format=xml&excluded_attributes=visible"


def test_wda_url_must_be_http():
    with pytest.raises(ValueError, match="wda_url"):
        DeviceSettings(wda_url="127.0.0.1:8100")
