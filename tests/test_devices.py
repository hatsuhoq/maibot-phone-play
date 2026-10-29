from typing import Dict, List, Sequence, Tuple

import base64
import pytest

from phone_play.devices import (
    AdbDevice,
    DeviceError,
    WdaDevice,
    create_device,
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


def make_device(responses, **settings) -> Tuple[AdbDevice, FakeRunner]:
    runner = FakeRunner(responses)
    return AdbDevice(DeviceSettings(**settings), runner=runner), runner


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
    device, _ = make_device(
        {"connect": (0, b"cannot connect to 127.0.0.1:16384: refused\n", b"")}, serial="127.0.0.1:16384"
    )
    with pytest.raises(DeviceError, match="adb connect"):
        await device.ensure_ready()


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
