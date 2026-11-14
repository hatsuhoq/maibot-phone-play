from typing import Dict, List, Optional, Sequence, Tuple

import base64
import pytest

from phone_play.devices import (
    AdbDevice,
    DeviceError,
    DeviceNeedsUserError,
    WdaDevice,
    create_device,
    parse_android_lock,
    parse_foreground_package,
    run_process,
)
from phone_play.settings import DeviceSettings

from conftest import make_png

DEVICES_ONE = b"List of devices attached\nemulator-5554\tdevice\n\n"


class FakeRunner:
    """按命令前缀返回预设结果，并记录所有 adb 调用。"""

    def __init__(self, responses: Dict[str, Tuple[int, bytes, bytes]]) -> None:
        self.responses = responses
        self.calls: List[List[str]] = []

    async def __call__(self, args: Sequence[str], timeout: float) -> Tuple[int, bytes, bytes]:
        command = list(args[1:])
        self.calls.append(command)
        if command[:1] == ["-s"]:
            command = command[2:]
        joined = " ".join(command)
        for prefix, response in self.responses.items():
            if joined.startswith(prefix):
                return response
        raise AssertionError(f"没有预设的 adb 命令：{joined}")


def make_device(responses, *, ports: Optional[Dict[str, str]] = None, **settings) -> Tuple[AdbDevice, FakeRunner]:
    """``ports`` 是假的无线调试端口查询结果（IP → 端口），查过的 IP 记在 runner.asked 里。"""
    runner = FakeRunner(responses)
    runner.asked = []

    def find_port(host: str) -> str:
        runner.asked.append(host)
        return (ports or {}).get(host, "")

    return AdbDevice(DeviceSettings(**settings), runner=runner, port_finder=find_port), runner


async def test_single_device_is_selected_and_used_with_serial():
    device, runner = make_device({"devices": (0, DEVICES_ONE, b""), "shell input tap": (0, b"", b"")})
    await device.tap(10, 20)
    assert runner.calls[-1] == ["-s", "emulator-5554", "shell", "input tap 10 20"]
    assert device.description == "安卓 · emulator-5554"


async def test_multiple_devices_require_serial():
    output = b"List of devices attached\na\tdevice\nb\tdevice\n"
    device, _ = make_device({"devices": (0, output, b"")})
    with pytest.raises(DeviceError, match="多台设备"):
        await device.ensure_ready()


async def test_unauthorized_device_has_hint():
    device, _ = make_device({"devices": (0, b"List of devices attached\nabc\tunauthorized\n", b"")})
    with pytest.raises(DeviceError, match="允许 USB 调试"):
        await device.ensure_ready()


async def test_no_device_has_hint():
    device, _ = make_device({"devices": (0, b"List of devices attached\n\n", b"")})
    with pytest.raises(DeviceError, match="没有检测到安卓设备"):
        await device.ensure_ready()


async def test_network_serial_is_connected_first():
    output = b"List of devices attached\n127.0.0.1:16384\tdevice\n"
    device, runner = make_device(
        {"connect": (0, b"connected to 127.0.0.1:16384\n", b""), "devices": (0, output, b"")},
        serial="127.0.0.1:16384",
    )
    await device.ensure_ready()
    assert runner.calls[0] == ["connect", "127.0.0.1:16384"]


async def test_failed_connect_is_reported():
    device, runner = make_device(
        {"connect": (0, b"cannot connect to 127.0.0.1:16384: refused\n", b"")}, serial="127.0.0.1:16384"
    )
    with pytest.raises(DeviceError, match="adb connect.*请确认模拟器已启动"):
        await device.ensure_ready()
    # 模拟器不去找无线调试端口
    assert runner.asked == []


PHONE = "192.168.1.23"
REFUSED = b"cannot connect to 192.168.1.23:39735: (10061)\n"


async def test_wireless_phone_whose_port_changed_is_found_with_mdns():
    # 插拔过数据线、开关过无线调试，配置里还是旧端口
    device, runner = make_device(
        {
            f"connect {PHONE}:39735": (0, REFUSED, b""),
            f"connect {PHONE}:40135": (0, f"connected to {PHONE}:40135\n".encode(), b""),
            "devices": (0, f"List of devices attached\n{PHONE}:40135\tdevice\n".encode(), b""),
            "shell input tap": (0, b"", b""),
        },
        ports={PHONE: "40135"},
        serial=f"{PHONE}:39735",
    )
    await device.ensure_ready()
    assert device.description == f"安卓 · {PHONE}:40135"
    assert device.supports_lock is True
    await device.tap(1, 2)
    assert runner.calls[-1][:2] == ["-s", f"{PHONE}:40135"]


