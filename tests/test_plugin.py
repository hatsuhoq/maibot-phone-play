from dataclasses import replace
from datetime import datetime
from io import BytesIO
from typing import Any, List
from unittest.mock import AsyncMock

import asyncio
import base64
import json
import re
import pytest

from PIL import Image

from maibot_sdk import DesktopPetButtonState
from maibot_sdk.context import PluginContext, PluginPaths

from phone_play import plugin as plugin_module
from phone_play.actions import Seen
from phone_play.agent import PlayRefused
from phone_play.life import LifeLinkError
from phone_play.plugin import _COMMAND_PATTERN, create_plugin
from phone_play.settings import AllowedApp, DeviceSettings, PhonePlayConfig
from phone_play.store import SessionRecord

from conftest import FakeDevice

PLAN_JSON = json.dumps({"app": "tv.danmaku.bili", "goal": "刷若叶睦的视频", "minutes": 8}, ensure_ascii=False)


def wire_llm(plugin, vision_responses: List[str]) -> None:
    """文本提示返回计划，多模态消息按顺序返回动作。"""
    responses = list(vision_responses)

    async def generate(prompt, **kwargs):
        if isinstance(prompt, str):
            return {"success": True, "response": PLAN_JSON}
        return {"success": True, "response": responses.pop(0)}

    plugin.ctx.llm.generate = generate


async def fast_sleep(seconds: float) -> None:
    await asyncio.sleep(0)


def make_plugin(tmp_path, **overrides: Any):
    plugin = create_plugin()
    plugin._ctx = PluginContext(
        "hohoko-chan.phone-play", paths=PluginPaths(data_dir=tmp_path / "data", runtime_dir=tmp_path / "temp")
    )
    config = PhonePlayConfig()
    config.plugin.admin_user_ids = ["10001"]
    for section, values in overrides.items():
        for key, value in values.items():
            setattr(getattr(config, section), key, value)
    plugin.set_plugin_config(config.model_dump())
    plugin.ctx.send.text = AsyncMock(return_value=True)
    plugin.ctx.send.image = AsyncMock(return_value=True)
    # 测试里没有桌宠：气泡跳过，按钮刷新只记下调用
    plugin.ctx.desktop_pet.status = AsyncMock(return_value={"ready": False, "visible": False})
    plugin.ctx.desktop_pet.refresh_buttons = AsyncMock(return_value={"success": True})
    plugin.ctx.config.get = AsyncMock(side_effect=lambda key, default=None: {"bot.nickname": "小睦"}.get(key, ""))
    return plugin


@pytest.mark.parametrize(
    "text,sub,arg",
    [
        ("/手机", None, None),
        ("/手机截图", "截图", None),
        ("/手机 截图", "截图", None),
        ("/手机 开始 刷B站看猫猫", "开始", "刷B站看猫猫"),
        ("/手机开始", "开始", None),
        ("/手机 停止 ", "停止", None),
        ("/手机 接管", "接管", None),
        ("/手机 交还", "交还", None),
        ("/手机 设备", "设备", None),
        ("/手机 切换 OPPO", "切换", "OPPO"),
        ("/手机 乱说", None, "乱说"),
    ],
)
def test_command_pattern(text, sub, arg):
    match = re.match(_COMMAND_PATTERN, text)
    assert match is not None
    assert (match.group("sub"), match.group("arg")) == (sub, arg)


@pytest.mark.parametrize("text", ["/手机号码是多少", "手机截图", "/手机截图吧"])
def test_command_pattern_ignores_normal_messages(text):
    assert re.match(_COMMAND_PATTERN, text) is None


async def test_components_and_lifecycle(tmp_path):
    plugin = make_plugin(tmp_path)
    names = sorted(component["name"] for component in plugin.get_components())
    assert names == sorted(
        [
            "phone",
            "play_phone",
            "show_phone_screen",
            "remember_for_phone",
            "phone_activity",
            "stop_phone",
            "switch_phone",
            "phone_play_planner_context",
            "phone_play_replyer_context",
            "webui_status",
            "webui_frame",
            "webui_start",
            "webui_stop",
            "webui_pause",
            "webui_resume",
            "webui_takeover",
            "webui_release",
            "webui_diagnostic",
            "webui_clear_diagnostics",
            "webui_memory",
            "webui_memory_delete",
            "webui_memory_clear",
            "webui_switch_device",
        ]
    )
    # 去玩、问近况、叫停一直在她的工具列表里，不用先 tool_search；看屏幕按需搜索
    visibility = {
        component["name"]: component["metadata"].get("visibility")
        for component in plugin.get_components()
        if component["type"] == "TOOL"
    }
    assert visibility == {
        "play_phone": "visible",
        "phone_activity": "visible",
        "stop_phone": "visible",
        "show_phone_screen": None,
        "remember_for_phone": None,
        "switch_phone": None,
    }
    # 宿主只把 description 发给模型（detailed_description 已经不用了），用法都要写在这里
    descriptions = {
        component["name"]: component["metadata"]["description"]
        for component in plugin.get_components()
        if component["type"] == "TOOL"
    }
    assert "玩手机上的游戏" in descriptions["play_phone"]
    assert "没调用就别说自己在玩" in descriptions["play_phone"]
    assert "attach_pic" in descriptions["show_phone_screen"]
    assert "别自己编" in descriptions["phone_activity"]
    await plugin.on_load()
    status = await plugin.webui_status()
    assert status["state"] == "idle" and status["nickname"] == "小睦"
    await plugin.on_config_update("self", {}, "next")
    await plugin.on_config_update("bot", {}, "next")
    assert plugin._bot_nickname == ""
    await plugin.on_unload()
    assert plugin._agent is None


