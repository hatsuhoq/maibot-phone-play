from dataclasses import replace
from typing import Any, List
from unittest.mock import AsyncMock

import asyncio
import json
import re
import pytest

from maibot_sdk.context import PluginContext, PluginPaths

from phone_play import plugin as plugin_module
from phone_play.agent import PlayRefused
from phone_play.plugin import _COMMAND_PATTERN, create_plugin
from phone_play.settings import AllowedApp, PhonePlayConfig
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
            "phone_activity",
            "stop_phone",
            "webui_status",
            "webui_frame",
            "webui_start",
            "webui_stop",
            "webui_pause",
            "webui_resume",
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
    }
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
        assert (await plugin.tool_phone_activity())["content"] == "最近没有玩过手机。"
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
        assert result["content"].startswith(
            "你拿起了手机，打算打开双人小游戏：和对方轮流玩双人小游戏。对方会在这台手机上"
        )
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


async def test_webui_actions_raise_readable_errors(tmp_path):
    # 指向没有服务的端口，不管本机有没有真的在跑 WebDriverAgent
    plugin = make_plugin(tmp_path, device={"backend": "ios_wda", "wda_url": "http://127.0.0.1:9"})
    await plugin.on_load()
    try:
        with pytest.raises(ValueError, match="连不上 iPhone"):
            await plugin.webui_start(goal="")
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