async def test_wireless_phone_can_be_configured_with_ip_only():
    device, runner = make_device(
        {
            f"connect {PHONE}:40135": (0, f"connected to {PHONE}:40135\n".encode(), b""),
            "devices": (0, f"List of devices attached\n{PHONE}:40135\tdevice\n".encode(), b""),
        },
        ports={PHONE: "40135"},
        serial=PHONE,
    )
    await device.ensure_ready()
    assert device.description == f"安卓 · {PHONE}:40135"
    assert runner.asked == [PHONE]
    assert runner.calls[0] == ["connect", f"{PHONE}:40135"]


async def test_working_wireless_port_is_not_looked_up():
    device, runner = make_device(
        {
            f"connect {PHONE}:40135": (0, f"already connected to {PHONE}:40135\n".encode(), b""),
            "devices": (0, f"List of devices attached\n{PHONE}:40135\tdevice\n".encode(), b""),
        },
        ports={PHONE: "40135"},
        serial=f"{PHONE}:40135",
    )
    await device.ensure_ready()
    assert runner.asked == []


async def test_stale_offline_wireless_connection_is_replaced():
    # 旧端口的连接还挂在 adb 里，一直是 offline，adb connect 却说已经连着
    device, runner = make_device(
        {
            f"connect {PHONE}:39735": (0, f"already connected to {PHONE}:39735\n".encode(), b""),
            f"connect {PHONE}:40135": (0, f"connected to {PHONE}:40135\n".encode(), b""),
            "devices": (
                0,
                f"List of devices attached\n{PHONE}:39735\toffline\n{PHONE}:40135\tdevice\n".encode(),
                b"",
            ),
        },
        ports={PHONE: "40135"},
        serial=f"{PHONE}:39735",
    )
    await device.ensure_ready()
    assert device.description == f"安卓 · {PHONE}:40135"
    # 旧连接 adb 会自己清理，不去断开（赶上它刚好消失时 adb disconnect 会报错）
    assert not any(call[0] == "disconnect" for call in runner.calls)


async def test_wireless_phone_without_mdns_service_is_explained():
    device, _ = make_device({f"connect {PHONE}:39735": (0, REFUSED, b"")}, serial=f"{PHONE}:39735")
    with pytest.raises(DeviceError, match="mDNS 也没找到.*「无线调试」开着") as raised:
        await device.ensure_ready()
    # 暂时连不上，开始时会在 20 秒内重试
    assert raised.value.retryable


async def test_failed_port_lookup_is_reported():
    def broken(host: str) -> str:
        raise OSError("网络不可达")

    device = AdbDevice(DeviceSettings(serial=PHONE), runner=FakeRunner({}), port_finder=broken)
    with pytest.raises(DeviceError, match="问 192.168.1.23 无线调试的端口失败：网络不可达"):
        await device.ensure_ready()


async def test_unauthorized_wireless_phone_still_asks_for_permission():
    device, _ = make_device(
        {
            f"connect {PHONE}:40135": (0, f"connected to {PHONE}:40135\n".encode(), b""),
            "devices": (0, f"List of devices attached\n{PHONE}:40135\tunauthorized\n".encode(), b""),
        },
        serial=f"{PHONE}:40135",
    )
    with pytest.raises(DeviceError, match="允许 USB 调试") as raised:
        await device.ensure_ready()
    assert raised.value.kind == "needs_action"


async def test_command_failure_resets_serial_for_recheck():
    device, runner = make_device({"devices": (0, DEVICES_ONE, b""), "shell input": (1, b"", b"error: closed")})
    with pytest.raises(DeviceError, match="adb shell input tap 1 2 失败：error: closed"):
        await device.tap(1, 2)
    with pytest.raises(DeviceError):
        await device.tap(1, 2)
    assert [call for call in runner.calls if call == ["devices"]] == [["devices"], ["devices"]]


async def test_screenshot_validates_png():
    device, _ = make_device({"devices": (0, DEVICES_ONE, b""), "exec-out screencap": (0, b"not png", b"")})
    with pytest.raises(DeviceError, match="截图数据无效"):
        await device.screenshot()
    png = make_png(10, 20)
    device, _ = make_device({"devices": (0, DEVICES_ONE, b""), "exec-out screencap": (0, png, b"")})
    assert await device.screenshot() == png


async def test_ascii_only_rejects_chinese_and_escapes_spaces():
    device, runner = make_device(
        {"devices": (0, DEVICES_ONE, b""), "shell input text": (0, b"", b"")}, text_input="ascii_only"
    )
    with pytest.raises(DeviceError, match="ADBKeyboard"):
        await device.input_text("猫猫")
    await device.input_text("cat video")
    assert runner.calls[-1][-1] == "input text cat%svideo"