async def command(plugin, text: str, user_id: str = "10001", is_local_operator: bool = False) -> str:
    match = re.match(_COMMAND_PATTERN, text)
    assert match is not None
    plugin.ctx.send.text.reset_mock()
    await plugin.handle_phone_command(
        stream_id="stream-1",
        user_id=user_id,
        is_local_operator=is_local_operator,
        matched_groups={key: value for key, value in match.groupdict().items() if value is not None},
    )
    calls = plugin.ctx.send.text.await_args_list
    return calls[-1].args[0] if calls else ""


async def test_help_status_and_permissions(tmp_path):
    plugin = make_plugin(tmp_path)
    await plugin.on_load()
    try:
        assert "/手机截图" in await command(plugin, "/手机 帮助")
        assert "小睦的手机：空闲" in await command(plugin, "/手机", user_id="anyone")
        assert "不认识的手机命令" in await command(plugin, "/手机 乱说")
        denied = await command(plugin, "/手机 开始", user_id="20002")
        assert "admin_user_ids" in denied and "20002" in denied
        assert "admin_user_ids" in await command(plugin, "/手机截图", user_id="20002")
        assert "现在没有暂停" in await command(plugin, "/手机 继续", user_id="", is_local_operator=True)
    finally:
        await plugin.on_unload()


async def test_screenshot_command_sends_image(tmp_path, monkeypatch):
    device = FakeDevice()
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: device)
    plugin = make_plugin(tmp_path, plugin={"screenshot_for_everyone": True})
    await plugin.on_load()
    try:
        assert await command(plugin, "/手机截图", user_id="20002") == ""
        image_args = plugin.ctx.send.image.await_args
        assert image_args.args[1] == "stream-1"
        assert image_args.args[0].startswith("/9j/")  # JPEG 的 Base64 开头
    finally:
        await plugin.on_unload()


async def test_start_command_tells_plan_and_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: FakeDevice())
    plugin = make_plugin(tmp_path, play={"finish_notice": "off"})
    wire_llm(plugin, [json.dumps({"action": "finish", "summary": "看完了"}, ensure_ascii=False)])
    await plugin.on_load()
    plugin.agent._sleep = fast_sleep
    try:
        reply = await command(plugin, "/手机 开始 刷B站看猫猫")
        assert reply.startswith("好，小睦去玩手机了：打开哔哩哔哩，刷若叶睦的视频（大概 8 分钟）。")
        await asyncio.wait_for(plugin.agent._task, 5)
        assert plugin.agent._store.history[-1].trigger == "命令"

        async def refuse(**kwargs):
            raise PlayRefused("今天已经玩了 8 次手机，达到每日上限", chat_text="今天不能再玩")

        plugin.agent.start = refuse
        # 命令是给管理员看的，用完整原因
        assert await command(plugin, "/手机 开始") == "现在不能玩手机：今天已经玩了 8 次手机，达到每日上限"
    finally:
        await plugin.on_unload()


async def test_tools(tmp_path):
    plugin = make_plugin(tmp_path, play={"allow_chat_trigger": False})
    await plugin.on_load()
    try:
        refused = await plugin.tool_play_phone(goal="看猫")
        assert refused["success"] is False and "不允许" in refused["error"]
        assert (await plugin.tool_phone_activity())["content"] == "现在没在玩手机，最近也没有玩过。"
        assert (await plugin.tool_stop_phone())["content"] == "你现在本来就没在玩手机。"
    finally:
        await plugin.on_unload()


async def test_play_phone_tool_tells_plan_and_comes_back_naturally(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: FakeDevice())
    plugin = make_plugin(tmp_path)
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": True, "queued": True})
    wire_llm(
        plugin,
        [
            json.dumps({"action": "wait", "seconds": 3, "thought": "睦睦好可爱"}, ensure_ascii=False),
            json.dumps({"action": "finish", "summary": "看了两个睦睦的视频"}, ensure_ascii=False),
        ],
    )
    await plugin.on_load()
    plugin.agent._sleep = fast_sleep
    try:
        result = await plugin.tool_play_phone(goal="看睦睦", stream_id="stream-1", user_id="20002")
        # 等她决定好玩什么再返回，她就能说出具体打算
        assert result["content"] == "你拿起了手机，打算打开哔哩哔哩：刷若叶睦的视频（大概玩 8 分钟）。"
        await asyncio.wait_for(plugin.agent._task, 5)
        # 玩完回到发起的聊天，让她自己决定怎么说，不发固定格式通知
        stream_id, intent = plugin.ctx.maisaka.proactive.trigger.await_args.args
        assert stream_id == "stream-1"
        assert "在哔哩哔哩里刷若叶睦的视频" in intent and "看了两个睦睦的视频" in intent
        plugin.ctx.send.text.assert_not_awaited()
    finally:
        await plugin.on_unload()


@pytest.mark.parametrize(
    "device,expected",
    [
        (FakeDevice(ready_error="adb connect 127.0.0.1:16416 失败"), "手机连不上，可能没开机或者没连好"),
        (FakeDevice(installed={"com.tencent.mm"}), "手机上没有你能玩的应用"),
    ],
)
async def test_play_phone_tool_explains_why_it_cannot_play(tmp_path, monkeypatch, device, expected):
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: device)
    plugin = make_plugin(tmp_path)
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": True})
    wire_llm(plugin, [])
    await plugin.on_load()
    try:
        result = await plugin.tool_play_phone(stream_id="stream-1")
        # 工具结果已经告诉她原因，不带 adb 细节，也不会再额外发一条结束通知
        assert result == {"success": False, "error": f"这次玩不了手机：{expected}"}
        plugin.ctx.maisaka.proactive.trigger.assert_not_awaited()
        assert plugin.agent.status()["today"]["sessions"] == 0
    finally:
        await plugin.on_unload()


