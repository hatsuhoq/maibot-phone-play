"""手机设备层：统一的操作接口，以及安卓（ADB）和 iPhone（WebDriverAgent）两种实现。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Sequence, Set, Tuple

import asyncio
import base64
import binascii
import httpx
import math
import re
import shlex
import subprocess
import sys

from .imaging import is_dark_screen
from .mdns import query_adb_port
from .settings import DeviceSettings
from .ui_tree import UIElement, parse_uiautomator_xml, parse_wda_xml

KeyName = Literal["back", "home", "enter"]
ProcessRunner = Callable[[Sequence[str], float], Awaitable[Tuple[int, bytes, bytes]]]

_KEYCODES: Dict[str, int] = {"back": 4, "home": 3, "enter": 66}
# 只点亮、只关掉屏幕的按键（安卓 4.4W 起有），不像电源键那样按一下切换一次
_KEYCODE_SLEEP = 223
_KEYCODE_WAKEUP = 224
# 安卓的锁屏状态：屏幕亮没亮（dumpsys power），锁屏界面在不在（dumpsys window policy 里 KeyguardServiceDelegate 那一段）
_LOCK_STATE_COMMAND = "dumpsys power | grep 'mWakefulness='; dumpsys window policy | grep -E '^ *showing='; true"
_WAKEFULNESS_PATTERN = re.compile(r"mWakefulness=(\w+)")
_KEYGUARD_PATTERN = re.compile(r"^\s*showing=(true|false)\s*$", re.MULTILINE)
# 用本机地址连的是模拟器（MuMu、雷电、夜神是 127.0.0.1:端口，Android Studio 的是 emulator-5554），不锁屏
_EMULATOR_SERIAL = re.compile(r"^(emulator-\d+|(127\.0\.0\.1|localhost):\d+)$")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
# 要 adb connect 的序列号：主机:端口，或者只有 IP（无线调试的端口用 mDNS 找）
_HOST_PORT_SERIAL = re.compile(r"^([^\s:]+):(\d+)$")
_BARE_IP_SERIAL = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
# 关屏、划掉锁屏界面都有动画，按完隔一会儿再看成没成
_ADB_LOCK_CHECKS = 4
_ADB_LOCK_CHECK_SECONDS = 0.5
_PACKAGE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_FOCUS_PATTERNS = (
    re.compile(r"mCurrentFocus=Window\{[^}]*?\s([A-Za-z0-9_.]+)/"),
    re.compile(r"mFocusedApp=[^{]*\{[^}]*?\s([A-Za-z0-9_.]+)/"),
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_UI_DUMP_PATH = "/sdcard/phone_play_ui.xml"
_ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
_STATE_HINTS: Dict[str, str] = {
    "unauthorized": "未授权：请在手机上点「允许 USB 调试」",
    "offline": "处于离线状态：请重启模拟器，或执行 adb kill-server 后重试",
}
_SPRINGBOARD = "com.apple.springboard"
# WDA 的截图最后都会转成 PNG，这一项只决定源图格式：用 JPEG 抓，图片、视频多的画面比 PNG、HEIC 源图小三四成、快一倍，
# 纯界面画面差别不大；视频类应用画面一直在动，不关掉「等界面空闲」的话每步操作前都要白等十秒
_WDA_SETTINGS: Dict[str, Any] = {"screenshotQuality": 1, "waitForIdleTimeout": 0, "animationCoolOffTimeout": 0}
_WDA_INVALID_SESSION = "invalid session id"
# 接口不存在时 WDA 返回的 W3C 错误码
_WDA_UNKNOWN_COMMAND = ("unknown command", "unknown method")
_WDA_NO_SUCH_ELEMENT = "no such element"
_W3C_ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"
# 打开没装的应用时，iOS 报的错误里带这些字样
_WDA_APP_NOT_FOUND = ("BSErrorCodeDescription=NotFound", "returned nil for")
# 界面树里不让 WDA 计算可见性：它要逐个元素检查遮挡，长列表能慢二十倍，屏幕外和被挡住的元素由插件按位置推断；
# accessible 只多花零点一秒，留着用来认出应用自己画的标签页、卡片
_WDA_SOURCE_PATH = "/source?format=xml&excluded_attributes=visible"
_WDA_SOURCE_MIN_TIMEOUT = 30.0
_WDA_PRESS_SECONDS = 0.05
# iPhone 没有返回键，用「从屏幕左边缘往右滑」的返回手势代替（相对屏幕宽高的比例）
_BACK_SWIPE = (0.005, 0.5, 0.75, 0.5)
_BACK_SWIPE_MS = 250
# 按主屏幕键点亮屏幕后，隔一会儿看亮了没有
_WAKE_CHECKS = 3
_WAKE_CHECK_SECONDS = 0.5


class DeviceError(RuntimeError):
    """设备操作失败；消息会直接展示给用户。

    默认当作暂时的连接问题（网络抖动、WDA 重启、数据线松动），连接时会在限定时间内重试；
    配置错误、需要人去处理的情况用下面的子类，不重试。
    """

    retryable = True
    # 给 WebUI 连接状态用：transient 暂时连不上，config 配置有误，needs_unlock 要手动解锁，needs_action 要人在手机上操作
    kind = "transient"


class DeviceConfigError(DeviceError):
    """配置有误（找不到 adb、包名不合法、多台设备没指定），重试也没用。"""

    retryable = False
    kind = "config"


class DeviceNeedsUserError(DeviceError):
    """要人去处理才能继续：手动解锁（needs_unlock）、在手机上允许调试（needs_action）。"""

    retryable = False

    def __init__(self, message: str, kind: Literal["needs_unlock", "needs_action"]) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Foreground:
    """一次前台应用查询的结果。

    ``package`` 为空时：``unsupported`` 为真表示这台设备明确给不出前台信息（系统输出里根本没有相关字段、
    接口不存在），否则只是这一次没认出来（切应用的动画、锁屏、系统界面）。
    """

    package: str
    unsupported: bool = False


@dataclass(frozen=True)
class Battery:
    """手机电量：百分比和是不是插着电（充电中或已充满）。读不到时不造一个出来，用 None 表示不知道。"""

    level: int
    charging: bool


class AppNotInstalledError(DeviceError):
    """要打开的应用没有安装在手机上（iPhone 只有打开时才知道）。"""

    def __init__(self, package: str) -> None:
        super().__init__(f"手机上没有安装 {package}")
        self.package = package


class PhoneDevice(ABC):
    """被控制手机的统一接口，坐标一律使用截图像素。"""

    @property
    @abstractmethod
    def description(self) -> str:
        """用于状态展示的设备说明。"""

    @abstractmethod
    async def ensure_ready(self) -> None:
        """确认设备在线可用，不可用时抛出 DeviceError。"""

    @abstractmethod
    async def screenshot(self) -> bytes:
        """返回当前屏幕的截图（PNG 或 JPEG）。"""

    @abstractmethod
    async def tap(self, x: int, y: int) -> None:
        """点击。"""

    @abstractmethod
    async def long_press(self, x: int, y: int, duration_ms: int) -> None:
        """长按。"""

    @abstractmethod
    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        """滑动。"""

    @abstractmethod
    async def input_text(self, text: str) -> None:
        """向当前输入框输入文字。"""

    @abstractmethod
    async def press_key(self, key: KeyName) -> None:
        """按下系统按键。"""

    @abstractmethod
    async def launch_app(self, package: str) -> None:
        """打开应用；应用已在后台时会回到它上次停下的页面。"""

    @abstractmethod
    async def stop_app(self, package: str) -> None:
        """关闭应用，下次打开时从首页开始。"""

    @abstractmethod
    async def foreground_package(self) -> str:
        """当前前台应用的包名，无法判断时返回空字符串。"""

    async def foreground(self) -> Foreground:
        """前台应用和这台设备能不能识别前台；能明确判断出不支持的设备要覆盖这个方法。"""
        return Foreground(await self.foreground_package())

    @abstractmethod
    async def home_packages(self) -> Set[str]:
        """桌面（启动器）应用的包名。"""

    @abstractmethod
    async def installed_packages(self, packages: Sequence[str]) -> Set[str]:
        """``packages`` 里已经安装在手机上的那些（iPhone 查不出来，打开时才知道，见 AppNotInstalledError）。"""

    @abstractmethod
    async def ui_elements(self, width: int, height: int, limit: int) -> List[UIElement]:
        """当前界面的可交互元素。"""

    async def focused_input_text(self) -> Optional[str]:
        """当前获得焦点的输入框里的文字；没有点开输入框时是空字符串，这台设备查不了时返回 None。"""
        return None

    async def battery(self) -> Optional[Battery]:
        """电量；这台设备查不了、系统没给出时返回 None（不当成 0%）。不点亮屏幕、不解锁。"""
        return None

    async def screen_viewable(self) -> Optional[bool]:
        """屏幕亮着又没锁、截图看得到内容；熄屏或锁着是 False，这台设备查不了是 None。不点亮、不解锁。"""
        return None

    # 能不能锁屏、自动解锁：iPhone 和安卓真机支持，模拟器不查锁屏、也不锁屏
    supports_lock = False

    async def unlock_if_locked(self) -> bool:
        """锁着屏就试着解开，返回刚才是不是锁着；不支持锁屏的设备不查，返回 False。

        Raises:
            DeviceNeedsUserError: 解不开（设了锁屏密码），kind 是 needs_unlock。
        """
        return False

    async def lock(self) -> bool:
        """锁屏，返回锁上没有；只在 ``supports_lock`` 为真的设备上调用。"""
        raise NotImplementedError(f"{self.description} 不支持锁屏")

    async def close(self) -> None:
        """释放连接等资源；关闭后再调用其它方法会重新连接。"""
        # 安卓每条命令都是单独的 adb 进程，没有要释放的
        return None


async def run_process(args: Sequence[str], timeout: float) -> Tuple[int, bytes, bytes]:
    """异步执行外部命令，不阻塞事件循环。

    Args:
        args: 命令及参数。
        timeout: 超时秒数。

    Returns:
        Tuple[int, bytes, bytes]: 退出码、标准输出、标准错误。
    """
    extra: Dict[str, int] = {}
    if sys.platform == "win32":
        extra["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **extra,
        )
    except FileNotFoundError as exc:
        raise DeviceConfigError(
            f"找不到 adb：{args[0]}。请安装 Android platform-tools，或在插件配置里填写 adb.exe 的完整路径"
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise DeviceError(f"adb 命令超时（{timeout:g} 秒）：{' '.join(args[1:5])}") from None
    except asyncio.CancelledError:
        process.kill()
        raise
    return int(process.returncode or 0), stdout, stderr


def _check_package(package: str) -> None:
    """包名会拼进 shell 命令，先校验格式。"""
    if not _PACKAGE_PATTERN.match(package):
        raise DeviceConfigError(f"包名格式不正确：{package}")


def parse_adb_devices(text: str) -> Dict[str, str]:
    """解析 ``adb devices`` 输出，返回 序列号 -> 状态。"""
    devices: Dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and not line.startswith(("List of devices", "*")):
            devices[parts[0]] = parts[1]
    return devices


def parse_foreground_package(text: str) -> str:
    """从 ``dumpsys window`` 输出中找出前台应用包名。"""
    for pattern in _FOCUS_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return match.group(1)
    return ""


def _network_target(serial: str) -> Optional[Tuple[str, str]]:
    """要用 adb connect 连的设备返回（主机，端口），只填了 IP 时端口是空字符串；USB 序列号返回 None。"""
    match = _HOST_PORT_SERIAL.match(serial)
    if match:
        return match.group(1), match.group(2)
    if _BARE_IP_SERIAL.match(serial):
        return serial, ""
    return None


def parse_android_battery(text: str) -> Optional[Battery]:
    """解析 ``dumpsys battery``：level / scale 算百分比；插着交流电、USB、无线充电，或者 status 是充电中（2）、
    已充满（5）都算插着电。没有 level 时返回 None。"""
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip().lower()] = value.strip().lower()
    level, scale = fields.get("level", ""), fields.get("scale", "100")
    if not level.isdigit():
        return None
    percent = round(int(level) * 100 / int(scale)) if scale.isdigit() and int(scale) > 0 else int(level)
    powered = any(fields.get(f"{source} powered") == "true" for source in ("ac", "usb", "wireless", "dock"))
    return Battery(level=max(0, min(100, percent)), charging=powered or fields.get("status") in ("2", "5"))


def parse_android_lock(text: str) -> Tuple[Optional[bool], Optional[bool]]:
    """从 ``_LOCK_STATE_COMMAND`` 的输出里取出（屏幕亮着吗，锁屏界面在不在），系统没给出的那项是 None。"""
    wakefulness = _WAKEFULNESS_PATTERN.search(text)
    keyguard = _KEYGUARD_PATTERN.search(text)
    return (
        wakefulness.group(1) == "Awake" if wakefulness else None,
        keyguard.group(1) == "true" if keyguard else None,
    )


def _state_error(serial: str, state: str) -> DeviceError:
    """adb 设备状态不对：未授权要人在手机上点允许，离线等情况重试可能恢复。"""
    message = f"设备 {serial} {_STATE_HINTS.get(state, f'状态异常：{state}')}"
    if state == "unauthorized":
        return DeviceNeedsUserError(message, "needs_action")
    return DeviceError(message)


class AdbDevice(PhoneDevice):
    """通过 adb 控制安卓真机或模拟器。"""

    def __init__(
        self,
        settings: DeviceSettings,
        runner: ProcessRunner = run_process,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        port_finder: Callable[[str], str] = query_adb_port,
    ) -> None:
        self._adb_path = settings.adb_path.strip() or "adb"
        self._configured_serial = settings.serial.strip()
        self._auto_connect = settings.auto_connect
        self._timeout = float(settings.command_timeout_seconds)
        self._text_input = settings.text_input
        self._runner = runner
        self._sleep = sleep
        # 问无线调试的手机它现在的端口（会阻塞，放进线程里调用）
        self._port_finder = port_finder
        # 已确认在线的设备序列号；命令失败后清空，下次重新确认
        self._serial = ""

    @property
    def description(self) -> str:
        return f"安卓 · {self._serial or self._configured_serial or '自动选择'}"

    @property
    def supports_lock(self) -> bool:  # type: ignore[override]
        """真机才锁屏、查锁屏；模拟器按连接地址认（还没连上、不知道是哪台时也先不查）。"""
        serial = self._serial or self._configured_serial
        return bool(serial) and not _EMULATOR_SERIAL.match(serial)

    async def _adb_global(self, args: Sequence[str], timeout: Optional[float] = None) -> bytes:
        code, stdout, stderr = await self._runner([self._adb_path, *args], timeout or self._timeout)
        if code != 0:
            message = (stderr or stdout).decode("utf-8", errors="replace").strip()
            # 带 -s 序列号的命令用子命令本身作为提示，例如「adb shell input」
            command = " ".join(args[2:4]) if args[0] == "-s" else args[0]
            raise DeviceError(f"adb {command} 失败：{message or f'退出码 {code}'}")
        return stdout

    async def _adb(self, *args: str, timeout: Optional[float] = None) -> bytes:
        if not self._serial:
            await self.ensure_ready()
        try:
            return await self._adb_global(["-s", self._serial, *args], timeout)
        except DeviceError:
            self._serial = ""
            raise

    async def _shell(self, command: str, timeout: Optional[float] = None) -> str:
        output = await self._adb("shell", command, timeout=timeout)
        return output.decode("utf-8", errors="replace")

    async def _devices(self) -> Dict[str, str]:
        return parse_adb_devices((await self._adb_global(["devices"])).decode("utf-8", errors="replace"))

    async def _adb_connect(self, serial: str) -> str:
        """adb connect；连上了（或本来就连着）返回空字符串，没连上返回 adb 的回答。"""
        text = (await self._adb_global(["connect", serial])).decode("utf-8", errors="replace").strip()
        return "" if text.startswith(("connected to", "already connected to")) else text

    async def _mdns_port(self, host: str) -> str:
        """直接问这个 IP 上无线调试现在的端口（见 mdns.py），它没回答时返回空字符串。"""
        try:
            return await asyncio.to_thread(self._port_finder, host)
        except (OSError, ValueError) as exc:
            raise DeviceError(f"问 {host} 无线调试的端口失败：{exc}") from exc

    async def _connect_network(self, host: str, port: str) -> Tuple[str, Dict[str, str]]:
        """连上 IP:端口 的设备，返回实际用的序列号和设备列表。

        手机上的「无线调试」每次重启（开关它、插拔数据线、重启手机）端口都会变，配置里的端口连不上、
        或者旧端口的连接一直是 offline 时，按 IP 用 mDNS 找它现在的端口；只填了 IP 时直接找。模拟器不找。
        """
        serial = f"{host}:{port}" if port else ""
        problem = "配置里只填了 IP"
        if port:
            problem = await self._adb_connect(serial)
            if not problem:
                devices = await self._devices()
                if devices.get(serial) != "offline" or host in _LOOPBACK_HOSTS:
                    # 未授权这类状态交给后面按状态提示
                    return serial, devices
                problem = "连接处于离线状态"
        if host in _LOOPBACK_HOSTS:
            raise DeviceError(f"adb connect {serial} 失败：{problem}。请确认模拟器已启动并开启了 ADB 调试")
        found = await self._mdns_port(host)
        if not found or found == port:
            raise DeviceError(
                f"连不上 {serial or host}（{problem}），mDNS 也没找到它无线调试的新端口："
                "请确认手机和电脑连着同一个 Wi-Fi、开发者选项里的「无线调试」开着；"
                "插拔过数据线、开关过无线调试后，端口会变"
            )
        # 旧端口的连接 adb 会自己在后台重连一阵，列表里间歇显示 offline，之后自己消失；不去断开它：
        # 赶上它刚好不在时 adb disconnect 会报 no such device，反而让这次连接失败
        serial = f"{host}:{found}"
        problem = await self._adb_connect(serial)
        if problem:
            raise DeviceError(f"adb connect {serial} 失败：{problem}")
        return serial, await self._devices()

    async def ensure_ready(self) -> None:
        serial = self._configured_serial
        target = _network_target(serial) if self._auto_connect else None
        if target is not None:
            serial, devices = await self._connect_network(*target)
        else:
            devices = await self._devices()
        if serial:
            state = devices.get(serial)
            if state is None:
                connected = "、".join(devices) or "无"
                raise DeviceError(f"没有找到设备 {serial}（当前已连接：{connected}）")
            if state != "device":
                raise _state_error(serial, state)
            self._serial = serial
            return

        if not devices:
            raise DeviceError(
                "没有检测到安卓设备。请用 USB 连接手机并开启 USB 调试，"
                "或启动模拟器后在插件配置里填写 serial（如 127.0.0.1:16384）"
            )
        online = [name for name, state in devices.items() if state == "device"]
        if not online:
            name, state = next(iter(devices.items()))
            raise _state_error(name, state)
        if len(online) > 1:
            raise DeviceConfigError(f"检测到多台设备（{'、'.join(online)}），请在插件配置的 serial 中指定一台")
        self._serial = online[0]

    async def screenshot(self) -> bytes:
        data = await self._adb("exec-out", "screencap", "-p")
        if not data.startswith(_PNG_SIGNATURE):
            raise DeviceError("截图数据无效：设备可能处于锁屏或黑屏状态")
        return data

    async def tap(self, x: int, y: int) -> None:
        await self._shell(f"input tap {int(x)} {int(y)}")

    async def long_press(self, x: int, y: int, duration_ms: int) -> None:
        await self._shell(f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {int(duration_ms)}")

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        await self._shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")

    async def input_text(self, text: str) -> None:
        if self._text_input == "adb_keyboard":
            ime = (await self._shell("settings get secure default_input_method")).strip()
            if not ime.startswith("com.android.adbkeyboard/"):
                raise DeviceError(
                    f"当前输入法是 {ime or '未知'}，不是 ADBKeyboard。请安装 ADBKeyboard 后执行 "
                    f"adb shell ime set {_ADB_KEYBOARD_IME}，或把配置里的输入方式改为 ascii_only"
                )
            # Base64 只含安全字符，不需要再做 shell 转义
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            await self._shell(f"am broadcast -a ADB_INPUT_B64 --es msg {payload}")
            return
        if not text.isascii():
            raise DeviceError(
                "当前输入方式只支持英文和数字；输入中文需要安装 ADBKeyboard，并把输入方式改为 adb_keyboard"
            )
        await self._shell("input text " + shlex.quote(text.replace(" ", "%s")))

    async def press_key(self, key: KeyName) -> None:
        await self._shell(f"input keyevent {_KEYCODES[key]}")

    async def launch_app(self, package: str) -> None:
        _check_package(package)
        output = await self._shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")
        if "No activities found" in output or "aborted" in output:
            raise DeviceError(f"打开 {package} 失败：手机上可能没有安装这个应用")

    async def stop_app(self, package: str) -> None:
        _check_package(package)
        await self._shell(f"am force-stop {package}")

    async def foreground_package(self) -> str:
        return (await self.foreground()).package

    async def foreground(self) -> Foreground:
        # grep 没有匹配时返回 1，用 || true 让「无法判断」走正常返回路径
        text = await self._shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' || true")
        package = parse_foreground_package(text)
        # 输出里连这两个字段都没有，说明这个系统的 dumpsys 格式给不出前台；
        # 有字段但为空（mCurrentFocus=null）只是这一次没认出来
        return Foreground(package, unsupported=not package and not text.strip())

    async def home_packages(self) -> Set[str]:
        text = await self._shell(
            "cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME"
        )
        packages = {line.split("/", 1)[0].strip() for line in text.splitlines() if "/" in line}
        if not packages:
            raise DeviceError("无法识别桌面应用，请把桌面的包名填到 extra_allowed_packages")
        return packages

    async def installed_packages(self, packages: Sequence[str]) -> Set[str]:
        text = await self._shell("pm list packages")
        lines = (line.strip() for line in text.splitlines())
        installed = {line[len("package:") :] for line in lines if line.startswith("package:")}
        return installed & set(packages)

    async def _lock_state(self) -> Tuple[Optional[bool], Optional[bool]]:
        return parse_android_lock(await self._shell(_LOCK_STATE_COMMAND))

    @staticmethod
    def _is_locked(state: Tuple[Optional[bool], Optional[bool]]) -> bool:
        awake, keyguard = state
        return awake is False or keyguard is True

    async def unlock_if_locked(self) -> bool:
        if not self.supports_lock:
            return False
        state = await self._lock_state()
        if not self._is_locked(state):
            return False
        if state[0] is False:
            await self._shell(f"input keyevent {_KEYCODE_WAKEUP}")
        # 没设锁屏方式时点亮就进去了；滑动解锁的锁屏界面能直接划掉，设了密码、图案时只会弹出输密码的界面
        await self._shell("wm dismiss-keyguard")
        for _ in range(_ADB_LOCK_CHECKS):
            await self._sleep(_ADB_LOCK_CHECK_SECONDS)
            if not self._is_locked(await self._lock_state()):
                return True
        raise DeviceNeedsUserError(
            "安卓手机锁着屏，自动解锁没成功（设了锁屏密码、图案时解不开）：请手动解锁；"
            "想让她自己解开，就把这台手机的锁屏方式改成「无」或「滑动」，或者关掉「玩完锁屏」（play.lock_after）",
            "needs_unlock",
        )

    async def lock(self) -> bool:
        await self._shell(f"input keyevent {_KEYCODE_SLEEP}")
        for _ in range(_ADB_LOCK_CHECKS):
            await self._sleep(_ADB_LOCK_CHECK_SECONDS)
            if self._is_locked(await self._lock_state()):
                return True
        return False

    async def battery(self) -> Optional[Battery]:
        return parse_android_battery(await self._shell("dumpsys battery"))

    async def screen_viewable(self) -> Optional[bool]:
        awake, locked = await self._lock_state()
        if awake is False or locked is True:
            return False
        return True if awake and locked is False else None

    async def ui_elements(self, width: int, height: int, limit: int) -> List[UIElement]:
        output = await self._shell(f"uiautomator dump {_UI_DUMP_PATH}", timeout=max(self._timeout, 15.0))
        if "dumped to" not in output:
            raise DeviceError(f"读取界面元素失败：{output.strip() or '没有输出'}")
        xml_text = (await self._adb("exec-out", "cat", _UI_DUMP_PATH)).decode("utf-8", errors="replace")
        try:
            # 复杂界面的 XML 解析要几十毫秒，放进线程，别卡住宿主的事件循环
            return await asyncio.to_thread(parse_uiautomator_xml, xml_text, width, height, limit)
        except ValueError as exc:
            raise DeviceError(str(exc)) from exc


class _WdaError(DeviceError):
    """WebDriverAgent 返回的错误，``code`` 是 W3C 错误码（如 invalid session id），``detail`` 是完整的错误信息。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"WebDriverAgent 报错：{_first_line(detail) or code}")
        self.code = code
        self.detail = detail