async def test_adb_keyboard_requires_active_ime_and_sends_base64():
    device, runner = make_device(
        {
            "devices": (0, DEVICES_ONE, b""),
            "shell settings get": (0, b"com.android.adbkeyboard/.AdbIME\n", b""),
            "shell am broadcast": (0, b"Broadcast completed: result=0", b""),
        }
    )
    await device.input_text("猫猫 视频")
    payload = base64.b64encode("猫猫 视频".encode("utf-8")).decode("ascii")
    assert runner.calls[-1][-1] == f"am broadcast -a ADB_INPUT_B64 --es msg {payload}"

    other_ime, _ = make_device(
        {"devices": (0, DEVICES_ONE, b""), "shell settings get": (0, b"com.google.android.inputmethod.latin/.Ime", b"")}
    )
    with pytest.raises(DeviceError, match="不是 ADBKeyboard"):
        await other_ime.input_text("猫猫")


async def test_launch_app_validates_package_and_reports_missing_app():
    device, _ = make_device(
        {"devices": (0, DEVICES_ONE, b""), "shell monkey": (0, b"** No activities found to run, monkey aborted.", b"")}
    )
    with pytest.raises(DeviceError, match="包名格式不正确"):
        await device.launch_app("tv.bili; rm -rf /")
    with pytest.raises(DeviceError, match="没有安装"):
        await device.launch_app("tv.danmaku.bili")


async def test_stop_app_force_stops_valid_package_only():
    device, runner = make_device({"devices": (0, DEVICES_ONE, b""), "shell am force-stop": (0, b"", b"")})
    await device.stop_app("tv.danmaku.bili")
    assert runner.calls[-1] == ["-s", "emulator-5554", "shell", "am force-stop tv.danmaku.bili"]
    with pytest.raises(DeviceError, match="包名格式不正确"):
        await device.stop_app("tv.bili && reboot")


async def test_installed_and_home_packages():
    device, _ = make_device(
        {
            "devices": (0, DEVICES_ONE, b""),
            "shell pm list packages": (0, b"package:tv.danmaku.bili\npackage:com.android.launcher3\n", b""),
            "shell cmd package resolve-activity": (
                0,
                b"priority=0 preferredOrder=0\ncom.android.launcher3/.Launcher\n",
                b"",
            ),
        }
    )
    assert await device.installed_packages(["tv.danmaku.bili", "com.xingin.xhs"]) == {"tv.danmaku.bili"}
    assert await device.home_packages() == {"com.android.launcher3"}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("  mCurrentFocus=Window{a1b2 u0 tv.danmaku.bili/tv.danmaku.bili.MainActivityV2}", "tv.danmaku.bili"),
        (
            "  mCurrentFocus=Window{a1 u0 NotificationShade}\n"
            "  mFocusedApp=ActivityRecord{c3 u0 com.xingin.xhs/.index.v2.IndexActivityV2 t12}",
            "com.xingin.xhs",
        ),
        ("  mCurrentFocus=null\n  mFocusedApp=null", ""),
    ],
)
def test_parse_foreground_package(text, expected):
    assert parse_foreground_package(text) == expected


async def test_missing_adb_binary_is_explained():
    with pytest.raises(DeviceError, match="找不到 adb"):
        await run_process(["definitely-not-adb-phone-play"], 5)


def test_create_device_picks_backend():
    assert isinstance(create_device(DeviceSettings()), AdbDevice)
    assert isinstance(create_device(DeviceSettings(backend="ios_wda")), WdaDevice)


@pytest.mark.parametrize("serial", ["127.0.0.1:16416", "localhost:5555", "emulator-5554"])
async def test_emulators_do_not_check_or_lock_the_screen(serial):
    # 模拟器不用锁屏：一条命令都不发
    device, runner = make_device({}, serial=serial)
    assert device.supports_lock is False
    assert await device.unlock_if_locked() is False
    assert runner.calls == []


@pytest.mark.parametrize("serial", ["0123ABCD", "192.168.1.23:5555"])
def test_real_phones_support_lock(serial):
    # USB 连的、无线调试连的真机
    device, _ = make_device({}, serial=serial)
    assert device.supports_lock is True


REAL_SERIAL = "0123ABCD"


def lock_state(wakefulness: str, showing: str) -> bytes:
    """``_LOCK_STATE_COMMAND`` 的输出：dumpsys power 的一行，加 KeyguardServiceDelegate 那一段里的一行。"""
    return f"  mWakefulness={wakefulness}\n    showing={showing}\n".encode()


class LockRunner(FakeRunner):
    """查锁屏状态时按顺序返回 ``states``（用完了一直返回最后一个），其它命令照常。"""

    def __init__(self, states: List[bytes]) -> None:
        super().__init__(
            {
                "devices": (0, f"List of devices attached\n{REAL_SERIAL}\tdevice\n".encode(), b""),
                "shell input keyevent": (0, b"", b""),
                "shell wm": (0, b"", b""),
            }
        )
        self.states = states

    async def __call__(self, args: Sequence[str], timeout: float) -> Tuple[int, bytes, bytes]:
        if args[-1].startswith("dumpsys power"):
            self.calls.append(list(args[1:]))
            return 0, self.states.pop(0) if len(self.states) > 1 else self.states[0], b""
        return await super().__call__(args, timeout)

    def shell(self) -> List[str]:
        return [call[-1] for call in self.calls if call[2:3] == ["shell"] and not call[-1].startswith("dumpsys")]