async def test_play_phone_tool_together_mode(tmp_path, monkeypatch):
    duel = AllowedApp(package="com.example.duel", name="双人小游戏", note="两个人在同一台手机上轮流玩")
    device = FakeDevice(installed={"com.example.duel"})
    device.default_foreground = "com.example.duel"
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: device)
    plugin = make_plugin(tmp_path, safety={"allowed_apps": [duel]})
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": True})

    async def generate(prompt, **kwargs):
        if isinstance(prompt, str):
            plan = {"app": "com.example.duel", "goal": "和对方轮流玩双人小游戏", "minutes": 10}
            return {"success": True, "response": json.dumps(plan, ensure_ascii=False)}
        return {"success": True, "response": '{"action": "finish", "summary": "玩完了，我赢了一局"}'}

    plugin.ctx.llm.generate = generate
    await plugin.on_load()
    plugin.agent._sleep = fast_sleep
    try:
        result = await plugin.tool_play_phone(goal="和对方玩双人小游戏", together=True, stream_id="stream-1")
        assert result["content"].startswith("你拿起了手机，打算打开双人小游戏：和对方轮流玩双人小游戏。")
        assert "对方用自己的手机联机时，你会进房间等对方、一起开局" in result["content"]
        # 聊天这边别自己编进展，问起时去看手机
        assert "不要编造进展" in result["content"] and "phone_activity" in result["content"]
        await asyncio.wait_for(plugin.agent._task, 5)
        _, intent = plugin.ctx.maisaka.proactive.trigger.await_args.args
        assert "这次和对方一起在双人小游戏里和对方轮流玩双人小游戏" in intent and "玩完了，我赢了一局" in intent
    finally:
        await plugin.on_unload()


async def test_play_phone_tool_declines_goal_the_phone_cannot_do(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: FakeDevice())
    plugin = make_plugin(tmp_path)
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": True})

    async def generate(prompt, **kwargs):
        return {"success": True, "response": '{"app": "", "reason": "手机上没有王者荣耀"}'}

    plugin.ctx.llm.generate = generate
    await plugin.on_load()
    try:
        result = await plugin.tool_play_phone(goal="打王者荣耀", stream_id="stream-1")
        assert result["error"] == "这次玩不了手机：手机上做不到：手机上没有王者荣耀（手机上能玩的只有哔哩哔哩、小红书）"
        await asyncio.wait_for(plugin.agent._task, 5)
        plugin.ctx.maisaka.proactive.trigger.assert_not_awaited()
    finally:
        await plugin.on_unload()


async def test_stop_phone_tool_skips_finish_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: FakeDevice())
    plugin = make_plugin(tmp_path)
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": True})
    blocked = asyncio.Event()

    async def generate(prompt, **kwargs):
        if isinstance(prompt, str):
            return {"success": True, "response": PLAN_JSON}
        blocked.set()
        await asyncio.sleep(3600)

    plugin.ctx.llm.generate = generate
    await plugin.on_load()
    plugin.agent._sleep = fast_sleep
    try:
        await plugin.tool_play_phone(stream_id="stream-1")
        await asyncio.wait_for(blocked.wait(), 5)
        result = await plugin.tool_stop_phone()
        assert result["content"].startswith("你放下了手机。")
        # 她正在聊天里接「别玩了」这句话，不需要再主动说一遍
        plugin.ctx.maisaka.proactive.trigger.assert_not_awaited()
    finally:
        await plugin.on_unload()


async def test_show_phone_screen_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: FakeDevice())
    plugin = make_plugin(tmp_path)
    await plugin.on_load()
    try:
        denied = await plugin.tool_show_phone_screen(user_id="20002")
        assert denied["success"] is False and "婉拒" in denied["error"]

        shown = await plugin.tool_show_phone_screen(user_id="10001")
        # 截图只交给她，由她用 attach_pic 附在回复里发出去，插件自己不发，免得同一张图发两遍
        plugin.ctx.send.image.assert_not_awaited()
        (item,) = shown["content_items"]
        assert item["content_type"] == "image" and item["mime_type"] == "image/jpeg"
        assert item["data"].startswith("/9j/")
        assert shown["content"].startswith("这是你手机现在的屏幕截图，对方还看不到；要给对方看，回复时用 attach_pic")
    finally:
        await plugin.on_unload()


@pytest.mark.parametrize("style,expected", [("natural", (1, 0)), ("template", (0, 1)), ("off", (0, 0))])
async def test_finish_notice_styles(tmp_path, style, expected):
    plugin = make_plugin(tmp_path, play={"finish_notice": style})
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": True})
    record = SessionRecord(
        started_at="2026-10-28T17:24:33",
        ended_at="2026-10-28T17:29:52",
        trigger="聊天中决定",
        app="哔哩哔哩",
        goal="刷若叶睦的视频",
        summary="软绵绵的睦睦好可爱",
        steps=11,
        end_reason="被命令叫停",
    )
    await plugin._notify_finished(record, "stream-1")
    assert (plugin.ctx.maisaka.proactive.trigger.await_count, plugin.ctx.send.text.await_count) == expected