def _first_line(text: Any, limit: int = 200) -> str:
    """WDA 的错误信息常带一长串调用栈，只留第一行。"""
    lines = str(text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def _parse_screen(value: Any) -> Tuple[float, Tuple[float, float]]:
    """从 ``/wda/screen`` 的返回里取出屏幕倍率和以「点」为单位的屏幕宽高。"""
    try:
        scale = float(value["scale"])
        size = (float(value["screenSize"]["width"]), float(value["screenSize"]["height"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise DeviceError(f"WebDriverAgent 返回的屏幕信息无效：{value}") from exc
    if scale <= 0 or min(size) <= 0:
        raise DeviceError(f"WebDriverAgent 返回的屏幕信息无效：{value}")
    return scale, size


class WdaDevice(PhoneDevice):
    """通过 WebDriverAgent（WDA）控制 iPhone。

    WDA 的坐标单位是「点」，截图是像素，两者差一个屏幕倍率（iPhone 14 是 3）。
    对外仍然一律用截图像素，发给 WDA 前再除以倍率。
    """

    def __init__(
        self,
        settings: DeviceSettings,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base_url = settings.wda_url.strip().rstrip("/")
        self._timeout = float(settings.command_timeout_seconds)
        self._transport = transport
        self._sleep = sleep
        # 复用同一个客户端：每次新建都要加载一遍证书库，要半秒多，每步操作都会慢下来
        self._client: Optional[httpx.AsyncClient] = None
        # 会话和倍率在 ensure_ready 里获取；会话失效后清空，下次重新建立
        self._session_id = ""
        self._scale = 0.0
        # WDA 查不到应用装没装，打开时发现没装的记在这里，之后不再列出
        self._missing_apps: Set[str] = set()

    @property
    def description(self) -> str:
        return f"iPhone · {self._base_url}"

    async def _request(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None
    ) -> Any:
        seconds = timeout or self._timeout
        if self._client is None or self._client.is_closed:
            # 不读系统代理：WDA 在本机端口上，走代理反而连不上
            self._client = httpx.AsyncClient(transport=self._transport, trust_env=False)
        try:
            response = await self._client.request(method, self._base_url + path, json=payload, timeout=seconds)
        except httpx.TimeoutException:
            raise DeviceError(f"WebDriverAgent 请求超时（{seconds:g} 秒）：{method} {path}") from None
        except httpx.TransportError as exc:
            raise DeviceError(
                f"连不上 iPhone 上的 WebDriverAgent（{self._base_url}）：无线连接时确认 iPhone 和电脑在同一个 Wi-Fi、"
                "启动 WDA 的 pymobiledevice3 和端口转发在运行；有线连接时确认数据线连好、"
                "ios runwda 和 ios forward 在运行"
            ) from exc
        try:
            data = response.json()
        except ValueError:
            raise DeviceError(
                f"WebDriverAgent 返回的不是 JSON（HTTP {response.status_code}）：{method} {path}"
            ) from None
        value = data.get("value") if isinstance(data, dict) else None
        if isinstance(value, dict) and "error" in value:
            raise _WdaError(str(value["error"]), str(value.get("message") or ""))
        if response.is_error:
            raise DeviceError(f"WebDriverAgent 请求失败（HTTP {response.status_code}）：{method} {path}")
        return value

    async def _session_request(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None
    ) -> Any:
        if not self._session_id:
            await self.ensure_ready()
        try:
            return await self._request(method, f"/session/{self._session_id}{path}", payload, timeout)
        except _WdaError as exc:
            if exc.code != _WDA_INVALID_SESSION:
                raise
        # WDA 重启过，或者会话被别的程序顶掉了：重建会话再试一次
        self._session_id = ""
        await self.ensure_ready()
        return await self._request(method, f"/session/{self._session_id}{path}", payload, timeout)

    async def _get_scale(self) -> float:
        if not self._session_id:
            await self.ensure_ready()
        return self._scale

    async def _drag(self, x1: float, y1: float, x2: float, y2: float, duration_ms: int) -> None:
        """按下后拖到终点，坐标单位是点；拖动速度按距离和时长换算。"""
        velocity = math.hypot(x2 - x1, y2 - y1) * 1000 / max(1, duration_ms)
        await self._session_request(
            "POST",
            "/wda/pressAndDragWithVelocity",
            {
                "fromX": x1,
                "fromY": y1,
                "toX": x2,
                "toY": y2,
                "pressDuration": _WDA_PRESS_SECONDS,
                "holdDuration": 0,
                "velocity": max(1.0, velocity),
            },
        )

    supports_lock = True

    async def unlock_if_locked(self) -> bool:
        if not await self._request("GET", "/wda/locked"):
            return await self._wake_if_dark()
        # 没设锁屏密码时 WDA 能自己解锁（点亮屏幕再上滑）；设了密码只会停在输密码的界面
        await self._request("POST", "/wda/unlock")
        if await self._request("GET", "/wda/locked"):
            raise DeviceNeedsUserError(
                "iPhone 锁着屏，自动解锁没成功（设了锁屏密码时解不开）：请手动解锁；"
                "想让她自己解开，就别给这台 iPhone 设锁屏密码，或者关掉「玩完锁屏」（play.lock_after）",
                "needs_unlock",
            )
        return True

    async def _wake_if_dark(self) -> bool:
        """实测有人动过手机之后出现过：屏幕关着，/wda/locked 却说没锁，/wda/unlock 也就什么都不做；
        这时按一下主屏幕键就点亮回到桌面。只在前台是桌面时这么做：应用里按主屏幕键会退出应用，
        应用里全黑的画面也可能只是视频黑场。返回点亮过没有。

        Raises:
            DeviceNeedsUserError: 按了主屏幕键还是黑的。
        """
        if await self.foreground_package() != _SPRINGBOARD or not await self._screen_dark():
            return False
        await self._request("POST", "/wda/homescreen")
        for _ in range(_WAKE_CHECKS):
            await self._sleep(_WAKE_CHECK_SECONDS)
            if not await self._screen_dark():
                return True
        raise DeviceNeedsUserError("iPhone 屏幕关着，按主屏幕键也没点亮：请手动点亮、解锁手机", "needs_unlock")

    async def _screen_dark(self) -> bool:
        return await asyncio.to_thread(is_dark_screen, await self.screenshot())

    async def lock(self) -> bool:
        await self._request("POST", "/wda/lock")
        return bool(await self._request("GET", "/wda/locked"))

    async def ensure_ready(self) -> None:
        status = await self._request("GET", "/status")
        if isinstance(status, dict) and status.get("ready") is False:
            raise DeviceError("iPhone 上的 WebDriverAgent 还没准备好，请过几秒再试")
        if not self._session_id:
            created = await self._request("POST", "/session", {"capabilities": {"alwaysMatch": {}}})
            session_id = str(created.get("sessionId") or "") if isinstance(created, dict) else ""
            if not session_id:
                raise DeviceError(f"WebDriverAgent 没有返回会话 ID：{created}")
            await self._request("POST", f"/session/{session_id}/appium/settings", {"settings": _WDA_SETTINGS})
            self._scale, _ = _parse_screen(await self._request("GET", "/wda/screen"))
            self._session_id = session_id
        # 这里不解锁：WebUI 空闲时刷新画面也会走到这里，玩完锁上的屏不能被它解开；要玩时由 unlock_if_locked 解

    async def screenshot(self) -> bytes:
        if not self._session_id:
            # 先建会话，设置好截图方式
            await self.ensure_ready()
        value = await self._request("GET", "/screenshot")
        try:
            data = base64.b64decode(str(value), validate=True)
        except binascii.Error as exc:
            raise DeviceError("截图数据无效：WebDriverAgent 返回的不是 base64") from exc
        if not data.startswith((_PNG_SIGNATURE, _JPEG_SIGNATURE)):
            # 这个版本的 WDA 总是返回 PNG；换了别的版本时可能不同，下次操作时重新建会话、重新设置
            self._session_id = ""
            raise DeviceError("截图格式不对（不是 PNG 或 JPEG），下次操作时会重新设置 WebDriverAgent")
        return data

    async def tap(self, x: int, y: int) -> None:
        scale = await self._get_scale()
        await self._session_request("POST", "/wda/tap", {"x": x / scale, "y": y / scale})

    async def long_press(self, x: int, y: int, duration_ms: int) -> None:
        scale = await self._get_scale()
        await self._session_request(
            "POST", "/wda/touchAndHold", {"x": x / scale, "y": y / scale, "duration": duration_ms / 1000}
        )

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        scale = await self._get_scale()
        await self._drag(x1 / scale, y1 / scale, x2 / scale, y2 / scale, duration_ms)

    async def input_text(self, text: str) -> None:
        try:
            await self._session_request("POST", "/wda/keys", {"value": [text]})
        except _WdaError as exc:
            raise DeviceError(f"输入文字失败，可能还没点开输入框：{exc}") from exc

    async def press_key(self, key: KeyName) -> None:
        if key == "home":
            await self._request("POST", "/wda/homescreen")
            return
        if key == "enter":
            await self._session_request("POST", "/wda/keys", {"value": ["\n"]})
            return
        await self._get_scale()
        # 横屏的游戏（如欢乐斗地主）里屏幕宽高和建会话时不一样，每次现查；按建会话时的竖屏尺寸算，起点在屏幕外
        _, (width, height) = _parse_screen(await self._request("GET", "/wda/screen"))
        x1, y1, x2, y2 = _BACK_SWIPE
        await self._drag(width * x1, height * y1, width * x2, height * y2, _BACK_SWIPE_MS)

    async def launch_app(self, package: str) -> None:
        # 从别的应用直接切过去，iOS 会在左上角显示「◀ 上一个应用」，实测模型会以为还在那个应用里；先回桌面再打开就没有
        if await self.foreground_package() not in (package, _SPRINGBOARD):
            await self._request("POST", "/wda/homescreen")
        # activate：没在运行就启动，已在后台就切回上次停下的页面
        try:
            await self._session_request("POST", "/wda/apps/activate", {"bundleId": package})
        except _WdaError as exc:
            if any(marker in exc.detail for marker in _WDA_APP_NOT_FOUND):
                self._missing_apps.add(package)
                raise AppNotInstalledError(package) from exc
            raise DeviceError(f"打开 {package} 失败：{exc}") from exc

    async def stop_app(self, package: str) -> None:
        await self._session_request("POST", "/wda/apps/terminate", {"bundleId": package})

    async def foreground_package(self) -> str:
        return (await self.foreground()).package

    async def foreground(self) -> Foreground:
        try:
            info = await self._request("GET", "/wda/activeAppInfo")
        except _WdaError as exc:
            # 老版本 WDA 没有这个接口：明确不支持；其它错误照常抛出
            if exc.code in _WDA_UNKNOWN_COMMAND:
                return Foreground("", unsupported=True)
            raise
        return Foreground(str(info.get("bundleId") or "") if isinstance(info, dict) else "")

    async def home_packages(self) -> Set[str]:
        return {_SPRINGBOARD}

    async def installed_packages(self, packages: Sequence[str]) -> Set[str]:
        # WDA 只能查应用运行状态，没装的和装了没运行一样都是 1，分不出来；
        # 先当作都装了，打开时发现没装的（AppNotInstalledError）记下来，以后去掉
        return set(packages) - self._missing_apps

    async def focused_input_text(self) -> Optional[str]:
        # 键盘没弹出来（比如开着应用自己的表情面板）时没有获得焦点的元素，WDA 回答 no such element
        try:
            active = await self._session_request("GET", "/element/active")
        except _WdaError as exc:
            if exc.code == _WDA_NO_SUCH_ELEMENT:
                return ""
            raise
        element_id = (active.get("ELEMENT") or active.get(_W3C_ELEMENT_KEY)) if isinstance(active, dict) else None
        if not element_id:
            return ""
        value = await self._session_request("GET", f"/element/{element_id}/attribute/value")
        return str(value or "")

    async def screen_viewable(self) -> Optional[bool]:
        """锁着就是看不到；没锁时当作亮着（屏幕关着却没锁的少见情况由 unlock_if_locked 点亮）。"""
        return not await self._request("GET", "/wda/locked")

    async def battery(self) -> Optional[Battery]:
        """WDA 的 batteryInfo：level 是 0~1（读不到是负数），state 1 没插电、2 充电中、3 已充满、0 不知道。"""
        return parse_wda_battery(await self._session_request("GET", "/wda/batteryInfo"))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ui_elements(self, width: int, height: int, limit: int) -> List[UIElement]:
        scale = await self._get_scale()
        xml_text = await self._request("GET", _WDA_SOURCE_PATH, timeout=max(self._timeout, _WDA_SOURCE_MIN_TIMEOUT))
        if not isinstance(xml_text, str):
            raise DeviceError("WebDriverAgent 返回的界面树无效")
        try:
            return await asyncio.to_thread(parse_wda_xml, xml_text, width, height, scale, limit)
        except ValueError as exc:
            raise DeviceError(str(exc)) from exc


def parse_wda_battery(value: Any) -> Optional[Battery]:
    """解析 WDA 的 batteryInfo；level 不是 0~1 的数（模拟器、读不到时是 -1）返回 None。"""
    if not isinstance(value, dict):
        return None
    level, state = value.get("level"), value.get("state")
    if isinstance(level, bool) or not isinstance(level, (int, float)) or not 0 <= level <= 1:
        return None
    return Battery(level=round(level * 100), charging=state in (2, 3))


def create_device(settings: DeviceSettings) -> PhoneDevice:
    """按配置创建设备实例。"""
    if settings.backend == "ios_wda":
        return WdaDevice(settings)
    return AdbDevice(settings)