def make_lock_device(states: List[bytes]) -> Tuple[AdbDevice, LockRunner, List[float]]:
    runner = LockRunner(states)
    slept: List[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return AdbDevice(DeviceSettings(serial=REAL_SERIAL), runner=runner, sleep=sleep), runner, slept


@pytest.mark.parametrize(
    "text,expected",
    [
        (lock_state("Awake", "false"), (True, False)),
        (lock_state("Asleep", "true"), (False, True)),
        # KeyguardStateMonitor 那一段的 mIsShowing、KeyguardServiceDelegate 的 showingAndNotOccluded 都不算
        (b"  mWakefulness=Dozing\n      showingAndNotOccluded=true\n        mIsShowing=true\n", (False, None)),
        (b"", (None, None)),
    ],
)
def test_parse_android_lock(text, expected):
    assert parse_android_lock(text.decode()) == expected


async def test_android_unlocked_screen_is_left_alone():
    device, runner, _ = make_lock_device([lock_state("Awake", "false")])
    assert await device.unlock_if_locked() is False
    assert runner.shell() == []
    # 系统给不出锁屏状态时当作没锁，照常玩
    device, runner, _ = make_lock_device([b""])
    assert await device.unlock_if_locked() is False
    assert runner.shell() == []


async def test_android_screen_off_is_woken_and_keyguard_dismissed():
    # 屏幕关着、锁屏界面是滑动解锁：点亮、划掉，划掉的动画要一会儿
    device, runner, slept = make_lock_device(
        [lock_state("Asleep", "true"), lock_state("Awake", "true"), lock_state("Awake", "false")]
    )
    assert await device.unlock_if_locked() is True
    assert runner.shell() == ["input keyevent 224", "wm dismiss-keyguard"]
    assert slept == [0.5, 0.5]


async def test_android_keyguard_with_password_needs_the_user():
    device, runner, slept = make_lock_device([lock_state("Awake", "true")])
    with pytest.raises(DeviceNeedsUserError, match="安卓手机锁着屏，自动解锁没成功") as raised:
        await device.unlock_if_locked()
    assert raised.value.kind == "needs_unlock"
    # 屏幕本来就亮着，不用再点亮
    assert runner.shell() == ["wm dismiss-keyguard"]
    assert len(slept) == 4


async def test_android_lock_turns_the_screen_off_and_checks():
    device, runner, slept = make_lock_device([lock_state("Awake", "false"), lock_state("Asleep", "true")])
    assert await device.lock() is True
    assert runner.shell() == ["input keyevent 223"]
    assert slept == [0.5, 0.5]
    # 按了屏幕还亮着
    device, _, slept = make_lock_device([lock_state("Awake", "false")])
    assert await device.lock() is False
    assert len(slept) == 4


def test_battery_is_read_from_dumpsys_and_wda_without_guessing():
    from phone_play.devices import Battery, parse_android_battery, parse_wda_battery

    unplugged = (
        "Current Battery Service state:\n  AC powered: false\n  USB powered: false\n"
        "  status: 3\n  level: 15\n  scale: 100\n"
    )
    assert parse_android_battery(unplugged) == Battery(level=15, charging=False)
    assert parse_android_battery(unplugged.replace("USB powered: false", "USB powered: true")).charging
    assert parse_android_battery("  status: 5\n  level: 100\n  scale: 100\n") == Battery(level=100, charging=True)
    assert parse_android_battery("  level: 30\n  scale: 50\n").level == 60
    # 没有 level 就是不知道，不当成 0%
    assert parse_android_battery("Can't find service: battery") is None
    # 实测 iPhone 14 插着线：{"level": 1, "state": 2}
    assert parse_wda_battery({"level": 1, "state": 2}) == Battery(level=100, charging=True)
    assert parse_wda_battery({"level": 0.42, "state": 1}) == Battery(level=42, charging=False)
    assert parse_wda_battery({"level": -1, "state": 0}) is None
    assert parse_wda_battery(None) is None


@pytest.mark.parametrize(
    "state,expected",
    [((False, None), False), ((True, True), False), ((True, False), True), ((None, None), None), ((True, None), None)],
)
async def test_android_screen_viewable_follows_wakefulness_and_keyguard(state, expected):
    device = AdbDevice(DeviceSettings(serial="emulator-5554"))

    async def lock_state():
        return state

    device._lock_state = lock_state
    assert await device.screen_viewable() is expected