async def test_natural_finish_notice_failure_is_raised(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": False, "error": "未找到已存在的聊天流"})
    record = SessionRecord("", "", "聊天中决定", "哔哩哔哩", "看猫猫", "橘猫好圆", 3, "出错：设备离线")
    with pytest.raises(RuntimeError, match="未找到已存在的聊天流"):
        await plugin._notify_finished(record, "stream-1")


async def test_webui_actions_raise_readable_errors(tmp_path, monkeypatch):
    import phone_play.agent as agent_module

    # 连接放在会话里重试；这里把截止时间调短，连不上很快就结束
    monkeypatch.setattr(agent_module, "_START_CONNECT_SECONDS", 5.0)
    # 指向没有服务的端口，不管本机有没有真的在跑 WebDriverAgent
    plugin = make_plugin(tmp_path, device={"backend": "ios_wda", "wda_url": "http://127.0.0.1:9"})
    await plugin.on_load()
    try:
        assert await plugin.webui_start(goal="") == {"started": True}
        await asyncio.wait_for(plugin.agent._task, 10)
        status = await plugin.webui_status()
        assert "连不上 iPhone" in status["last_error"] and status["today"]["sessions"] == 0
        # 无线、有线都提示到，不再只让人查数据线
        assert status["connection"]["state"] == "failed" and "pymobiledevice3" in status["connection"]["hint"]
        with pytest.raises(ValueError, match="无法暂停"):
            await plugin.webui_pause()
        assert (await plugin.webui_stop()) == {"stopped": False}
    finally:
        await plugin.on_unload()


async def test_finish_notice_mentions_her_interactions(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": True})
    record = SessionRecord("", "", "聊天中决定", "哔哩哔哩", "看猫猫", "橘猫好圆", 3, "自己不想玩了")
    record = replace(record, interactions=["点赞 2 次", "评论「好圆」"])
    await plugin._notify_finished(record, "stream-1")
    _, intent = plugin.ctx.maisaka.proactive.trigger.await_args.args
    assert "用自己的账号点赞 2 次、评论「好圆」" in intent


async def test_chat_sees_phone_state_and_passes_requests_while_playing(tmp_path, monkeypatch):
    # 实测玩的时候她在聊天里照着对方的话编进展（「点到了，已经退出房间了」），手机上的她根本不知道；
    # 对方说「不玩了」她也不放下手机
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: FakeDevice())
    plugin = make_plugin(tmp_path)
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": True})
    deciding, gate = asyncio.Event(), asyncio.Event()
    states: List[str] = []
    responses = [
        '{"action": "wait", "seconds": 30, "summary": "看睦睦的视频", "thought": "这个睦睦好可爱"}',
        '{"action": "finish", "summary": "看了睦睦的视频，按对方说的换了一个"}',
    ]

    async def generate(prompt, **kwargs):
        if isinstance(prompt, str):
            return {"success": True, "response": PLAN_JSON}
        states.append(prompt[1]["content"][0]["text"])
        if len(states) == 1:
            deciding.set()
            await gate.wait()
        return {"success": True, "response": responses.pop(0)}

    plugin.ctx.llm.generate = generate
    await plugin.on_load()
    try:
        await plugin.tool_play_phone(goal="看睦睦", stream_id="stream-1")
        await asyncio.wait_for(deciding.wait(), 5)

        # 发起的聊天每一轮都带上手机上的实际情况，追加在最后，不动前面的消息
        items = [{"item_type": "UserMessageItem", "meta": {"item_id": "m1"}, "parts": []}]
        hooked = await plugin.inject_planner_context(items=items, session_id="stream-1", item_schema_version=1)
        new_items = hooked["modified_kwargs"]["items"]
        assert new_items[0] == items[0] and hooked["modified_kwargs"]["item_schema_version"] == 1
        note = new_items[-1]
        assert note["item_type"] == "UserMessageItem" and note["meta"]["logical_turn_id"] is None
        text = note["parts"][0]["text"]
        assert text.startswith("【手机上的实际情况】正在用手机：哔哩哔哩") and "stop_phone" in text
        # 列的是这几步想做什么，不代表做成了
        assert "成没成要看后面几步" in text
        # 写回复时也带上；别的聊天不带
        replyer = await plugin.inject_replyer_context(extra_prompt="", session_id="stream-1")
        assert replyer["modified_kwargs"]["extra_prompt"].startswith("【手机上的实际情况】")
        assert await plugin.inject_planner_context(items=items, session_id="stream-2") == {"action": "continue"}

        # 玩的时候再调用 play_phone：不重新开始，把要求交给手机上的她
        result = await plugin.tool_play_phone(goal="换个视频", stream_id="stream-1")
        assert result["content"].startswith("已经告诉手机上的你：换个视频。") and "phone_activity" in result["content"]
        gate.set()
        await asyncio.wait_for(plugin.agent._task, 5)
        # 本来要看 30 秒，有了新要求马上醒来，下一步先照着做
        assert "对方刚在聊天里让你：换个视频。这比原来的打算优先" in states[1]
        assert "第2步前，对方在聊天里让你：换个视频" in states[1]

        # 玩完后的一段时间里，提醒她现在没在玩
        after = await plugin.inject_planner_context(items=items, session_id="stream-1")
        assert "现在没在玩手机" in after["modified_kwargs"]["items"][-1]["parts"][0]["text"]
    finally:
        await plugin.on_unload()


async def test_takeover_and_release_from_commands_and_webui(tmp_path):
    plugin = make_plugin(tmp_path)
    await plugin.on_load()
    try:
        reply = await plugin._run_command("接管", "", "stream-1", "10001", False)
        assert reply.startswith("好，手机交给你") and plugin.agent.controller == "user"
        assert await plugin._run_command("接管", "", "stream-1", "10001", False) == "你已经在操作手机了"
        status = await plugin.webui_status()
        assert status["controller"] == "user" and status["controller_label"] == "你在操作"
        with pytest.raises(ValueError, match="已经在操作"):
            await plugin.webui_takeover()
        assert await plugin.webui_release() == {"controller": "bot"}
        with pytest.raises(ValueError, match="本来就在她手里"):
            await plugin.webui_release()
        assert await plugin.webui_takeover() == {"controller": "user"}
        assert (await plugin._run_command("交还", "", "stream-1", "10001", False)).startswith("已交还给")
        # 不是管理员不能接管
        assert "只有管理员" in await plugin._run_command("接管", "", "stream-1", "20002", False)
    finally:
        await plugin.on_unload()


async def test_failed_step_diagnostic_can_be_viewed_and_cleared(tmp_path, monkeypatch):
    # 点之前目标没了（StaleTarget）：留一份诊断，WebUI 里能看当时的画面、计划动作和失败原因
    from conftest import scene_png
    from phone_play.ui_tree import UIElement

    popup = UIElement(index=1, label="不再提醒", kind="Button", clickable=True, bounds=(40, 1050, 500, 1130))
    plus = UIElement(index=1, label="投稿", kind="ImageView", clickable=True, bounds=(230, 1060, 310, 1140))

    class Phone(FakeDevice):
        def __init__(self) -> None:
            super().__init__()
            self.shots = 0

        async def screenshot(self) -> bytes:
            self.shots += 1
            if self.shots == 1:
                self.png, self.elements = scene_png(((40, 1050, 500, 1130), "#ffffff")), [popup]
            else:
                self.png, self.elements = scene_png(((230, 1060, 310, 1140), "#ff6699")), [plus]
            return await super().screenshot()

    monkeypatch.setattr(plugin_module, "create_device", lambda settings: Phone())
    plugin = make_plugin(tmp_path)
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": True})
    wire_llm(plugin, ['{"action": "tap", "element": 1, "summary": "关掉弹窗"}', '{"action": "finish"}'])
    await plugin.on_load()
    plugin.agent._sleep = fast_sleep
    try:
        await plugin.webui_start(goal="")
        await asyncio.wait_for(plugin.agent._task, 5)
        logs = (await plugin.webui_status())["logs"]
        failed = next(log for log in logs if log["diagnostic"])
        detail = await plugin.webui_diagnostic(id=failed["diagnostic"])
        assert detail["image"].startswith("data:image/jpeg;base64,")
        entry = detail["entry"]
        assert entry["action"] == "点击 [1]" and "不再提醒" in entry["target"]
        assert entry["result"].startswith("没点：「不再提醒」已经不在原来的位置了")
        assert await plugin.webui_clear_diagnostics() == {"cleared": 1}
        with pytest.raises(ValueError, match="过期或被清理"):
            await plugin.webui_diagnostic(id=failed["diagnostic"])
    finally:
        await plugin.on_unload()


async def test_host_token_usage_reaches_the_session_stats(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: FakeDevice())
    plugin = make_plugin(tmp_path)
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": True})

    async def generate(prompt, **kwargs):
        if isinstance(prompt, str):
            # 宿主拿不到用量时 token 字段是 0：按未提供处理
            return {"success": True, "response": PLAN_JSON, "total_tokens": 0}
        return {
            "success": True,
            "response": '{"action": "finish", "summary": "看了两个睦睦的视频，挺开心"}',
            "prompt_tokens": 3500,
            "completion_tokens": 80,
            "total_tokens": 3580,
        }

    plugin.ctx.llm.generate = generate
    await plugin.on_load()
    plugin.agent._sleep = fast_sleep
    try:
        await plugin.webui_start(goal="")
        await asyncio.wait_for(plugin.agent._task, 5)
        stats = plugin.agent.status()["history"][0]["stats"]
        assert stats["tokens"] == {"total": 3580, "prompt": 3500, "completion": 80, "missing": 1}
        assert (await plugin.webui_status())["model_calls"]["today"] == 2
    finally:
        await plugin.on_unload()


async def test_memory_can_be_added_from_chat_viewed_and_deleted(tmp_path):
    plugin = make_plugin(tmp_path)
    await plugin.on_load()
    try:
        result = await plugin.tool_remember_for_phone(note="  对方喜欢看 种田小说 ")
        assert result["content"].startswith("记住了：对方喜欢看 种田小说")
        items = (await plugin.webui_memory())["items"]
        assert [(item["kind"], item["source"], item["source_label"]) for item in items] == [
            ("interest", "user", "对方说过")
        ]
        assert await plugin.webui_memory_delete(id=items[0]["id"]) == {"deleted": items[0]["id"]}
        with pytest.raises(ValueError, match="已经不在"):
            await plugin.webui_memory_delete(id=items[0]["id"])
        await plugin.tool_remember_for_phone(note="下次接着看田园小当家")
        assert await plugin.webui_memory_clear() == {"cleared": 1}
        assert (await plugin.tool_remember_for_phone(note=" "))["success"] is False
    finally:
        await plugin.on_unload()


@pytest.mark.parametrize(
    "end_reason,expected",
    [
        ("卡住了：最近 6 步一直在两个画面之间来回切换", "卡住了"),
        ("这次的模型调用预算用完了（120 次）", "差不多该放下"),
        ("今天的模型调用预算用完了（1000 次）", "差不多该放下"),
        ("暂停或接管太久，这次先结束", "停下来太久"),
        ("被停止", "被叫停了"),
    ],
)
def test_new_end_reasons_get_a_natural_explanation(end_reason, expected):
    assert expected in plugin_module._finish_reason_text(end_reason)


async def test_watched_videos_can_be_cleared_without_forgetting_the_rest(tmp_path):
    plugin = make_plugin(tmp_path)
    await plugin.on_load()
    try:
        await plugin.tool_remember_for_phone(note="对方喜欢看种田小说")
        plugin._memory.remember_watched(Seen("video", "迷途之子!!!!!"), "哔哩哔哩", "long", datetime.now())
        items = (await plugin.webui_memory())["items"]
        assert sorted((item["kind"], item["form"]) for item in items) == [("interest", ""), ("watched", "long")]
        assert await plugin.webui_memory_clear(kind="watched") == {"cleared": 1}
        assert [item["kind"] for item in (await plugin.webui_memory())["items"]] == ["interest"]
        with pytest.raises(ValueError, match="不认识的记忆类型"):
            await plugin.webui_memory_clear(kind="everything")
    finally:
        await plugin.on_unload()


def two_device_plugin(tmp_path):
    return make_plugin(
        tmp_path,
        device={"name": "iPhone", "backend": "ios_wda"},
        multi_device={"devices": [DeviceSettings(name="OPPO", serial="192.168.1.23")]},
    )


async def test_devices_can_be_listed_and_switched_by_admins(tmp_path):
    plugin = two_device_plugin(tmp_path)
    await plugin.on_load()
    try:
        listed = await command(plugin, "/手机 设备", user_id="anyone")
        assert "· iPhone（现在用的）：iPhone · http://127.0.0.1:8100" in listed
        assert "· OPPO：安卓 · 192.168.1.23" in listed
        assert "admin_user_ids" in await command(plugin, "/手机 切换 OPPO", user_id="20002")
        assert await command(plugin, "/手机 切换 华为") == "没换：没有叫「华为」的设备，能用的有：iPhone、OPPO"
        assert "要换哪台" in await command(plugin, "/手机 切换")
        # 聊天里常说「安卓」：只有一台安卓设备时就是它
        assert await command(plugin, "/手机 切换 安卓") == "好，小睦之后用OPPO玩手机"
        assert await command(plugin, "/手机 切换 oppo") == "现在用的就是OPPO"
        assert "设备：OPPO（共 2 台" in await command(plugin, "/手机")
    finally:
        await plugin.on_unload()
    # 记住了：插件重新加载后还用 OPPO
    plugin = two_device_plugin(tmp_path)
    await plugin.on_load()
    try:
        assert (await plugin.webui_status())["device_name"] == "OPPO"
    finally:
        await plugin.on_unload()


async def test_switch_phone_tool_is_for_admins_only(tmp_path):
    plugin = two_device_plugin(tmp_path)
    await plugin.on_load()
    try:
        listed = await plugin.tool_switch_phone(device="", user_id="20002")
        assert listed["content"] == "你的手机有：iPhone（iPhone，现在用的）、OPPO（安卓）"
        refused = await plugin.tool_switch_phone(device="OPPO", user_id="20002")
        assert refused["success"] is False and "只有管理员" in refused["error"]
        assert plugin.agent.status()["device_name"] == "iPhone"
        switched = await plugin.tool_switch_phone(device="OPPO", user_id="10001")
        assert switched["success"] and "之后玩手机用OPPO" in switched["content"]
        assert "不用换" in (await plugin.tool_switch_phone(device="OPPO", user_id="10001"))["content"]
        unknown = await plugin.tool_switch_phone(device="小米", user_id="10001")
        assert unknown["success"] is False and "能用的有：iPhone、OPPO" in unknown["error"]
    finally:
        await plugin.on_unload()


async def test_webui_switches_devices(tmp_path):
    plugin = two_device_plugin(tmp_path)
    await plugin.on_load()
    try:
        assert await plugin.webui_switch_device(name="OPPO") == {"device": "OPPO", "changed": True}
        status = await plugin.webui_status()
        assert status["device_name"] == "OPPO"
        assert [(item["name"], item["current"]) for item in status["devices"]] == [("iPhone", False), ("OPPO", True)]
        with pytest.raises(ValueError, match="没有叫「小米」的设备"):
            await plugin.webui_switch_device(name="小米")
    finally:
        await plugin.on_unload()


# 麦生活 get_schedule_snapshot 经 SDK 返回的样子：宿主那一层已经拆掉，剩它自己的信封
WORKING = {
    "success": True,
    "error": None,
    "data": {
        "timezone": "Asia/Shanghai",
        "as_of": "2026-11-06T13:47:00+08:00",
        "revision": "abc",
        "schedule_available": True,
        "current": {
            "id": "n7",
            "kind": "work",
            "name": "补齐项目表格里的进度记录",
            "description": "",
            "start_at": "2026-11-06T13:25:00+08:00",
            "end_at": "2026-11-06T17:30:00+08:00",
            "location": "公司工位",
            "scene": "",
        },
        "next_activities": [],
    },
}


async def test_auto_play_asks_mai_life_for_the_schedule(tmp_path):
    plugin = make_plugin(tmp_path, auto_play={"enabled": True})
    calls = []

    async def api_call(api_name, *, version="", **kwargs):
        calls.append((api_name, version, kwargs))
        return WORKING

    plugin.ctx.api.call = api_call
    await plugin.on_load()
    try:
        status = await plugin.webui_status()
        assert calls == [("maibot-community.mai-life.get_schedule_snapshot", "1", {"future_limit": 0})]
        assert (status["life"]["allowed"], status["life"]["slack"]) == (True, True)
        assert status["life"]["text"] == (
            "按麦生活的日程，现在是「补齐项目表格里的进度记录」（工作 · 公司工位），"
            "偶尔摸鱼玩一会儿（每次考虑时 10% 的概率，最多 5 分钟）"
        )
        # WebUI 每隔几秒要一次状态：一分钟内不再问
        await plugin.webui_status()
        assert len(calls) == 1
        assert "生活联动：按麦生活的日程，现在是「补齐项目表格里的进度记录」" in await command(plugin, "/手机")
    finally:
        await plugin.on_unload()


async def test_without_mai_life_auto_play_uses_active_hours(tmp_path):
    plugin = make_plugin(tmp_path, auto_play={"enabled": True})
    plugin.ctx.api.call = AsyncMock(
        return_value={"success": False, "error": "未找到 API 提供方插件: maibot-community.mai-life"}
    )
    await plugin.on_load()
    try:
        status = await plugin.webui_status()
        assert status["life"] == {
            "checked_at": status["life"]["checked_at"],
            "allowed": None,
            "slack": False,
            "text": "没连上麦生活：未找到 API 提供方插件: maibot-community.mai-life，按 active_hours（10:00-23:00）",
        }
        # 宿主 RPC 本身出错：带着原因退回 active_hours，不让状态接口跟着报错
        plugin.ctx.api.call = AsyncMock(side_effect=TimeoutError("等宿主回应超时"))
        await plugin.agent.refresh_life()
        assert (await plugin.webui_status())["life"]["text"] == (
            "查麦生活的日程失败：等宿主回应超时，按 active_hours（10:00-23:00）"
        )
    finally:
        await plugin.on_unload()


async def test_schedule_is_not_asked_while_auto_play_is_off(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.ctx.api.call = AsyncMock(return_value=WORKING)
    await plugin.on_load()
    try:
        assert (await plugin.webui_status())["life"] is None
        assert "生活联动" not in await command(plugin, "/手机")
        plugin.ctx.api.call.assert_not_awaited()
    finally:
        await plugin.on_unload()


async def test_finished_session_is_handed_to_mai_life_for_sharing(tmp_path):
    plugin = make_plugin(tmp_path, life_link={"share_weight": 0.6})
    plugin.ctx.api.call = AsyncMock(return_value={"success": True, "opportunity_id": "f00d"})
    record = SessionRecord(
        started_at="2026-11-06T19:20:00",
        ended_at="2026-11-06T19:30:00",
        trigger="自己想玩",
        app="抖音",
        goal="刷几个短视频",
        summary="这只布偶叫得好认真",
        steps=13,
        end_reason="玩够 10 分钟了",
    )
    assert await plugin._share_with_life(record) == "f00d"
    plugin.ctx.api.call.assert_awaited_once_with(
        "maibot-community.mai-life.create_proactive_opportunity",
        version="1",
        topic="刚才自己刷抖音看到的：这只布偶叫得好认真",
        motive="空闲时自己拿起手机刷到的，觉得有意思可以当见闻随口聊聊；不想说也可以不说",
        weight=0.6,
        expires_minutes=120,
    )
    # 宿主 RPC 出错：带着原因，交给 agent 记进操作记录
    plugin.ctx.api.call = AsyncMock(side_effect=TimeoutError("等宿主回应超时"))
    with pytest.raises(LifeLinkError, match="交给麦生活失败：等宿主回应超时"):
        await plugin._share_with_life(record)


def panel_actions(panel) -> List[str]:
    return [action["id"] for action in panel["actions"]]


async def test_desktop_pet_button_and_panel_follow_her_state(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: FakeDevice())
    plugin = make_plugin(tmp_path)
    looking = asyncio.Event()

    async def generate(prompt, **kwargs):
        if isinstance(prompt, str):
            return {"success": True, "response": PLAN_JSON}
        # 看屏幕的那一步一直不回来，好在「正在玩」的时候看面板
        looking.set()
        await asyncio.sleep(3600)

    plugin.ctx.llm.generate = generate
    await plugin.on_load()
    try:
        assert await plugin.get_desktop_pet_button_states() == {
            "phone": DesktopPetButtonState(visible=True, text="空闲 · 今天玩了 0 次")
        }
        panel = await plugin.on_desktop_pet_button_click("phone")
        assert panel["title"] == "小睦的手机"
        # 面板窄、一行只放得下十几个字：空闲时只有一行，还没玩过就不提上一次
        assert panel["text"] == "空闲 · 今天玩了 0 次"
        assert panel_actions(panel) == ["start", "takeover", "refresh"]
        # 假手机的截图整张放进 480×640 的透明画布
        raw = base64.b64decode(panel["image"], validate=True)
        assert raw.startswith(b"\x89PNG\r\n\x1a\n") and Image.open(BytesIO(raw)).size == (480, 640)

        panel = await plugin.on_desktop_pet_panel_action("phone", "start")
        assert panel["text"].startswith("她拿起了手机，正在想玩什么")
        await asyncio.wait_for(looking.wait(), 5)
        # 插件用真实时钟，第一步前按步间隔等了几秒，已玩的分钟数不固定
        assert re.fullmatch(
            r"正在玩：哔哩哔哩 [\d.]+/8 分钟", (await plugin.get_desktop_pet_button_states())["phone"].text
        )
        panel = await plugin.get_desktop_pet_panel("phone")
        playing, goal = panel["text"].split("\n")
        assert re.fullmatch(r"正在玩：哔哩哔哩 [\d.]+/8 分钟", playing) and goal == "想做：刷若叶睦的视频"
        assert panel_actions(panel) == ["pause", "stop", "takeover", "refresh"]

        panel = await plugin.on_desktop_pet_panel_action("phone", "pause")
        assert panel["text"].startswith("暂停了") and panel_actions(panel)[0] == "resume"
        panel = await plugin.on_desktop_pet_panel_action("phone", "takeover")
        assert panel_actions(panel) == ["release", "refresh"]
        assert (await plugin.get_desktop_pet_button_states())["phone"].text == "你在操作手机"
        await plugin.on_desktop_pet_panel_action("phone", "release")
        await plugin.on_desktop_pet_panel_action("phone", "resume")
        panel = await plugin.on_desktop_pet_panel_action("phone", "stop")
        assert panel["text"].startswith("她正在放下手机")
        # 停止在后台进行；会话任务是被取消的，用 wait 等它结束
        await asyncio.wait({plugin.agent._task}, timeout=5)
        record = plugin.agent._store.history[-1]
        assert (record.trigger, record.end_reason) == ("桌宠", "在桌宠上停止")
        # 放下之后：空闲一行，上一次玩了什么一行，她的感想一行
        idle, last, *thought = (await plugin.get_desktop_pet_panel("phone"))["text"].split("\n")
        assert idle == "空闲 · 今天玩了 1 次"
        assert re.fullmatch(r"上次 \d\d:\d\d · 哔哩哔哩 · 1 分钟", last) and len(thought) <= 1
        with pytest.raises(ValueError, match="没有这个操作"):
            await plugin.on_desktop_pet_panel_action("phone", "explode")
        with pytest.raises(ValueError, match="没有这个桌宠按钮"):
            await plugin.get_desktop_pet_panel("weather")
    finally:
        await plugin.on_unload()


def pet_status(**changes: Any) -> dict:
    status = {
        "state": "idle",
        "state_label": "空闲",
        "controller": "bot",
        "devices": [{"name": "iPhone"}, {"name": "OPPO"}],
        "device_name": "iPhone",
        "app": "",
        "goal": "",
        "thought": "",
        "elapsed_minutes": 0,
        "max_minutes": 15,
        "today": {"sessions": 2},
        "history": [
            {
                "started_at": "2026-11-09T19:05:00",
                "ended_at": "2026-11-09T19:17:10",
                "app": "哔哩哔哩",
                # 实际记录里的感想：几句用「；」连着，每句还带句号
                "summary": "木吉他的声音好舒服，再慢慢听一会儿。；已经接着播到《音一会》了，再安静听一会儿。",
            }
        ],
    }
    status.update(changes)
    return status


def test_desktop_pet_panel_text_stays_short():
    assert plugin_module._pet_panel_lines(pet_status()) == [
        "空闲 · iPhone · 今天玩了 2 次",
        "上次 11-09 19:05 · 哔哩哔哩 · 12 分钟",
        "💭 木吉他的声音好舒服，再慢慢听一会儿",
    ]
    playing = pet_status(
        state="playing",
        state_label="正在玩",
        app="哔哩哔哩",
        goal="搜 BanG Dream! MyGO!!!!! 安静的吉他弹唱或乐队现场，避开最近看过的《春日影》，挑一段慢慢看",
        thought="这首前奏好温柔。",
        elapsed_minutes=3.2,
        devices=[{"name": "iPhone"}],
    )
    assert plugin_module._pet_panel_lines(playing) == [
        "正在玩：哔哩哔哩 3.2/15 分钟",
        "想做：搜 BanG Dream! MyGO!!!!! 安…",
        "💭 这首前奏好温柔",
    ]
    assert plugin_module._pet_panel_lines(pet_status(controller="user", devices=[{"name": "iPhone"}])) == [
        "你在操作手机，用完点「交还控制」"
    ]


async def test_desktop_pet_button_can_be_hidden(tmp_path):
    plugin = make_plugin(tmp_path, desktop_pet={"button": False})
    await plugin.on_load()
    try:
        assert await plugin.get_desktop_pet_button_states() == {"phone": DesktopPetButtonState(visible=False, text="")}
    finally:
        await plugin.on_unload()


@pytest.mark.parametrize(
    "bubbles,pet_status,expected",
    [
        (
            True,
            {"ready": True, "visible": True},
            ["拿起手机：打开哔哩哔哩，刷若叶睦的视频", "放下手机：看了两只小猫，软乎乎的"],
        ),
        # 桌宠没开、还没就绪：跳过，不报错
        (True, {"ready": False, "visible": False}, []),
        (False, {"ready": True, "visible": True}, []),
    ],
)
async def test_desktop_pet_bubbles(tmp_path, monkeypatch, bubbles, pet_status, expected):
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: FakeDevice())
    plugin = make_plugin(tmp_path, play={"finish_notice": "off"}, desktop_pet={"bubbles": bubbles})
    wire_llm(plugin, [json.dumps({"action": "finish", "summary": "看了两只小猫，软乎乎的"}, ensure_ascii=False)])
    plugin.ctx.desktop_pet.status = AsyncMock(return_value=pet_status)
    plugin.ctx.desktop_pet.say = AsyncMock(return_value={"success": True})
    await plugin.on_load()
    plugin.agent._sleep = fast_sleep
    try:
        await command(plugin, "/手机 开始")
        await asyncio.wait_for(plugin.agent._task, 5)
        while plugin.agent._pet_tasks:
            await asyncio.wait(set(plugin.agent._pet_tasks))
        assert [call.args[0] for call in plugin.ctx.desktop_pet.say.await_args_list] == expected
    finally:
        await plugin.on_unload()


@pytest.mark.parametrize("button,refreshes", [(True, 3), (False, 0)])
async def test_desktop_pet_button_refreshes_as_she_plays(tmp_path, monkeypatch, button, refreshes):
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: FakeDevice())
    plugin = make_plugin(tmp_path, play={"finish_notice": "off"}, desktop_pet={"button": button})
    wire_llm(plugin, [json.dumps({"action": "finish", "summary": "看了两只小猫"}, ensure_ascii=False)])
    await plugin.on_load()
    plugin.agent._sleep = fast_sleep
    try:
        await command(plugin, "/手机 开始")
        await asyncio.wait_for(plugin.agent._task, 5)
        while plugin.agent._pet_tasks:
            await asyncio.wait(set(plugin.agent._pet_tasks))
        # 拿起手机、打开应用、放下手机各刷新一次；按钮关掉了就不去打扰宿主
        assert plugin.ctx.desktop_pet.refresh_buttons.await_count == refreshes
    finally:
        await plugin.on_unload()
