from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import asyncio
import json
import logging
import pytest
import threading
import time

from phone_play.agent import (
    AgentError,
    LifeSource,
    ModelReply,
    PhoneAgent,
    PlayRefused,
    Session,
    SwitchRefused,
    in_active_hours,
    match_device,
)
from phone_play.devices import DeviceConfigError, DeviceError, DeviceNeedsUserError, Foreground
from phone_play.life import LifeActivity, LifeLinkError
from phone_play.prompts import Persona
from phone_play.settings import AllowedApp, DeviceSettings, PhonePlayConfig
from phone_play.actions import Seen
from phone_play.memory import MemoryStore
from phone_play.store import SessionRecord, UsageStore
from phone_play.ui_tree import UIElement

from conftest import BILI_DETAIL, BILI_PLAYER, BILI_SCREEN, BILI_SEARCH, FakeDevice, make_png, scene_png

PLAN = json.dumps({"app": "tv.danmaku.bili", "goal": "看猫猫视频", "minutes": 5}, ensure_ascii=False)


def action(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


class Harness:
    """把假设备、脚本化的模型回复和使用记录组装成 PhoneAgent。"""

    def __init__(
        self,
        tmp_path,
        device: FakeDevice,
        responses: Sequence[str],
        *,
        config: Optional[PhonePlayConfig] = None,
        plan: Union[str, Sequence[str]] = PLAN,
        life: Optional[LifeSource] = None,
        share: bool = False,
        announce: bool = False,
        refresh: bool = False,
    ) -> None:
        self.config = config or PhonePlayConfig()
        self.device = device
        self.responses = list(responses)
        self.vision_messages: List[List[Dict[str, Any]]] = []
        self.plan_prompts: List[str] = []
        self.notified: List[tuple] = []
        # announce=True 时接上假的桌宠气泡：记下说了什么，announce_error 不为空时抛出
        self.announced: List[str] = []
        self.events: List[str] = []
        self.announce_error: Optional[Exception] = None
        # refresh=True 时接上假的「刷新桌宠按钮」：记下刷新了几次
        self.refreshes = 0
        # share=True 时接上假的「交给麦生活分享」：记下交过去的记录，share_error 不为空时抛出
        self.shared: List[SessionRecord] = []
        self.share_error: Optional[LifeLinkError] = None
        # 给了多个计划时依次返回，最后一个一直沿用
        self.plans = [plan] if isinstance(plan, str) else list(plan)
        self.store = UsageStore(tmp_path / "usage.json")
        # 假时钟：等待只推进时间，不真的睡
        self.seconds = 0.0
        self.agent = PhoneAgent(
            config=lambda: self.config,
            device_factory=lambda settings: self.device,
            vision=self.vision,
            planner=self.planner,
            persona=self.persona,
            store=self.store,
            notifier=self.notifier,
            logger=logging.getLogger("phone_play_test"),
            clock=lambda: self.seconds,
            sleep=self.fake_sleep,
            life=life,
            life_share=self.share if share else None,
            announcer=self.announce if announce else None,
            refresher=self.refresh if refresh else None,
        )

    async def announce(self, text: str, event: str = "") -> None:
        if self.announce_error is not None:
            raise self.announce_error
        if text:
            self.announced.append(text)
        if event:
            self.events.append(event)

    async def refresh(self) -> None:
        self.refreshes += 1

    async def settle(self) -> None:
        """等后台的气泡说完、按钮刷新完。"""
        while self.agent._pet_tasks:
            await asyncio.wait(set(self.agent._pet_tasks))

    async def share(self, record: SessionRecord) -> str:
        if self.share_error is not None:
            raise self.share_error
        self.shared.append(record)
        return f"op{len(self.shared)}"

    async def vision(self, messages: List[Dict[str, Any]]) -> str:
        self.vision_messages.append(messages)
        return self.responses.pop(0)

    async def planner(self, prompt: str) -> str:
        self.plan_prompts.append(prompt)
        return self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]

    async def persona(self) -> Persona:
        return Persona(nickname="小睦", personality="安静的猫娘")

    async def notifier(self, record: SessionRecord, stream_id: str) -> None:
        self.notified.append((record, stream_id))

    async def fake_sleep(self, seconds: float) -> None:
        self.seconds += seconds
        await asyncio.sleep(0)

    async def run(self, **start_args: Any) -> SessionRecord:
        await self.agent.start(**{"trigger": "command", **start_args})
        await self.wait_done()
        return self.store.history[-1]

    async def wait_done(self) -> None:
        task = self.agent._task
        assert task is not None
        await asyncio.wait_for(task, 5)

    def state_texts(self) -> List[str]:
        """每一步发给视觉模型的文字状态。"""
        return [messages[1]["content"][0]["text"] for messages in self.vision_messages]

    def assert_not_counted(self) -> None:
        """没打开过应用：不算一次、不记录、不通知。"""
        assert self.agent.status()["today"]["sessions"] == 0
        assert self.store.history == []
        assert self.notified == []


async def test_full_session_plans_launches_acts_and_records(tmp_path):
    elements = [UIElement(index=1, label="橘猫视频", kind="LinearLayout", clickable=True, bounds=(0, 100, 540, 300))]
    harness = Harness(
        tmp_path,
        FakeDevice(elements=elements),
        [
            action(thought="这个橘猫好圆", summary="点开猫猫视频", action="tap", element=1),
            action(thought="看一会儿", summary="看视频", action="wait", seconds=5),
            action(thought="下一个", summary="刷下一个", action="swipe", direction="up"),
            action(thought="看够啦", summary="看了三个猫猫视频", action="finish"),
        ],
    )
    record = await harness.run(hint="看猫", origin_stream="stream-1")

    # 先关掉应用再打开（从首页开始），玩完回桌面、锁屏
    assert harness.device.actions() == [
        ("stop", "tv.danmaku.bili"),
        ("launch", "tv.danmaku.bili"),
        ("tap", 270, 200),
        ("swipe", 270, 600, 270, 120),
        ("key", "home"),
        ("lock",),
    ]
    assert record.app == "哔哩哔哩"
    assert record.goal == "看猫猫视频"
    # 她写的总结太短，补上停下来看时和结束时的想法
    assert record.summary == "看了三个猫猫视频；看一会儿；看够啦"
    assert record.end_reason == "自己不想玩了"
    assert record.steps == 4
    assert harness.notified == [(record, "stream-1")]
    assert harness.agent.state == "idle"
    assert "这次想做的事：看猫" in harness.plan_prompts[0]

    # 决策消息带人设、截图和元素列表
    system, user = harness.vision_messages[0]
    assert "你是小睦" in system["content"]
    assert user["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "[1] LinearLayout 橘猫视频（可点）" in user["content"][0]["text"]
    # 第二步的历史里能看到第一步的结果，但不带截图像素坐标（实测模型会照抄成超出 0~1000 的 point）
    second = harness.vision_messages[1][1]["content"][0]["text"]
    assert "第1步：点开猫猫视频（点击 [1]）→ 已点击\n" in second
    assert "(270, 200)" not in second
    fourth = harness.vision_messages[3][1]["content"][0]["text"]
    assert "（上滑）→ 已滑动" in fourth

    saved = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert saved["sessions"] == 1 and saved["steps"] == 4


async def test_leaving_allowed_apps_goes_home_without_asking_model(tmp_path):
    harness = Harness(
        tmp_path,
        FakeDevice(foregrounds=["com.android.settings"]),
        [action(summary="结束", action="finish")],
    )
    record = await harness.run()
    # 一次是安全检查回桌面，一次是玩完回桌面
    assert harness.device.actions().count(("key", "home")) == 2
    assert len(harness.vision_messages) == 1
    assert record.steps == 2
    assert any("不在允许范围内" in log["result"] for log in harness.agent.status()["logs"])


async def test_fresh_start_and_go_home_can_be_disabled(tmp_path):
    config = PhonePlayConfig()
    config.play.fresh_start = False
    config.play.go_home_after = False
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=config)
    await harness.run()
    # 不回桌面也照常锁屏：锁屏不会关掉应用，解开后还在原来的画面
    assert harness.device.actions() == [("launch", "tv.danmaku.bili"), ("lock",)]


async def test_goal_is_repeated_next_to_screenshot(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")])
    await harness.run()
    state_text = harness.vision_messages[0][1]["content"][0]["text"]
    assert state_text.startswith("这次想做的事：看猫猫视频")
    assert "不要被眼前的画面带跑" in harness.vision_messages[0][0]["content"]


async def test_long_videos_are_not_swiped_like_short_videos(tmp_path):
    # 实测她点开 B 站长视频后还一直上滑「刷下一个」，其实只是在翻评论
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")])
    await harness.run()
    system_text = harness.vision_messages[0][0]["content"]
    assert "上滑只会翻简介和评论，不会换视频" in system_text
    assert "返回到视频列表" in system_text
    # 滑动说明和示例都不再暗示上滑就是下一个视频
    assert "看下一条、往下翻" not in system_text
    assert '"summary":"刷下一个视频"' not in system_text
    # 没开互动时不能点赞，也不教她怎么点赞（之前写着「除了点赞不做其它互动」）
    assert "不要点赞" in system_text and "同一条视频只点一次" not in system_text
    # 实测 B 站弹出求好评的框时，她连着四步去点弹窗后面的搜索框
    assert "后面的东西都点不到" in system_text


async def test_launching_unlisted_app_is_rejected(tmp_path):
    harness = Harness(
        tmp_path,
        FakeDevice(installed={"tv.danmaku.bili", "com.tencent.mm"}),
        [action(summary="打开微信", action="launch", app="com.tencent.mm"), action(action="finish", summary="不玩了")],
    )
    await harness.run()
    assert ("launch", "com.tencent.mm") not in harness.device.actions()
    assert (
        "第1步：打开微信 → 失败：「com.tencent.mm」不在可以打开的应用里"
        in (harness.vision_messages[1][1]["content"][0]["text"])
    )


async def test_text_input_respects_safety_settings(tmp_path):
    config = PhonePlayConfig()
    config.safety.allow_text_input = False
    harness = Harness(
        tmp_path, FakeDevice(), [action(action="type", text="猫猫"), action(action="finish")], config=config
    )
    await harness.run()
    assert ("text", "猫猫") not in harness.device.actions()
    assert "不要输入任何文字" in harness.vision_messages[0][0]["content"]


async def test_ascii_only_input_is_announced_to_model(tmp_path):
    config = PhonePlayConfig()
    config.device.text_input = "ascii_only"
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=config)
    await harness.run()
    assert "只能输入英文字母和数字" in harness.vision_messages[0][0]["content"]

    config.device.text_input = "adb_keyboard"
    second = Harness(tmp_path / "second", FakeDevice(), [action(action="finish")], config=config)
    await second.run()
    assert "只能输入英文字母和数字" not in second.vision_messages[0][0]["content"]


async def test_iphone_back_gesture_is_explained_and_chinese_is_allowed(tmp_path):
    config = PhonePlayConfig()
    config.device.backend = "ios_wda"
    # 这一项只管安卓，iPhone 直接能输中文
    config.device.text_input = "ascii_only"
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=config)
    await harness.run()
    system_text = harness.vision_messages[0][0]["content"]
    assert "iPhone 没有返回键" in system_text
    assert "只能输入英文字母和数字" not in system_text
    # 实测她看到左上角的「◀ 抖音」以为自己还在抖音
    assert "「◀ 应用名」" in system_text

    android = Harness(tmp_path / "android", FakeDevice(), [action(action="finish")])
    await android.run()
    assert "「◀ 应用名」" not in android.vision_messages[0][0]["content"]


async def test_consecutive_invalid_answers_end_session(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), ["我也不知道", "嗯……", '{"action":"jump"}'])
    record = await harness.run()
    # 结束原因里说清楚卡在哪
    assert record.end_reason.startswith("连续 3 步操作失败（最后一次：没能决定下一步")
    assert harness.agent.status()["last_error"] == record.end_reason


async def test_plan_must_choose_installed_allowed_app(tmp_path):
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [],
        plan=json.dumps({"app": "com.dragon.read", "goal": "看小说", "minutes": 5}, ensure_ascii=False),
    )
    await harness.agent.start(trigger="chat", origin_stream="s")
    with pytest.raises(PlayRefused, match="^出错：没能决定玩什么") as refused:
        await harness.agent.wait_for_plan(5)
    await harness.wait_done()
    assert refused.value.chat_text == "你拿起手机后没想好玩什么，又放下了"
    assert "没能决定玩什么" in harness.agent.status()["last_error"]
    assert harness.device.actions() == []
    harness.assert_not_counted()


async def test_goal_the_phone_cannot_do_is_declined(tmp_path):
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [],
        plan=json.dumps({"app": "", "reason": "手机上没有王者荣耀"}, ensure_ascii=False),
    )
    await harness.agent.start(trigger="chat", hint="打王者荣耀", origin_stream="s")
    with pytest.raises(PlayRefused) as refused:
        await harness.agent.wait_for_plan(5)
    await harness.wait_done()
    # 告诉她做不到的原因和手机上能玩什么，她就不会硬说自己在下棋
    assert refused.value.chat_text == "手机上做不到：手机上没有王者荣耀（手机上能玩的只有哔哩哔哩、小红书）"
    assert '{"app": "", "reason"' in harness.plan_prompts[0]
    assert harness.device.actions() == []
    harness.assert_not_counted()


async def test_no_installed_allowed_app_is_refused(tmp_path):
    harness = Harness(tmp_path, FakeDevice(installed={"com.tencent.mm"}), [])
    await harness.agent.start(trigger="chat")
    with pytest.raises(PlayRefused, match="都没有安装") as refused:
        await harness.agent.wait_for_plan(5)
    await harness.wait_done()
    assert refused.value.chat_text == "手机上没有你能玩的应用"
    assert harness.agent.state == "idle"
    harness.assert_not_counted()


async def test_app_missing_on_launch_is_dropped_and_replanned(tmp_path):
    # iPhone 查不到装了哪些应用，打开小红书时才发现没装，换成 B 站
    device = FakeDevice(missing={"com.xingin.xhs"})
    xhs_plan = json.dumps({"app": "com.xingin.xhs", "goal": "看穿搭", "minutes": 5}, ensure_ascii=False)
    harness = Harness(tmp_path, device, [action(action="finish")], plan=[xhs_plan, PLAN])
    record = await harness.run()
    assert device.actions()[:4] == [
        ("stop", "com.xingin.xhs"),
        ("launch", "com.xingin.xhs"),
        ("stop", "tv.danmaku.bili"),
        ("launch", "tv.danmaku.bili"),
    ]
    assert "com.xingin.xhs" in harness.plan_prompts[0]
    assert "com.xingin.xhs" not in harness.plan_prompts[1]
    assert record.app == "哔哩哔哩"
    assert harness.agent.status()["today"]["sessions"] == 1


async def test_all_apps_missing_on_launch_is_refused_without_counting(tmp_path):
    harness = Harness(tmp_path, FakeDevice(installed={"tv.danmaku.bili"}, missing={"tv.danmaku.bili"}), [])
    await harness.agent.start(trigger="chat", origin_stream="s")
    with pytest.raises(PlayRefused, match="手机上都没有装") as refused:
        await harness.agent.wait_for_plan(5)
    await harness.wait_done()
    assert refused.value.chat_text == "手机上没有你能玩的应用"
    assert "allowed_apps" in harness.agent.status()["last_error"]
    harness.assert_not_counted()


async def test_unreachable_phone_is_refused_without_using_quota(tmp_path):
    device = FakeDevice(ready_error="adb connect 127.0.0.1:16416 失败：目标计算机积极拒绝")
    harness = Harness(tmp_path, device, [action(action="finish")])
    await harness.agent.start(trigger="chat", origin_stream="s")
    with pytest.raises(PlayRefused, match="积极拒绝") as refused:
        await harness.agent.wait_for_plan(60)
    await harness.wait_done()
    # 暂时连不上会在 20 秒内反复重试，到点才放弃
    assert device.calls.count(("ready",)) >= 4
    assert 15 <= harness.seconds <= 21
    # 聊天里只说手机连不上，adb 细节留在 WebUI 的最近错误里
    assert refused.value.chat_text == "手机连不上，可能没开机或者没连好"
    assert harness.agent.state == "idle"
    assert "积极拒绝" in harness.agent.status()["last_error"]
    connection = harness.agent.status()["connection"]
    assert connection["state"] == "failed" and "adb devices" in connection["hint"]
    harness.assert_not_counted()

    # 手机开好后马上就能玩，前面那次不占每日次数
    device.ready_error = ""
    await harness.run()
    assert harness.agent.status()["today"]["sessions"] == 1
    assert harness.agent.status()["last_error"] == ""


async def test_unchanged_screen_is_pointed_out(tmp_path):
    device = FakeDevice()
    harness = Harness(
        tmp_path,
        device,
        [action(action="wait", seconds=5, thought="还在加载")] * 4 + [action(action="finish")],
    )
    original_vision = harness.vision

    async def vision(messages):
        if len(harness.vision_messages) == 2:
            # 第三步之后画面变了（比如视频终于加载出来），第四步截图时就不一样了
            device.png = make_png(color="#000000")
        return await original_vision(messages)

    harness.agent._vision = vision
    await harness.run()
    texts = harness.state_texts()
    assert "几乎一样" not in texts[0]
    assert "画面已经连续 1 步和上一步几乎一样" in texts[1]
    assert "画面已经连续 2 步和上一步几乎一样" in texts[2]
    assert "几乎一样" not in texts[3]
    assert "画面已经连续 1 步和上一步几乎一样" in texts[4]
    assert "转圈加载时不要干等" in harness.vision_messages[0][0]["content"]


async def test_session_stops_at_step_limit(tmp_path):
    config = PhonePlayConfig()
    config.play.max_steps_per_session = 2
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [
            action(action="back", summary="返回", thought="这篇讲猫咪运动康复，有点学术"),
            action(action="back", summary="返回", thought="橘猫踩奶的图好可爱"),
        ],
        config=config,
    )
    record = await harness.run()
    assert record.end_reason == "达到单次 2 步的上限"
    assert harness.device.actions().count(("key", "back")) == 2
    # 不是她自己结束的，用最近的想法作为总结，方便之后聊起看到了什么
    assert record.summary == "这篇讲猫咪运动康复，有点学术；橘猫踩奶的图好可爱"


async def test_summary_prefers_thoughts_while_watching(tmp_path):
    config = PhonePlayConfig()
    config.play.max_steps_per_session = 3
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [
            action(action="wait", seconds=8, summary="看视频", thought="软绵绵的睦睦好可爱，再看一会儿"),
            action(action="back", summary="返回", thought="怎么跑到设置页面了，得回去"),
            action(action="back", summary="返回", thought="还在设置页，再返回一下"),
        ],
        config=config,
    )
    record = await harness.run()
    # 最后几步只是在返回，总结仍然是她停下来看内容时的想法
    assert record.summary == "软绵绵的睦睦好可爱，再看一会儿"


async def test_daily_limits_refuse_new_sessions(tmp_path):
    config = PhonePlayConfig()
    config.play.daily_session_limit = 1
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=config)
    await harness.run()
    with pytest.raises(PlayRefused, match="每日上限"):
        await harness.agent.start(trigger="chat")


async def test_stop_cancels_pending_model_call(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [])
    blocked = asyncio.Event()

    async def slow_vision(messages):
        blocked.set()
        await asyncio.sleep(3600)
        return ""

    harness.agent._vision = slow_vision
    await harness.agent.start(trigger="command", origin_stream="s")
    with pytest.raises(PlayRefused, match="已经在玩"):
        await harness.agent.start(trigger="chat")
    await asyncio.wait_for(blocked.wait(), 5)
    assert await harness.agent.stop("被命令叫停") is True
    assert harness.agent.state == "idle"
    assert harness.store.history[-1].end_reason == "被命令叫停"
    assert harness.notified[-1][1] == "s"
    assert await harness.agent.stop("again") is False


async def test_unloading_stops_without_finish_notice(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [])
    blocked = asyncio.Event()

    async def slow_vision(messages):
        blocked.set()
        await asyncio.sleep(3600)
        return ""

    harness.agent._vision = slow_vision
    await harness.agent.start(trigger="chat", origin_stream="s")
    await asyncio.wait_for(blocked.wait(), 5)
    await harness.agent.shutdown()
    assert harness.store.history[-1].end_reason == "插件卸载"
    assert harness.notified == []
    # 卸载时断开和手机的连接
    assert harness.device.calls[-1] == ("close",)


async def test_pause_and_resume(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")])
    gate = asyncio.Event()
    original_vision = harness.vision

    async def gated_vision(messages):
        await gate.wait()
        return await original_vision(messages)

    harness.agent._vision = gated_vision
    await harness.agent.start(trigger="webui")
    for _ in range(100):
        if harness.agent.state == "playing":
            break
        await asyncio.sleep(0)
    assert harness.agent.pause() is True
    assert harness.agent.state == "paused"
    assert harness.agent.pause() is False
    assert harness.agent.resume() is True
    gate.set()
    await asyncio.wait_for(harness.agent._task, 5)
    assert harness.store.history[-1].end_reason == "自己不想玩了"


async def test_frames_are_shared_with_webui(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [])
    assert harness.agent.frame_payload(0)["frame_id"] == 0
    await harness.agent.refresh_frame()
    payload = harness.agent.frame_payload(0)
    assert payload["frame_id"] == 1
    assert payload["image"].startswith("data:image/jpeg;base64,")
    assert (payload["width"], payload["height"]) == (540, 1200)
    # 画面没变时不重复传图片
    assert harness.agent.frame_payload(1)["image"] is None
    jpeg = await harness.agent.chat_screenshot()
    assert jpeg[:2] == b"\xff\xd8"


async def test_activity_text_describes_last_session(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish", summary="看了两只猫")])
    assert harness.agent.activity_text() == "现在没在玩手机，最近也没有玩过。"
    await harness.run()
    text = harness.agent.activity_text()
    assert text.startswith("现在没在玩手机。上一次玩手机是")
    assert "哔哩哔哩" in text and "看了两只猫" in text and "今天一共玩了 1 次" in text


async def test_auto_play_respects_probability_and_hours(tmp_path):
    config = PhonePlayConfig()
    config.auto_play.enabled = True
    config.auto_play.play_probability = 0.5
    config.auto_play.active_hours = "00:00-00:00"
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=config)

    harness.agent._rng = lambda: 0.9
    await harness.agent._auto_decide()
    assert harness.agent.state == "idle"

    harness.agent._rng = lambda: 0.1
    await harness.agent._auto_decide()
    assert harness.agent.state != "idle"
    await asyncio.wait_for(harness.agent._task, 5)
    assert harness.store.history[-1].trigger == "自己想玩"


class Schedule:
    """假的麦生活：依次给出日程里现在这一段（或抛出查不到的原因），最后一个一直沿用；记下被问了几次。"""

    def __init__(self, *results: Union[Optional[LifeActivity], LifeLinkError]) -> None:
        self.results = list(results)
        self.calls = 0

    async def __call__(self) -> Optional[LifeActivity]:
        self.calls += 1
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, LifeLinkError):
            raise result
        return result


def life(kind: str, name: str, minutes: float = 40, location: str = "", scene: str = "") -> LifeActivity:
    return LifeActivity(kind=kind, name=name, location=location, scene=scene, remaining_seconds=minutes * 60)


def auto_config(**life_link: Any) -> PhonePlayConfig:
    """一定会去玩的自动玩（概率 1、全天），生活联动按参数改。"""
    config = PhonePlayConfig()
    config.auto_play.enabled = True
    config.auto_play.play_probability = 1.0
    config.auto_play.active_hours = "00:00-00:00"
    for key, value in life_link.items():
        setattr(config.life_link, key, value)
    return config


async def test_auto_play_skips_sleep_in_the_schedule_and_says_why(tmp_path):
    schedule = Schedule(life("sleep", "睡到闹钟响", location="卧室"))
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=auto_config(), life=schedule)
    await harness.agent._auto_decide()
    assert harness.agent.state == "idle"
    assert harness.agent.status()["life"] == {
        "checked_at": harness.agent._life_check.checked_at.strftime("%H:%M"),
        "allowed": False,
        "slack": False,
        "text": "按麦生活的日程，现在是「睡到闹钟响」（睡觉 · 卧室），先不玩",
    }
    # 看状态时一分钟内不重复问；自动玩每次都重新问
    await harness.agent.refresh_life(max_age=60)
    assert schedule.calls == 1
    harness.seconds += 61
    await harness.agent.refresh_life(max_age=60)
    assert schedule.calls == 2


async def test_work_time_is_an_occasional_short_slack(tmp_path):
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [action(action="finish", summary="偷看了两条猫猫视频")],
        config=auto_config(),
        life=Schedule(life("work", "整理项目文档", minutes=120, location="公司工位")),
        share=True,
    )
    # 平常的概率是 1，上班时只按摸鱼的概率（默认 0.1）
    harness.agent._rng = lambda: 0.2
    await harness.agent._auto_decide()
    assert harness.agent.state == "idle"
    status = harness.agent.status()["life"]
    assert (status["allowed"], status["slack"]) == (True, True)
    assert status["text"] == (
        "按麦生活的日程，现在是「整理项目文档」（工作 · 公司工位），"
        "偶尔摸鱼玩一会儿（每次考虑时 10% 的概率，最多 5 分钟）"
    )

    harness.agent._rng = lambda: 0.05
    await harness.agent._auto_decide()
    await harness.wait_done()
    prompt = harness.plan_prompts[0]
    assert "你正忙着，想趁没人注意偷偷拿起手机摸一会儿鱼。" in prompt
    assert "这是工作中途偷偷摸鱼：只玩几分钟" in prompt and "玩完接着工作" in prompt
    assert "玩多久（1~5 分钟）" in prompt
    logs = harness.agent.status()["logs"]
    assert logs[-1]["summary"] == "拿起手机（摸鱼）"
    assert (
        logs[-1]["result"]
        == "按麦生活的日程，现在是「整理项目文档」（工作 · 公司工位），偷偷摸鱼玩一会儿（最多 5 分钟）"
    )
    record = harness.store.history[-1]
    assert record.trigger == "摸鱼"
    # 自然结束的摸鱼也交给麦生活
    assert harness.shared == [record]


async def test_slack_can_be_turned_off(tmp_path):
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [],
        config=auto_config(slack_probability=0),
        life=Schedule(life("work", "整理项目文档", location="公司工位")),
    )
    harness.agent._rng = lambda: 0.0
    await harness.agent._auto_decide()
    assert harness.agent.state == "idle"
    assert harness.agent.status()["life"]["text"] == "按麦生活的日程，现在是「整理项目文档」（工作 · 公司工位），先不玩"


async def test_auto_play_in_free_time_fits_into_what_is_left(tmp_path):
    config = auto_config(min_minutes=1)
    schedule = Schedule(life("travel", "坐地铁回家", minutes=3.5, location="地铁上", scene="站在车门边，信号时有时无"))
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=config, life=schedule)
    await harness.agent._auto_decide()
    await harness.wait_done()

    prompt = harness.plan_prompts[0]
    assert (
        "按你今天的日程，现在是「坐地铁回家」（在路上 · 地铁上），这段还有大约 3 分钟。"
        "此刻的情形：站在车门边，信号时有时无" in prompt
    )
    assert "玩多久（1~3 分钟）" in prompt
    logs = harness.agent.status()["logs"]
    assert logs[-1]["summary"] == "拿起手机（自己想玩）"
    assert logs[-1]["result"] == "按麦生活的日程，现在是「坐地铁回家」（在路上 · 地铁上），这段还有 3 分钟"
    # 模型想玩 5 分钟：日程只剩 3 分钟，按 3 分钟
    planned = next(log for log in logs if log["summary"].startswith("打算打开"))
    assert planned["result"] == "计划玩 3 分钟"
    assert harness.store.history[-1].trigger == "自己想玩"


async def test_auto_play_waits_when_free_time_is_almost_over(tmp_path):
    harness = Harness(
        tmp_path, FakeDevice(), [], config=auto_config(), life=Schedule(life("meal", "午饭", minutes=4.5))
    )
    await harness.agent._auto_decide()
    assert harness.agent.state == "idle"
    assert harness.agent.status()["life"]["text"] == "按麦生活的日程，现在是「午饭」（吃饭），只剩 4 分钟，先不玩"


@pytest.mark.parametrize(
    "result,reason",
    [
        (
            LifeLinkError("没连上麦生活：未找到 API 提供方插件: maibot-community.mai-life"),
            "没连上麦生活：未找到 API 提供方插件: maibot-community.mai-life",
        ),
        (LifeLinkError("麦生活今天的日程还没生成"), "麦生活今天的日程还没生成"),
        (None, "麦生活的日程这会儿是空档"),
    ],
)
async def test_auto_play_without_a_usable_schedule_uses_active_hours(tmp_path, result, reason):
    config = auto_config()
    config.auto_play.active_hours = "19:00-23:00"
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=config, life=Schedule(result))
    harness.agent._now = lambda: datetime(2026, 11, 6, 13, 0)
    await harness.agent._auto_decide()
    assert harness.agent.state == "idle"
    assert harness.agent.status()["life"] == {
        "checked_at": "13:00",
        "allowed": None,
        "slack": False,
        "text": f"{reason}，按 active_hours（19:00-23:00）",
    }

    config.auto_play.active_hours = "10:00-23:00"
    await harness.agent._auto_decide()
    await harness.wait_done()
    assert "按你今天的日程" not in harness.plan_prompts[0]
    assert harness.store.history[-1].trigger == "自己想玩"


async def test_schedule_is_not_asked_when_the_link_is_off_or_she_is_asked_to_play(tmp_path):
    schedule = Schedule(life("work", "整理项目文档"))
    config = auto_config(enabled=False)
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")] * 2, config=config, life=schedule)
    # 关掉联动：日程上在工作也照 active_hours 去玩
    await harness.agent._auto_decide()
    await harness.wait_done()
    assert schedule.calls == 0 and harness.agent.status()["life"] is None

    # 聊天、命令、WebUI 叫她玩：不看日程
    config.life_link.enabled = True
    await harness.run()
    assert schedule.calls == 0
    assert "按你今天的日程" not in harness.plan_prompts[-1]


def log_entries(harness: Harness, summary: str) -> List[Dict[str, Any]]:
    return [entry for entry in harness.agent.status()["logs"] if entry["summary"] == summary]


async def test_auto_play_by_the_schedule_is_shared_with_mai_life(tmp_path):
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [action(thought="这只布偶叫得好认真", summary="看布偶猫", action="finish")],
        config=auto_config(),
        life=Schedule(life("leisure", "看悬疑剧", location="客厅")),
        share=True,
    )
    await harness.agent._auto_decide()
    await harness.wait_done()
    assert [record.app for record in harness.shared] == ["哔哩哔哩"]
    assert harness.shared[0].end_reason == "自己不想玩了"
    assert log_entries(harness, "交给麦生活分享")[0]["ok"] is True


async def test_mai_life_refusing_the_share_is_shown_in_the_log(tmp_path):
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [action(action="finish", summary="看布偶猫")],
        config=auto_config(),
        life=Schedule(life("leisure", "看悬疑剧")),
        share=True,
    )
    harness.share_error = LifeLinkError("麦生活的日程这会儿是空档，它只在有日程时收分享")
    await harness.agent._auto_decide()
    await harness.wait_done()
    entry = log_entries(harness, "没交给麦生活分享")[0]
    assert entry["ok"] is False and entry["result"] == "麦生活的日程这会儿是空档，它只在有日程时收分享"
    # 记录照常保存
    assert harness.store.history[-1].app == "哔哩哔哩"


async def test_only_natural_auto_sessions_from_the_schedule_are_shared(tmp_path):
    finish = [action(action="finish", summary="看布偶猫")]

    # 关掉分享
    harness = Harness(
        tmp_path / "off",
        FakeDevice(),
        finish,
        config=auto_config(share_after_play=False),
        life=Schedule(life("leisure", "看悬疑剧")),
        share=True,
    )
    await harness.agent._auto_decide()
    await harness.wait_done()
    assert harness.shared == [] and log_entries(harness, "交给麦生活分享") == []

    # 没装麦生活、按 active_hours 玩的：不去调一个不存在的接口
    harness = Harness(
        tmp_path / "fallback",
        FakeDevice(),
        finish,
        config=auto_config(),
        life=Schedule(LifeLinkError("没连上麦生活：未找到 API 提供方插件: maibot-community.mai-life")),
        share=True,
    )
    await harness.agent._auto_decide()
    await harness.wait_done()
    assert harness.shared == [] and log_entries(harness, "没交给麦生活分享") == []

    # 命令叫她玩的：有人看着，不交
    harness = Harness(
        tmp_path / "command", FakeDevice(), finish, config=auto_config(), life=Schedule(life("leisure", "看悬疑剧"))
    )
    harness.agent._life_share = harness.share
    await harness.run()
    assert harness.shared == []

    # 自动玩到一半被叫停：不是自然结束，不交
    harness = Harness(
        tmp_path / "stopped",
        FakeDevice(),
        [],
        config=auto_config(),
        life=Schedule(life("leisure", "看悬疑剧")),
        share=True,
    )
    looking = asyncio.Event()

    async def slow_vision(messages: List[Dict[str, Any]]) -> str:
        looking.set()
        await asyncio.sleep(3600)
        return ""

    harness.agent._vision = slow_vision
    await harness.agent._auto_decide()
    await asyncio.wait_for(looking.wait(), 5)
    assert await harness.agent.stop("在 WebUI 中停止") is True
    assert harness.store.history[-1].end_reason == "在 WebUI 中停止"
    assert harness.shared == []


async def test_she_says_when_she_picks_up_and_puts_down_the_phone(tmp_path):
    harness = Harness(
        tmp_path, FakeDevice(), [action(action="finish", summary="看了三只橘猫，圆滚滚的好可爱")], announce=True
    )
    record = await harness.run(trigger="pet")
    await harness.settle()
    assert harness.announced == ["拿起手机：打开哔哩哔哩，看猫猫视频", f"放下手机：{record.summary}"]
    # 拿起手机打个招呼，玩够了放下时高兴一下
    assert harness.events == ["greeting", "happy"]
    assert record.trigger == "桌宠"


async def test_slacking_off_is_announced_as_such_and_long_thoughts_are_cut(tmp_path):
    harness = Harness(
        tmp_path,
        FakeDevice(),
        # 她的总结太短时会拼上她的想法，拼出来可能很长
        [action(action="finish", summary="看完了", thought="猫" * 100)],
        config=auto_config(),
        life=Schedule(life("work", "整理项目文档")),
        announce=True,
    )
    harness.agent._rng = lambda: 0.0
    await harness.agent._auto_decide()
    await harness.wait_done()
    await harness.settle()
    record = harness.store.history[-1]
    assert len(record.summary) > 80
    assert harness.announced[0] == "偷偷摸鱼：打开哔哩哔哩，看猫猫视频"
    assert harness.announced[1] == "放下手机：" + record.summary[:79] + "…"


async def test_a_failing_bubble_does_not_get_in_her_way(tmp_path, caplog):
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish", summary="看完了")], announce=True)
    harness.announce_error = RuntimeError("桌宠窗口还没就绪")
    with caplog.at_level(logging.WARNING, logger="phone_play_test"):
        record = await harness.run()
        await harness.settle()
    assert record.end_reason == "自己不想玩了"
    assert caplog.text.count("桌宠气泡或动作没显示：桌宠窗口还没就绪") == 2


async def test_nothing_is_announced_when_the_plugin_unloads_or_nothing_was_opened(tmp_path):
    harness = Harness(tmp_path, FakeDevice(installed=set()), [], announce=True)
    with pytest.raises(PlayRefused):
        await harness.agent.start(trigger="command")
        await harness.agent.wait_for_plan(5)
    await harness.settle()
    assert harness.announced == []

    harness = Harness(tmp_path, FakeDevice(), [], announce=True)
    looking = asyncio.Event()

    async def slow_vision(messages: List[Dict[str, Any]]) -> str:
        looking.set()
        await asyncio.sleep(3600)
        return ""

    harness.agent._vision = slow_vision
    await harness.agent.start(trigger="command")
    await asyncio.wait_for(looking.wait(), 5)
    await harness.agent.shutdown()
    # 拿起手机那句已经说了；插件卸载时不再说放下手机
    assert harness.announced == ["拿起手机：打开哔哩哔哩，看猫猫视频"]


async def test_her_thoughts_pop_up_while_she_plays(tmp_path):
    config = PhonePlayConfig()
    config.play.step_interval_seconds = 10
    config.desktop_pet.thought_bubble_seconds = 5
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [
            action(action="back", summary="返回", thought="橘猫踩奶的图好可爱"),
            # 同一句想法不再冒
            action(action="back", summary="返回", thought="橘猫踩奶的图好可爱"),
            action(action="back", summary="返回", thought="这篇讲猫咪运动康复，有点学术"),
            # 结束这一步的想法跟着「放下手机」那句说，不单独冒
            action(action="finish", summary="看完了", thought="不看了，去睡觉"),
        ],
        config=config,
        announce=True,
        refresh=True,
    )
    record = await harness.run()
    await harness.settle()
    assert harness.announced == [
        "拿起手机：打开哔哩哔哩，看猫猫视频",
        "💭 橘猫踩奶的图好可爱",
        "💭 这篇讲猫咪运动康复，有点学术",
        f"放下手机：{record.summary}",
    ]
    # 开始、打开应用、三步各一次、放下手机：按钮跟着刷新
    assert harness.refreshes == 6


async def test_thought_bubbles_are_spaced_out_and_can_be_turned_off(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [], announce=True)
    pet = harness.config.desktop_pet
    pet.thought_bubble_seconds = 20
    agent = harness.agent
    agent._announce("拿起手机：打开哔哩哔哩，看猫猫视频")
    harness.seconds = 10
    # 离上一个气泡才 10 秒：前一句可能还没看完，不冒
    agent._announce("💭 好多猫", thought=True)
    harness.seconds = 25
    agent._announce("💭 好多猫", thought=True)
    harness.seconds = 50
    agent._announce("💭 好多猫", thought=True)
    agent._announce("💭 这只是橘猫", thought=True)
    harness.seconds = 55
    # 拿起、放下手机的那句不受间隔限制
    agent._announce("放下手机：看了好多猫")
    await harness.settle()
    assert harness.announced == [
        "拿起手机：打开哔哩哔哩，看猫猫视频",
        "💭 好多猫",
        "💭 这只是橘猫",
        "放下手机：看了好多猫",
    ]

    pet.thought_bubbles = False
    harness.seconds = 100
    agent._announce("💭 又一只猫", thought=True)
    agent._announce("拿起手机：打开抖音，随便看看")
    pet.bubbles = False
    # 气泡关了，动作照做
    agent._announce("放下手机：看完了", event="happy")
    pet.animations = False
    # 气泡和动作都关了，不打扰桌宠
    agent._announce("拿起手机：打开抖音，随便看看", event="greeting")
    await harness.settle()
    assert harness.announced[4:] == ["拿起手机：打开抖音，随便看看"]
    assert harness.events == ["happy"]


async def test_the_pet_shakes_its_head_when_she_gets_stuck_but_not_when_you_stop_her(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), ["我也不知道", "嗯……", '{"action":"jump"}'], announce=True)
    record = await harness.run()
    await harness.settle()
    assert record.end_reason.startswith("连续 3 步操作失败")
    assert harness.events == ["greeting", "error"]

    harness = Harness(tmp_path, FakeDevice(), [], announce=True)
    looking = asyncio.Event()

    async def slow_vision(messages: List[Dict[str, Any]]) -> str:
        looking.set()
        await asyncio.sleep(3600)
        return ""

    harness.agent._vision = slow_vision
    await harness.agent.start(trigger="command")
    await asyncio.wait_for(looking.wait(), 5)
    await harness.agent.stop("在 WebUI 中停止")
    await harness.settle()
    # 被叫停：照样说一句放下手机，但不做动作
    assert harness.announced[-1].startswith("放下手机：")
    assert harness.events == ["greeting"]


async def test_the_pet_button_refreshes_when_you_pause_or_take_over(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [], refresh=True)
    looking = asyncio.Event()

    async def slow_vision(messages: List[Dict[str, Any]]) -> str:
        looking.set()
        await asyncio.sleep(3600)
        return ""

    harness.agent._vision = slow_vision
    await harness.agent.start(trigger="command")
    await asyncio.wait_for(looking.wait(), 5)
    await harness.settle()
    started = harness.refreshes
    assert started == 2
    for change in (harness.agent.pause, harness.agent.resume, harness.agent.takeover, harness.agent.release):
        assert change() is True
    # 状态没变的不刷新
    assert harness.agent.release() is False
    await harness.settle()
    assert harness.refreshes == started + 4
    await harness.agent.shutdown()


class PartnerPhone(FakeDevice):
    """一起玩时的手机：等对方时截了 change_after 张图之后，对方操作了，画面变了。"""

    def __init__(self, change_after: int) -> None:
        super().__init__()
        self.change_after = change_after
        self.waiting_shots = 0

    async def screenshot(self) -> bytes:
        if self.calls and self.calls[-1][0] == "screenshot":
            self.waiting_shots += 1
            if self.waiting_shots >= self.change_after:
                self.png = make_png(color="#cc3366")
        return await super().screenshot()


async def test_together_waits_for_partner_without_calling_model(tmp_path):
    harness = Harness(
        tmp_path,
        PartnerPhone(change_after=3),
        [action(action="wait_turn", thought="轮到对方了"), action(action="finish", summary="和对方轮流玩了一会儿")],
    )
    record = await harness.run(trigger="chat", together=True, origin_stream="s")
    # 等对方时只截图比对，两步决策之间没有多调模型
    assert len(harness.vision_messages) == 2
    assert "对方操作完了，画面变了" in harness.state_texts()[1]
    system = harness.vision_messages[0][0]["content"]
    assert '{"action":"wait_turn"}' in system and "这次是和对方一起玩" in system
    assert record.together is True and harness.notified == [(record, "s")]


async def test_together_also_covers_playing_online_from_partners_own_phone(tmp_path):
    # 实测对方在自己手机上和她组斗地主 2v2：规则只许同机轮流、不许联网匹配，她进了房间不敢开局就结束了；
    # 每次开始又先关掉应用，房间也跟着没了
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish", summary="和对方打了一局斗地主，赢了")])
    await harness.run(trigger="chat", together=True, origin_stream="s")
    assert ("stop", "tv.danmaku.bili") not in harness.device.actions()
    assert harness.device.actions()[0] == ("launch", "tv.danmaku.bili")
    system = harness.vision_messages[0][0]["content"]
    assert "对方用自己的手机和你联机" in system and "等开局、等匹配时，用 wait 或 wait_turn 等着，不要 finish" in system
    assert "不要联网匹配陌生人" not in system
    assert "也可能用自己的手机和你联机、组队" in harness.plan_prompts[0]


async def test_wait_turn_needs_a_partner_and_gives_up_after_a_while(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [action(action="wait_turn"), action(action="finish")])
    await harness.run()
    assert "失败：只有和对方一起玩时才需要等对方" in harness.state_texts()[1]
    assert '{"action":"wait_turn"}' not in harness.vision_messages[0][0]["content"]

    config = PhonePlayConfig()
    config.play.turn_wait_seconds = 10
    waiting = Harness(
        tmp_path / "waiting", FakeDevice(), [action(action="wait_turn"), action(action="finish")], config=config
    )
    await waiting.run(together=True)
    assert "等了 10 秒，对方还没动" in waiting.state_texts()[1]


@pytest.mark.parametrize(
    "spec,hour,minute,expected",
    [
        ("10:00-23:00", 9, 59, False),
        ("10:00-23:00", 10, 0, True),
        ("22:00-02:00", 23, 30, True),
        ("22:00-02:00", 1, 59, True),
        ("22:00-02:00", 2, 0, False),
        ("00:00-00:00", 15, 0, True),
    ],
)
def test_in_active_hours(spec, hour, minute, expected):
    assert in_active_hours(spec, datetime(2026, 10, 28, hour, minute)) is expected


def interaction_config(**overrides: Any) -> PhonePlayConfig:
    config = PhonePlayConfig()
    config.interaction.enabled = True
    for key, value in overrides.items():
        setattr(config.interaction, key, value)
    return config


def interaction_elements() -> List[UIElement]:
    return [
        UIElement(index=1, label="点赞", kind="Button", clickable=True, bounds=(980, 1150, 1170, 1310)),
        UIElement(index=2, label="关注", kind="Button", clickable=True, bounds=(1020, 1020, 1140, 1130)),
        UIElement(index=3, label="发送", kind="Button", clickable=True, bounds=(1000, 2200, 1160, 2300)),
        UIElement(index=4, label="说点什么", kind="TextView", clickable=True, bounds=(40, 2200, 960, 2300)),
    ]


async def test_interactions_are_off_by_default(tmp_path):
    responses = [action(action="like", element=1), action(action="finish")]
    harness = Harness(tmp_path, FakeDevice(elements=interaction_elements()), responses)
    record = await harness.run()
    system_text = harness.vision_messages[0][0]["content"]
    assert "不要发评论" in system_text
    assert '"action":"comment"' not in system_text
    assert "今天还能点赞" not in harness.state_texts()[0]
    # 没开启时 like 被拦下，手机上什么都没点
    assert not any(call[0] == "tap" for call in harness.device.actions())
    assert "没有开启互动" in harness.state_texts()[1]
    assert record.interactions == []
    assert "不发布内容、不和别人互动" in harness.plan_prompts[0]


async def test_she_can_like_comment_and_follow_on_her_own_account(tmp_path):
    harness = Harness(
        tmp_path,
        FakeDevice(elements=interaction_elements(), input_focus=True),
        [
            action(action="like", element=1, summary="点赞橘猫视频"),
            action(action="comment", element=4, text="这只橘猫也太圆了", summary="评论橘猫"),
            action(action="tap", element=3, summary="发送评论"),
            action(action="follow", element=2, summary="关注猫猫博主"),
            action(action="finish", summary="看了橘猫"),
        ],
        config=interaction_config(),
    )
    record = await harness.run(origin_stream="stream-1")

    touches = [call for call in harness.device.actions() if call[0] in ("tap", "text")]
    # 写评论时先点开输入框
    assert touches == [
        ("tap", 1075, 1230),
        ("tap", 500, 2250),
        ("text", "这只橘猫也太圆了"),
        ("tap", 1080, 2250),
        ("tap", 1080, 1075),
    ]
    assert record.interactions == ["点赞 1 次", "评论「这只橘猫也太圆了」", "关注了猫猫博主"]
    assert harness.notified[0][0].interactions == record.interactions

    system_text = harness.vision_messages[0][0]["content"]
    assert "登录的是你自己的账号" in system_text
    # 决定玩什么时也知道能互动，否则「想给 UP 主留评论」会被当成做不到
    assert "可以点赞、评论、关注" in harness.plan_prompts[0]
    assert "不发布内容、不和别人互动" not in harness.plan_prompts[0]
    assert '"action":"comment"' in system_text and '"action":"follow"' in system_text
    assert "不要发评论" not in system_text
    # 实测她在抖音上对同一条视频连点四次赞，点一次赞、再点一次又取消了
    assert "同一条视频只点一次" in system_text
    # 额度紧挨着截图说，每用一次就少一次
    states = harness.state_texts()
    assert "今天还能点赞 30 次、评论 5 条、关注 3 个" in states[0]
    assert "今天还能点赞 29 次、评论 5 条" in states[1]
    assert "已写好评论，还要点发送或发布才会发出去" in states[2]

    saved = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert saved["interactions"] == {"like": 1, "comment": 1, "follow": 1}
    assert "那次用自己的账号点赞 1 次、评论「这只橘猫也太圆了」、关注了猫猫博主" in harness.agent.activity_text()


async def test_interaction_limits_and_comment_checks_are_enforced_by_code(tmp_path):
    config = interaction_config(daily_comment_limit=2, allow_follow=False, comment_max_length=10)
    config.play.max_consecutive_errors = 5
    harness = Harness(
        tmp_path,
        FakeDevice(elements=interaction_elements()),
        [
            action(action="comment", element=4, text="去 www.example.com 看看"),
            action(action="comment", element=4, text="这只橘猫真的真的太圆了吧"),
            action(action="comment", element=4, text="好圆"),
            action(action="comment", element=4, text="@小明 快来看"),
            action(action="comment", element=4, text="加我 12345678"),
            action(action="comment", element=4, text="再说一句"),
            action(action="comment", element=4, text="第三句"),
            action(action="follow", element=2),
            action(action="finish"),
        ],
        config=config,
    )
    await harness.run()
    assert [call[1] for call in harness.device.actions() if call[0] == "text"] == ["好圆", "再说一句"]
    states = harness.state_texts()
    assert "链接、@ 别人或联系方式" in states[1]
    assert "评论太长了，最多 10 个字" in states[2]
    assert "链接、@ 别人或联系方式" in states[4] and "链接、@ 别人或联系方式" in states[5]
    assert "今天不能再评论了" in states[7]
    assert "今天不能再关注了" in states[8]
    # 不允许关注时，提示词里也不教她用 follow
    assert '"action":"follow"' not in harness.vision_messages[0][0]["content"]
    assert "今天不能再关注了。" in harness.vision_messages[0][0]["content"]


async def test_comment_that_did_not_reach_the_input_box_is_not_counted(tmp_path):
    # 实测她点到了表情按钮，打开的是应用自己的表情面板，字没写进去，她却以为写好了
    harness = Harness(
        tmp_path,
        FakeDevice(elements=interaction_elements(), input_focus=False),
        [action(action="comment", element=4, text="好圆"), action(action="finish")],
        config=interaction_config(),
    )
    record = await harness.run()
    assert "评论没写进输入框" in harness.state_texts()[1]
    assert record.interactions == []
    assert harness.store.interactions(harness.agent._today()) == {}


async def test_repeating_the_same_tap_is_pointed_out(tmp_path):
    # 视频一直在播，画面总在变，只比截图发现不了她在同一个地方点了二十多次
    tap = action(action="tap", point=[900, 611], summary="发送评论")
    nearby = action(action="tap", point=[905, 615])
    harness = Harness(tmp_path, FakeDevice(), [tap, tap, nearby, tap, action(action="finish")])
    await harness.run()
    states = harness.state_texts()
    assert "同一个操作" not in states[2]
    # 差几个点也算同一个位置
    assert "你已经连续 3 步在做同一个操作（点击 (905, 615)）" in states[3]
    assert "你已经连续 4 步在做同一个操作" in states[4]


async def test_interaction_counts_reset_daily_and_survive_restart(tmp_path):
    path = tmp_path / "usage.json"
    # 旧版本写的文件没有 interactions
    path.write_text(json.dumps({"date": "2026-10-29", "sessions": 1, "steps": 3, "history": []}), encoding="utf-8")
    store = UsageStore(path)
    await store.load()
    assert store.interactions("2026-10-29") == {}
    store.add_interaction("2026-10-29", "comment")
    store.add_interaction("2026-10-29", "comment")
    await store.save()

    reloaded = UsageStore(path)
    await reloaded.load()
    assert reloaded.interactions("2026-10-29") == {"comment": 2}
    assert reloaded.interactions("2026-10-30") == {}


@pytest.mark.parametrize(
    "content", ["{坏掉的", '{"date": "2026-10-29"}', '{"date": "x", "sessions": 1, "steps": 2, "history": [1]}']
)
async def test_broken_usage_file_is_backed_up_instead_of_blocking_the_plugin(tmp_path, content):
    # 插件中心审核建议：记录文件坏了不该让整个插件加载失败、要用户手动删文件
    path = tmp_path / "usage.json"
    path.write_text(content, encoding="utf-8")
    store = UsageStore(path)
    await store.load()
    assert store.history == [] and store.usage("2026-10-31") == (0, 0)
    backups = list(tmp_path.glob("usage.json.broken-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == content
    assert "已损坏" in store.recovered and backups[0].name in store.recovered
    store.add_session("2026-10-31")
    await store.save()
    assert json.loads(path.read_text(encoding="utf-8"))["sessions"] == 1


async def test_short_finish_summary_is_padded_with_what_she_saw(tmp_path):
    # 实测 gpt-6.1-terra 结束时只写了「结束刷抖音」，聊天里提起这次时没什么可说的
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [
            action(action="wait", seconds=15, thought="奶奶找吃的给孙女，看看。"),
            action(action="finish", summary="结束刷抖音", thought="刷得差不多了，奶奶的视频很暖。"),
        ],
    )
    record = await harness.run()
    assert record.summary == "结束刷抖音；奶奶找吃的给孙女，看看；刷得差不多了，奶奶的视频很暖"
    system_text = harness.vision_messages[0][0]["content"]
    assert "这一步的 summary 不要写「结束」" in system_text

    # 写得够长就照用她的总结
    long_summary = "看了好几只小奶猫，那只恐高的小猫最呆"
    second = Harness(
        tmp_path / "second",
        FakeDevice(),
        [action(action="wait", seconds=15, thought="好呆"), action(action="finish", summary=long_summary)],
    )
    assert (await second.run()).summary == long_summary


class FlakyDevice(FakeDevice):
    """第 drop_at 次截图时断开，之后 down_checks 次连接检查都还连不上。"""

    def __init__(self, drop_at: int, down_checks: int) -> None:
        super().__init__()
        self.drop_at = drop_at
        self.down_checks = down_checks
        self.shots = 0

    async def screenshot(self) -> bytes:
        self.shots += 1
        if self.shots == self.drop_at:
            raise DeviceError("连不上 iPhone 上的 WebDriverAgent")
        return await super().screenshot()

    async def ensure_ready(self) -> None:
        await super().ensure_ready()
        if self.shots >= self.drop_at and self.down_checks > 0:
            self.down_checks -= 1
            raise DeviceError("连不上 iPhone 上的 WebDriverAgent")


async def test_short_disconnect_is_waited_out(tmp_path):
    # 实测 iPhone 一晚上断开好几次，保活脚本十几秒就把 WDA 拉起来，之前整次玩手机却直接出错结束
    harness = Harness(
        tmp_path,
        FlakyDevice(drop_at=2, down_checks=2),
        [action(action="wait", seconds=5), action(action="finish", summary="看了一会儿视频，中间手机断了一下")],
    )
    record = await harness.run()
    assert record.end_reason == "自己不想玩了"
    summaries = [entry["summary"] for entry in harness.agent.status()["logs"]]
    assert "手机断开了，等它恢复" in summaries and "手机恢复了，接着玩" in summaries
    assert "手机断开了一会儿，刚恢复" in harness.state_texts()[-1]


async def test_long_disconnect_still_ends_the_session(tmp_path):
    harness = Harness(tmp_path, FlakyDevice(drop_at=2, down_checks=100), [action(action="wait", seconds=5)])
    record = await harness.run()
    assert record.end_reason.startswith("出错：连不上 iPhone")
    # 等满了才放弃
    assert harness.seconds >= 45


class ScenePhone(FakeDevice):
    """画面按截图次数切换：第 k 次截图起是 scenes[k-1]（用完停在最后一个），界面元素跟着画面走。

    每一步先截图做决定；要点的话插件点之前再截一张核对，所以第 2 张就是「模型想好时」的画面。
    """

    def __init__(self, scenes: Sequence[Tuple[bytes, Sequence[UIElement]]]) -> None:
        super().__init__()
        self.scenes = list(scenes)
        self.shots = 0
        self.ui_reads = 0

    async def screenshot(self) -> bytes:
        self.shots += 1
        self.png, elements = self.scenes[min(self.shots, len(self.scenes)) - 1]
        self.elements = list(elements)
        return await super().screenshot()

    async def ui_elements(self, width: int, height: int, limit: int) -> List[UIElement]:
        self.ui_reads += 1
        return await super().ui_elements(width, height, limit)

    def taps(self) -> List[Tuple]:
        return [call for call in self.actions() if call[0] == "tap"]


async def test_tap_is_skipped_when_the_target_is_gone_by_the_time_of_tapping(tmp_path):
    # 实测：她去点 B 站弹窗的「不再提醒」，等模型想好，弹窗已经自己收起，点到了底下的投稿按钮
    popup_box, plus_box = (40, 1050, 500, 1130), (230, 1060, 310, 1140)
    popup = UIElement(index=1, label="不再提醒", kind="Button", clickable=True, bounds=popup_box)
    plus = UIElement(index=1, label="投稿", kind="ImageView", clickable=True, bounds=plus_box)
    with_popup = (scene_png((popup_box, "#ffffff")), [popup])
    without_popup = (scene_png((plus_box, "#ff6699")), [plus])
    # 连着三次都是这样，也不算连续出错，不会因此结束
    harness = Harness(
        tmp_path,
        ScenePhone([with_popup, without_popup] * 3),
        [action(action="tap", element=1, summary="关掉弹窗")] * 3
        + [action(action="finish", summary="弹窗总是自己没了，不玩了")],
    )
    record = await harness.run()
    assert harness.device.taps() == []
    assert record.end_reason == "自己不想玩了"
    assert (
        "→ 没点：点之前画面变了，「不再提醒」已经不在原来的位置了。先看看现在的画面再决定" in harness.state_texts()[1]
    )
    log = harness.agent.status()["logs"][-4]
    assert log["ok"] is False and log["result"].startswith("没点：")


async def test_tap_follows_the_same_element_when_it_moved_a_little(tmp_path):
    # 上面的横幅加载出来，列表往下挪了一点：还是那张卡片，按它现在的位置点
    before, after = (0, 300, 540, 500), (0, 340, 540, 540)
    card = UIElement(index=1, label="橘猫视频", kind="LinearLayout", clickable=True, bounds=before)
    moved = UIElement(index=1, label="橘猫视频", kind="LinearLayout", clickable=True, bounds=after)
    harness = Harness(
        tmp_path,
        ScenePhone([(scene_png((before, "#ffcc00")), [card]), (scene_png((after, "#ffcc00")), [moved])]),
        [action(action="tap", element=1, summary="点开猫猫视频"), action(action="finish")],
    )
    await harness.run()
    assert harness.device.taps() == [("tap", 270, 440)]


async def test_unchanged_target_is_tapped_without_reading_the_screen_again(tmp_path):
    # 视频在别处播放、画面一直在变，但按钮那块没变：直接点，不多花一两秒重新读界面
    button_box, video_box = (400, 100, 520, 160), (0, 300, 540, 600)
    button = UIElement(index=1, label="关注", kind="Button", clickable=True, bounds=button_box)
    scenes = [
        (scene_png((button_box, "#ff6699"), (video_box, "#000000")), [button]),
        (scene_png((button_box, "#ff6699"), (video_box, "#ffffff")), [button]),
    ]
    harness = Harness(tmp_path, ScenePhone(scenes), [action(action="tap", element=1), action(action="finish")])
    await harness.run()
    assert harness.device.taps() == [("tap", 460, 130)]
    # 只有两步做决定时各读了一次
    assert harness.device.ui_reads == 2


async def test_coordinate_tap_on_a_playing_video_still_goes_through(tmp_path):
    # 视频画面上没有界面元素，播放时那块一直在变；重新读界面那里还是空的，照原样点
    video_box = (0, 100, 540, 400)
    title = UIElement(index=1, label="视频标题", kind="TextView", clickable=False, bounds=(0, 700, 540, 760))
    scenes = [(scene_png((video_box, "#000000")), [title]), (scene_png((video_box, "#ffffff")), [title])]
    harness = Harness(tmp_path, ScenePhone(scenes), [action(action="tap", point=[500, 200]), action(action="finish")])
    await harness.run()
    assert harness.device.taps() == [("tap", 270, 240)]


async def test_coordinate_tap_is_skipped_when_something_popped_up_there(tmp_path):
    video_box, popup_box = (0, 100, 540, 400), (60, 150, 480, 350)
    title = UIElement(index=1, label="视频标题", kind="TextView", clickable=False, bounds=(0, 700, 540, 760))
    popup = UIElement(index=1, label="开通会员", kind="Button", clickable=True, bounds=popup_box)
    scenes = [
        (scene_png((video_box, "#000000")), [title]),
        (scene_png((video_box, "#000000"), (popup_box, "#ffcc00")), [popup, title]),
    ]
    harness = Harness(tmp_path, ScenePhone(scenes), [action(action="tap", point=[500, 200]), action(action="finish")])
    await harness.run()
    assert harness.device.taps() == []
    assert "那里现在是「开通会员」" in harness.state_texts()[1]


async def test_new_request_wakes_the_session_while_it_is_watching(tmp_path):
    # 她停下来看视频时一次等 30 秒；对方在聊天里让她点准备，不能等满了才去
    harness = Harness(tmp_path, FakeDevice(), [])
    agent = harness.agent
    agent._sleep = asyncio.sleep
    agent._session = Session(
        trigger="chat", hint="", origin_stream="s", minutes_override=0, started_at=datetime.now(), started_clock=0.0
    )
    nap = asyncio.create_task(agent._nap(agent._session, 30))
    await asyncio.sleep(0.05)
    assert not nap.done()
    assert agent.add_request("点准备")
    await asyncio.wait_for(nap, 1)
    assert agent._session.requests == ["点准备"]


async def test_chat_context_only_for_the_chat_that_started_and_not_long_after(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish", summary="看了两只猫，好圆")])
    assert harness.agent.chat_context("s") == ""
    assert harness.agent.add_request("换一个") is False
    await harness.run(origin_stream="s")
    assert harness.agent.chat_context("s").startswith("【手机上的实际情况】现在没在玩手机。")
    assert harness.agent.chat_context("other") == ""
    harness.seconds += 31 * 60
    assert harness.agent.chat_context("s") == ""


async def test_new_request_leaves_time_to_carry_it_out(tmp_path):
    # 实测「去打一把路人局」交过去 44 秒后，这次就因为玩够 5 分钟结束了
    harness = Harness(tmp_path, FakeDevice(), [])
    agent = harness.agent
    agent._session = Session(
        trigger="chat",
        hint="",
        origin_stream="s",
        minutes_override=0,
        started_at=datetime.now(),
        started_clock=0.0,
        max_minutes=5,
    )
    harness.seconds = 4.8 * 60
    agent.add_request("去打一把路人局")
    assert agent._session.max_minutes == 8
    agent.add_request("再打一把", minutes=6)
    assert agent._session.max_minutes == 11
    # 不超过配置的单次上限
    agent.add_request("一直打", minutes=60)
    assert agent._session.max_minutes == 15


async def test_keep_open_apps_are_not_closed_or_left_for_the_home_screen(tmp_path):
    # 联机游戏回桌面会退出房间、关掉要重新加载一分钟（实测群友进好友房时她已经走了）
    config = PhonePlayConfig()
    config.safety.allowed_apps = [AllowedApp(package="tv.danmaku.bili", name="哔哩哔哩", keep_open=True)]
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish", summary="在房间里等了一会儿")], config=config)
    await harness.run()
    assert harness.device.actions() == [("launch", "tv.danmaku.bili")]


async def test_reopening_the_same_app_soon_resumes_where_she_left(tmp_path):
    # 实测群友一喊「回房间」她就重开一次，每次都先关掉游戏、冷启动
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish", summary="看了一会儿猫猫视频")] * 3)

    def first_app_action() -> Tuple:
        # 上次玩完锁了屏，这次先解锁；只看对应用做了什么
        return next(call for call in harness.device.actions() if call[0] in ("stop", "launch"))

    await harness.run()
    assert first_app_action() == ("stop", "tv.danmaku.bili")
    harness.device.calls.clear()
    harness.seconds += 5 * 60
    await harness.run()
    assert first_app_action() == ("launch", "tv.danmaku.bili")
    harness.device.calls.clear()
    harness.seconds += 16 * 60
    await harness.run()
    assert first_app_action() == ("stop", "tv.danmaku.bili")


@pytest.mark.parametrize("changes_at,woke_early", [(5, True), (None, False)])
async def test_long_wait_watches_the_screen_without_calling_the_model(tmp_path, changes_at, woke_early):
    # 在房间里等人时一次等 2 分钟，省下每 30 秒一次的识图调用；有人进来、开局了（画面大变）就提前醒来
    still, changed = (make_png(), []), (make_png(color="#cc3366"), [])
    scenes = [still] * (changes_at - 1) + [changed] if changes_at else [still]
    harness = Harness(
        tmp_path,
        ScenePhone(scenes),
        [action(action="wait", seconds=120, summary="等人进房间"), action(action="finish", summary="等到人开局了")],
    )
    await harness.run()
    assert len(harness.vision_messages) == 2
    # 开头打开应用后等 3 秒；没变化时等满 120 秒，画面变了第三次比对（9 秒）就醒
    assert harness.seconds == (3 + 9 if woke_early else 3 + 120)
    assert "在联机游戏的房间里（好友房、组队房间）等别人进来" in harness.vision_messages[0][0]["content"]


class SlowHomePhone(FakeDevice):
    """回桌面要等一会儿（真机上是一次网络请求）。"""

    def __init__(self) -> None:
        super().__init__()
        self.going_home = asyncio.Event()

    async def press_key(self, key) -> None:
        if key == "home":
            self.going_home.set()
            await asyncio.sleep(0.2)
        await super().press_key(key)


async def test_stopping_while_she_is_wrapping_up_does_not_get_stuck(tmp_path):
    # 实测她自己结束、正在回桌面时聊天里正好叫停，状态一直停在「正在玩」，再也开始不了
    harness = Harness(tmp_path, SlowHomePhone(), [action(action="finish", summary="看完了，回桌面歇会儿")] * 2)
    await harness.agent.start(trigger="command")
    await asyncio.wait_for(harness.device.going_home.wait(), 5)
    assert await harness.agent.stop("在聊天中放下了手机") is True
    assert harness.agent.state == "idle" and harness.agent._session is None
    assert ("key", "home") in harness.device.actions()
    await harness.agent.start(trigger="command")
    await harness.wait_done()


async def test_switching_to_a_keep_open_game_counts_at_the_end(tmp_path):
    # 先开普通应用、再切去联机游戏进了房间：结束时按游戏算，不回桌面（回桌面会退出房间）
    config = PhonePlayConfig()
    config.safety.allowed_apps = [
        AllowedApp(package="tv.danmaku.bili", name="哔哩哔哩"),
        AllowedApp(package="com.xingin.xhs", name="斗地主", keep_open=True),
    ]
    device = FakeDevice(foregrounds=["tv.danmaku.bili"])
    device.default_foreground = "com.xingin.xhs"
    harness = Harness(
        tmp_path,
        device,
        [action(action="launch", app="com.xingin.xhs"), action(action="finish", summary="在房间里等人，先这样")],
        config=config,
    )
    await harness.run()
    assert ("key", "home") not in harness.device.actions()
    assert harness.agent._last_package == "com.xingin.xhs"


async def test_action_is_skipped_if_the_foreground_left_allowed_apps_while_deciding(tmp_path):
    # 模型想的这几秒里广告跳到了别的应用：做之前再看一次前台，不在白名单里就不做
    harness = Harness(
        tmp_path,
        FakeDevice(foregrounds=["tv.danmaku.bili", "com.android.settings"]),
        [action(action="swipe", direction="up", summary="刷下一个"), action(action="finish")],
    )
    await harness.run()
    assert not any(call[0] == "swipe" for call in harness.device.actions())
    assert "做之前前台变成了 com.android.settings" in harness.state_texts()[1]


async def test_comment_needs_text_input_allowed(tmp_path):
    config = interaction_config()
    config.safety.allow_text_input = False
    harness = Harness(
        tmp_path,
        FakeDevice(elements=interaction_elements(), input_focus=True),
        [action(action="comment", element=3, text="好圆"), action(action="finish")],
        config=config,
    )
    await harness.run()
    assert harness.device.typed == ""
    assert "配置不允许输入文字，不能写评论" in harness.state_texts()[1]
    # 关了文字输入就不教她评论
    assert '"action":"comment"' not in harness.vision_messages[0][0]["content"]


async def test_usage_is_saved_while_playing_not_only_at_the_end(tmp_path):
    # 之前次数、步数只在结束时保存，玩到一半宿主崩溃就丢了
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish", summary="看了一会儿猫猫视频")])
    seen: List[Dict[str, Any]] = []
    original = harness.vision

    async def vision(messages):
        seen.append(json.loads((tmp_path / "usage.json").read_text(encoding="utf-8")))
        return await original(messages)

    harness.agent._vision = vision
    await harness.run()
    assert seen[0]["sessions"] == 1 and seen[0]["steps"] == 1


async def test_changing_device_config_mid_session_ends_it(tmp_path):
    # 接着玩会操作旧手机、截图和回桌面却发给新手机
    harness = Harness(tmp_path, FakeDevice(), [action(action="wait", seconds=5), action(action="finish")])
    original = harness.vision

    async def vision(messages):
        harness.config.device.serial = "127.0.0.1:5555"
        return await original(messages)

    harness.agent._vision = vision
    record = await harness.run()
    assert record.end_reason == "设备配置改了，这次先停下"
    assert len(harness.vision_messages) == 1


async def test_earlier_requests_stay_visible_after_history_scrolls(tmp_path):
    # 「退出房间，再打一局」这种要求，操作记录一长就被挤掉了
    config = PhonePlayConfig()
    config.play.history_steps = 1
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [action(action="wait", seconds=1)] * 3 + [action(action="finish")],
        config=config,
    )
    original = harness.vision

    async def vision(messages):
        if len(harness.vision_messages) == 0:
            harness.agent.add_request("退出房间，再打一局")
        return await original(messages)

    harness.agent._vision = vision
    await harness.run()
    assert "对方刚在聊天里让你：退出房间，再打一局" in harness.state_texts()[1]
    assert "对方这次在聊天里提过：退出房间，再打一局。还没做完的接着做" in harness.state_texts()[3]


async def test_cancelled_save_never_overwrites_newer_data(tmp_path):
    # 叫停时保存被取消，写文件的线程却停不下来；它晚写完时不能把后面存的新记录盖回旧的
    path = tmp_path / "usage.json"
    store = UsageStore(path)
    started, release = threading.Event(), threading.Event()
    original = store._write

    def slow_first_write(version: int, payload: Dict[str, Any]) -> None:
        if version == 1:
            started.set()
            release.wait(5)
        original(version, payload)

    store._write = slow_first_write
    store.add_session("2026-11-01")
    first = asyncio.create_task(store.save())
    await asyncio.to_thread(started.wait, 5)
    first.cancel()
    await asyncio.wait({first})
    store.add_session("2026-11-01")
    store.add_step("2026-11-01")
    await store.save()
    release.set()
    await asyncio.sleep(0.2)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert (data["sessions"], data["steps"]) == (2, 1)


async def test_unknown_foreground_right_before_acting_skips_the_action(tmp_path):
    # 刚才认得出、做之前认不出（切应用、系统界面）就先不做
    device = FakeDevice(foregrounds=["tv.danmaku.bili", ""])
    harness = Harness(tmp_path, device, [action(action="swipe", direction="up"), action(action="finish")])
    await harness.run()
    assert not any(call[0] == "swipe" for call in harness.device.actions())
    assert "做之前看不出现在在哪个应用（这台手机之前能认出来" in harness.state_texts()[1]


async def test_foreground_lost_stays_blocked_until_recognised_again(tmp_path):
    # 之前只比较这一步开始时和执行前：下一步两次都是空的又会照常操作。现在认出来过的连接，空着就一直不操作
    device = FakeDevice(
        foregrounds=["tv.danmaku.bili", "tv.danmaku.bili", "", "", "tv.danmaku.bili", "tv.danmaku.bili"]
    )
    harness = Harness(
        tmp_path,
        device,
        [action(action="swipe", direction="up", summary="刷下一个")] * 2 + [action(action="finish")],
    )
    record = await harness.run()
    # 第 1 步正常滑；第 2、3 步认不出前台：不问模型、不操作；第 4 步认出来了，按新画面重新决定
    swipes = [call for call in harness.device.actions() if call[0] == "swipe"]
    assert len(swipes) == 2 and len(harness.vision_messages) == 3
    logs = [log["result"] for log in harness.agent.status()["logs"]]
    assert sum("暂时没认出" in result for result in logs) == 2
    assert record.end_reason == "自己不想玩了"


async def test_foreground_never_recognised_ends_with_a_clear_reason(tmp_path):
    # 一直认不出、设备也没说不支持：不能当成「不支持」放行，按连续失败结束并说明原因
    device = FakeDevice()
    device.default_foreground = ""
    harness = Harness(tmp_path, device, [action(action="swipe", direction="up")] * 5)
    record = await harness.run()
    assert harness.vision_messages == [] and not any(call[0] == "swipe" for call in harness.device.actions())
    assert record.end_reason.startswith("连续 3 步操作失败（最后一次：看不出现在在哪个应用（还没确认")


class NoForegroundPhone(FakeDevice):
    """系统给不出前台信息的设备（比如 dumpsys 里根本没有焦点字段）。"""

    async def foreground(self):
        return Foreground("", unsupported=True)


async def test_device_that_cannot_report_foreground_still_works(tmp_path):
    harness = Harness(tmp_path, NoForegroundPhone(), [action(action="swipe", direction="up"), action(action="finish")])
    await harness.run()
    assert any(call[0] == "swipe" for call in harness.device.actions())
    logs = [log["summary"] for log in harness.agent.status()["logs"]]
    assert logs.count("这台手机给不出前台应用") == 1


class HangingPhone(FakeDevice):
    """断开后连接请求一直不返回（真机上要等到请求超时）。"""

    async def ensure_ready(self) -> None:
        await asyncio.Event().wait()


async def test_reconnect_wait_is_bounded_even_if_a_request_hangs(tmp_path, monkeypatch):
    import phone_play.agent as agent_module

    monkeypatch.setattr(agent_module, "_RECONNECT_INTERVAL", 0.4)
    harness = Harness(tmp_path, HangingPhone(), [])
    started = time.perf_counter()
    with pytest.raises(DeviceError, match="连接手机超时"):
        await asyncio.wait_for(harness.agent._connect(harness.device, 1.0), 5)
    # 每次连接最多等剩下的时间，总共不超过截止时间
    assert time.perf_counter() - started < 2.5


async def test_status_and_chat_follow_the_app_she_switched_to(tmp_path):
    # 切到别的应用后，聊天里的近况、WebUI 也要跟着换，不然又在说没在发生的事
    device = FakeDevice(foregrounds=["tv.danmaku.bili"])
    device.default_foreground = "com.xingin.xhs"
    harness = Harness(
        tmp_path,
        device,
        [action(action="launch", app="com.xingin.xhs", summary="去逛小红书"), action(action="finish")],
    )
    seen: List[str] = []
    original = harness.vision

    async def vision(messages):
        seen.append(harness.agent.status()["app"] + "|" + harness.agent.activity_text())
        return await original(messages)

    harness.agent._vision = vision
    await harness.run()
    assert seen[0].startswith("哔哩哔哩|正在用手机：哔哩哔哩")
    assert seen[1].startswith("小红书|正在用手机：小红书")


class RefusingPhone(FakeDevice):
    """连接时报的是重试也没用的错误。"""

    def __init__(self, error: DeviceError) -> None:
        super().__init__()
        self.error = error

    async def ensure_ready(self) -> None:
        self.calls.append(("ready",))
        raise self.error


@pytest.mark.parametrize(
    "error,state,chat",
    [
        (DeviceConfigError("找不到 adb：adb"), "config", "手机的设置有问题，现在玩不了"),
        (
            DeviceNeedsUserError("iPhone 锁屏了，自动解锁没成功", "needs_unlock"),
            "needs_unlock",
            "手机锁着屏，得先解锁才能玩",
        ),
    ],
)
async def test_errors_that_retrying_cannot_fix_are_not_retried(tmp_path, error, state, chat):
    harness = Harness(tmp_path, RefusingPhone(error), [])
    await harness.agent.start(trigger="chat", origin_stream="s")
    with pytest.raises(PlayRefused) as refused:
        await harness.agent.wait_for_plan(5)
    await harness.wait_done()
    assert harness.device.calls.count(("ready",)) == 1 and harness.seconds == 0
    assert refused.value.chat_text == chat
    assert harness.agent.status()["connection"]["state"] == state
    harness.assert_not_counted()


def log_lines(harness: Harness) -> List[Tuple[str, str]]:
    return [(log["summary"], log["result"]) for log in harness.agent.status()["logs"]]


async def test_locked_phone_is_unlocked_at_start_and_locked_again_after(tmp_path):
    device = FakeDevice()
    device.locked = True
    harness = Harness(tmp_path, device, [action(action="finish", summary="看了一会儿")])
    await harness.run()
    actions = harness.device.actions()
    assert actions[0] == ("unlock",)
    assert actions[-2:] == [("key", "home"), ("lock",)]
    assert ("已连接 测试手机", "手机锁着屏，已自动解开") in log_lines(harness)
    assert ("锁屏了", "下次玩时自动解开") in log_lines(harness)
    assert harness.device.locked


class LockedMidwayPhone(FakeDevice):
    """滑动后手机锁屏了（模拟有人按了电源键、到了自动锁定时间）。"""

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        await super().swipe(x1, y1, x2, y2, duration_ms)
        self.locked = True


async def test_phone_locked_midway_is_unlocked_before_the_next_look(tmp_path):
    harness = Harness(
        tmp_path,
        LockedMidwayPhone(),
        [action(action="swipe", direction="up", summary="刷下一个"), action(action="finish", summary="刷完了")],
    )
    record = await harness.run()
    calls = harness.device.calls
    unlock = calls.index(("unlock",))
    # 先解锁再截图看画面，她看到的不是锁屏
    assert calls[unlock + 1] == ("screenshot",)
    assert calls.index(("swipe", 270, 600, 270, 120)) < unlock
    assert (record.steps, record.end_reason) == (2, "自己不想玩了")
    assert ("手机锁屏了，已自动解开", "") in log_lines(harness)
    assert "第2步前：手机锁屏了，已经帮你解开" in harness.state_texts()[1]
    assert harness.device.actions()[-1] == ("lock",)


async def test_phone_locked_midway_that_cannot_be_unlocked_ends_the_session(tmp_path):
    device = LockedMidwayPhone()
    device.unlock_fails = True
    harness = Harness(
        tmp_path,
        device,
        [action(action="swipe", direction="up", summary="刷下一个"), action(action="finish", summary="不会走到这")],
    )
    record = await harness.run()
    assert record.end_reason == "出错：测试手机锁着屏，自动解锁没成功"
    # 解不开时不再问模型，也不会对着锁屏乱点；本来就锁着，收尾不再锁
    assert len(harness.vision_messages) == 1
    assert ("lock",) not in harness.device.actions()
    assert harness.agent.status()["connection"]["state"] == "needs_unlock"


async def test_phone_that_could_not_be_unlocked_is_not_locked_after_play(tmp_path):
    # 上次解不开（多半设了锁屏密码），人手动解开后再玩：玩完不锁，免得下次又解不开
    device = FakeDevice()
    device.locked, device.unlock_fails = True, True
    harness = Harness(tmp_path, device, [action(action="finish", summary="看了一会儿")] * 2)
    await harness.agent.start(trigger="chat", origin_stream="s")
    with pytest.raises(PlayRefused):
        await harness.agent.wait_for_plan(5)
    await harness.wait_done()
    assert harness.agent.status()["connection"]["state"] == "needs_unlock"
    harness.assert_not_counted()

    device.locked = False
    await harness.run()
    assert ("lock",) not in device.actions()
    assert any(summary == "这次不锁屏" for summary, _ in log_lines(harness))

    # 后来又能自动解开了（去掉了锁屏密码），之后照常锁屏
    device.locked, device.unlock_fails = True, False
    await harness.run()
    assert device.actions()[-1] == ("lock",)


async def test_lock_after_can_be_disabled_and_skips_apps_that_stay_open(tmp_path):
    config = PhonePlayConfig()
    config.play.lock_after = False
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=config)
    await harness.run()
    assert ("lock",) not in harness.device.actions()

    # 联机游戏设了玩完留在里面：锁屏也会掉线，不锁
    config = PhonePlayConfig()
    config.safety.allowed_apps = [AllowedApp(package="tv.danmaku.bili", name="哔哩哔哩", go_home_after="no")]
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=config)
    await harness.run()
    assert harness.device.actions()[-1] == ("launch", "tv.danmaku.bili")


class AndroidPhone(FakeDevice):
    """安卓：不查锁屏，也不锁屏。"""

    supports_lock = False

    async def unlock_if_locked(self) -> bool:
        self.calls.append(("unlock_check",))
        return False


async def test_android_is_never_locked_after_play(tmp_path):
    harness = Harness(tmp_path, AndroidPhone(), [action(action="finish", summary="看了一会儿")])
    await harness.run()
    assert ("lock",) not in harness.device.actions()
    assert not any("锁" in summary for summary, _ in log_lines(harness))


class SilentLockPhone(FakeDevice):
    """锁屏请求发出去了却没锁上。"""

    async def lock(self) -> bool:
        self.calls.append(("lock",))
        return False


async def test_lock_that_did_not_take_is_reported_once(tmp_path):
    # 按了关屏屏幕还亮着：不能写成「锁屏了」，说一次，之后不再试
    harness = Harness(tmp_path, SilentLockPhone(), [action(action="finish", summary="看了一会儿")] * 2)
    await harness.run()
    assert ("这台手机锁不了屏", "按了关屏，屏幕还亮着，之后玩完不再试") in log_lines(harness)
    assert not any(summary == "锁屏了" for summary, _ in log_lines(harness))
    await harness.run()
    assert harness.device.actions().count(("lock",)) == 1


async def test_declined_plan_relocks_only_a_phone_it_unlocked(tmp_path):
    plan = json.dumps({"app": "", "reason": "手机上没有王者荣耀"}, ensure_ascii=False)
    device = FakeDevice()
    device.locked = True
    harness = Harness(tmp_path, device, [], plan=plan)
    await harness.agent.start(trigger="chat", origin_stream="s")
    with pytest.raises(PlayRefused):
        await harness.agent.wait_for_plan(5)
    await harness.wait_done()
    # 是她解开的，没玩成也锁回去
    assert device.actions() == [("unlock",), ("lock",)]


async def test_watching_the_screen_from_webui_does_not_unlock(tmp_path):
    device = FakeDevice()
    device.locked = True
    harness = Harness(tmp_path, device, [])
    await harness.agent.refresh_frame()
    await harness.agent.chat_screenshot()
    assert device.locked and ("unlock",) not in device.calls


class StuckConnectingPhone(FakeDevice):
    """连接请求一直不返回。"""

    async def ensure_ready(self) -> None:
        self.calls.append(("ready",))
        await asyncio.Event().wait()


async def test_stop_cancels_the_startup_connection_wait(tmp_path):
    harness = Harness(tmp_path, StuckConnectingPhone(), [])
    await harness.agent.start(trigger="chat", origin_stream="s")
    await asyncio.sleep(0.05)
    assert harness.agent.state == "starting"
    assert await harness.agent.stop("在 WebUI 中停止") is True
    assert harness.agent.state == "idle" and harness.agent._task.done()
    harness.assert_not_counted()


def gated_vision(harness: Harness, gate: asyncio.Event, deciding: asyncio.Event):
    """第一次做决定时停住，等测试放行；之后照常按脚本回答。"""
    original = harness.vision

    async def vision(messages):
        if not harness.vision_messages:
            deciding.set()
            await gate.wait()
        return await original(messages)

    return vision


async def test_takeover_discards_the_decision_that_comes_back_late(tmp_path):
    # 接管期间模型才返回：按旧画面想的点击不能执行；交还后先重新看画面再决定
    harness = Harness(
        tmp_path,
        FakeDevice(
            elements=[UIElement(index=1, label="橘猫视频", kind="Cell", clickable=True, bounds=(0, 100, 540, 300))]
        ),
        [
            action(action="tap", element=1, summary="点开猫猫视频"),
            action(action="finish", summary="交还后看了一会儿猫猫"),
        ],
    )
    gate, deciding = asyncio.Event(), asyncio.Event()
    harness.agent._vision = gated_vision(harness, gate, deciding)
    await harness.agent.start(trigger="command")
    await asyncio.wait_for(deciding.wait(), 5)
    assert harness.agent.takeover() is True
    assert harness.agent.takeover() is False
    gate.set()
    await asyncio.sleep(0.05)
    # 接管中：不点、不问模型，状态显示你在操作
    assert not any(call[0] == "tap" for call in harness.device.actions())
    assert len(harness.vision_messages) == 1 and harness.agent.status()["controller"] == "user"
    assert harness.agent.release() is True
    assert harness.agent.release() is False
    await harness.wait_done()
    assert not any(call[0] == "tap" for call in harness.device.actions())
    assert "对方接管过手机，刚交还给你" in harness.state_texts()[1]
    summaries = [log["summary"] for log in harness.agent.status()["logs"]]
    assert "接管期间想好的这一步作废了" in summaries


class TakeoverDuringReconnectPhone(FlakyDevice):
    """断开后重连的时候，对方正好接管了手机。"""

    def __init__(self, agent_ref: List[PhoneAgent]) -> None:
        super().__init__(drop_at=2, down_checks=1)
        self.agent_ref = agent_ref

    async def ensure_ready(self) -> None:
        if self.shots >= self.drop_at and self.agent_ref[0].controller == "bot" and self.down_checks:
            self.agent_ref[0].takeover()
        await super().ensure_ready()


async def test_reconnect_finishing_during_takeover_does_not_touch_the_phone(tmp_path):
    agent_ref: List[PhoneAgent] = []
    harness = Harness(
        tmp_path,
        TakeoverDuringReconnectPhone(agent_ref),
        [action(action="wait", seconds=1), action(action="finish", summary="交还之后接着看了一会儿")],
    )
    agent_ref.append(harness.agent)
    await harness.agent.start(trigger="command")
    await asyncio.sleep(0.05)
    # 重连完了，但你还在操作：不截图做决定、不发操作
    assert harness.agent.controller == "user" and len(harness.vision_messages) == 1
    assert harness.device.actions() == [("stop", "tv.danmaku.bili"), ("launch", "tv.danmaku.bili")]
    harness.agent.release()
    await asyncio.wait_for(harness.agent._task, 5)
    assert harness.store.history[-1].end_reason == "自己不想玩了"


async def test_takeover_blocks_starting_and_auto_play(tmp_path):
    config = PhonePlayConfig()
    config.auto_play.enabled = True
    config.auto_play.active_hours = "00:00-00:00"
    config.auto_play.play_probability = 1.0
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=config)
    assert harness.agent.takeover() is True
    with pytest.raises(PlayRefused, match="人工接管") as refused:
        await harness.agent.start(trigger="chat")
    assert refused.value.chat_text == "对方正在用这台手机，现在不能玩"
    await harness.agent._auto_decide()
    assert harness.agent.state == "idle" and harness.device.actions() == []
    harness.agent.release()
    await harness.agent._auto_decide()
    await harness.wait_done()
    assert harness.store.history[-1].trigger == "自己想玩"


async def test_takeover_time_is_not_play_time_but_has_a_wall_clock_cap(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish", summary="看了一会儿就放下了")])
    gate, deciding = asyncio.Event(), asyncio.Event()
    harness.agent._vision = gated_vision(harness, gate, deciding)
    await harness.agent.start(trigger="command")
    await asyncio.wait_for(deciding.wait(), 5)
    harness.agent.takeover()
    harness.seconds += 10 * 60
    harness.agent.release()
    # 接管的 10 分钟不算进这次的游玩时长（计划 5 分钟）
    assert harness.agent.status()["elapsed_minutes"] < 1
    session = harness.agent._session
    harness.agent.takeover()
    # 墙钟上限：计划时长 5 分钟再加 30 分钟，过了就不再等交还
    harness.seconds = session.started_clock + (5 + 30) * 60 + 1
    assert await harness.agent._wait_running(session) is False
    assert session.end_reason == "暂停或接管太久，这次先结束"
    gate.set()
    await harness.agent.stop("测试结束")


async def test_ending_while_user_has_control_does_not_go_home(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")])
    gate, deciding = asyncio.Event(), asyncio.Event()
    harness.agent._vision = gated_vision(harness, gate, deciding)
    await harness.agent.start(trigger="command")
    await asyncio.wait_for(deciding.wait(), 5)
    harness.agent.takeover()
    assert await harness.agent.stop("在 WebUI 中停止") is True
    # 你接管着手机时结束：不替你回桌面，控制权还在你手里
    assert ("key", "home") not in harness.device.actions()
    assert harness.agent.state == "idle" and harness.agent.controller == "user"


def two_apps(**overrides) -> PhonePlayConfig:
    config = PhonePlayConfig()
    config.safety.allowed_apps = [
        AllowedApp(package="tv.danmaku.bili", name="哔哩哔哩", **overrides),
        AllowedApp(package="com.xingin.xhs", name="小红书"),
    ]
    return config


@pytest.mark.parametrize(
    "overrides,global_fresh,stopped",
    [
        ({"reopen_on_start": "no"}, True, False),
        ({"reopen_on_start": "yes"}, False, True),
        ({"keep_open": True}, True, False),
        # 单独设了 reopen_on_start 就以它为准，旧的 keep_open 不再起作用
        ({"keep_open": True, "reopen_on_start": "yes"}, True, True),
        ({}, True, True),
        ({}, False, False),
    ],
)
async def test_reopen_on_start_per_app(tmp_path, overrides, global_fresh, stopped):
    config = two_apps(**overrides)
    config.play.fresh_start = global_fresh
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=config)
    await harness.run()
    assert (("stop", "tv.danmaku.bili") in harness.device.actions()) is stopped


@pytest.mark.parametrize(
    "overrides,global_home,went_home",
    [
        ({"go_home_after": "no"}, True, False),
        ({"go_home_after": "yes"}, False, True),
        ({"keep_open": True}, True, False),
        ({"keep_open": True, "go_home_after": "yes"}, True, True),
        ({}, False, False),
    ],
)
async def test_go_home_after_per_app(tmp_path, overrides, global_home, went_home):
    config = two_apps(**overrides)
    config.play.go_home_after = global_home
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=config)
    await harness.run()
    assert (("key", "home") in harness.device.actions()) is went_home


async def test_app_max_minutes_cannot_exceed_global_limit(tmp_path):
    # PLAN 打算玩 5 分钟；应用上限 3 分钟就按 3 分钟
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=two_apps(max_minutes=3))
    await harness.agent.start(trigger="command")
    assert (await harness.agent.wait_for_plan(5)).minutes == 3
    await harness.wait_done()
    # 应用上限写得比全局还大，也不超过全局（一起玩时是 together_minutes）
    config = two_apps(max_minutes=200)
    config.play.together_minutes = 20
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish")], config=config)
    await harness.agent.start(trigger="command", together=True)
    assert (await harness.agent.wait_for_plan(5)).minutes == 20
    await harness.wait_done()


async def test_step_interval_follows_the_app_she_is_in(tmp_path):
    config = two_apps(step_interval_seconds=10)
    config.play.step_interval_seconds = 3
    device = FakeDevice(foregrounds=["tv.danmaku.bili", "tv.danmaku.bili"])
    device.default_foreground = "com.xingin.xhs"
    harness = Harness(
        tmp_path,
        device,
        [
            action(action="swipe", direction="up"),
            action(action="launch", app="com.xingin.xhs"),
            action(action="finish"),
        ],
        config=config,
    )
    await harness.run()
    # 打开 B 站后等 10 秒，在 B 站滑一下等 10 秒；切到小红书后用全局的 3 秒
    assert harness.seconds == 10 + 10 + 3


async def test_history_records_every_app_she_used(tmp_path):
    # 之前跨应用后整次活动都记在起始应用名下，聊天里也这么说
    device = FakeDevice(foregrounds=["tv.danmaku.bili"])
    device.default_foreground = "com.xingin.xhs"
    harness = Harness(
        tmp_path,
        device,
        [
            action(action="wait", seconds=30, thought="看看 B 站"),
            action(action="launch", app="com.xingin.xhs", summary="去逛小红书"),
            action(action="wait", seconds=30, thought="看看穿搭"),
            action(action="finish", summary="先刷了 B 站，又去小红书看了穿搭"),
        ],
        config=two_apps(),
    )
    record = await harness.run(origin_stream="s")
    assert record.app == "哔哩哔哩" and record.last_app == "小红书"
    assert [item["name"] for item in record.apps] == ["哔哩哔哩", "小红书"]
    assert all(item["seconds"] > 0 for item in record.apps)
    assert record.places_text().startswith("哔哩哔哩（约 1 分钟）、小红书（约")
    assert "在哔哩哔哩（约 1 分钟）、小红书（约" in harness.agent.activity_text()
    saved = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert saved["history"][-1]["last_app"] == "小红书"


async def test_old_and_newer_history_files_still_load(tmp_path):
    path = tmp_path / "usage.json"
    old = {
        "started_at": "2026-10-29T20:37:11",
        "ended_at": "2026-10-29T20:42:20",
        "trigger": "聊天中决定",
        "app": "哔哩哔哩",
        "goal": "看猫猫",
        "summary": "看了两只猫",
        "steps": 14,
        "end_reason": "自己不想玩了",
    }
    newer = dict(old, apps=[{"name": "哔哩哔哩", "package": "tv.danmaku.bili", "seconds": 60}], some_future_field=1)
    path.write_text(
        json.dumps({"date": "2026-10-29", "sessions": 2, "steps": 20, "history": [old, newer]}), encoding="utf-8"
    )
    store = UsageStore(path)
    await store.load()
    # 旧格式、带新字段的都照常读出来，不当成损坏清空
    assert store.recovered == "" and list(tmp_path.glob("usage.json.broken-*")) == []
    assert [record.places_text() for record in store.history] == ["哔哩哔哩", "哔哩哔哩"]
    assert store.history[0].apps == [] and store.history[0].last_app == ""


async def test_session_call_budget_ends_the_session_in_order(tmp_path):
    config = PhonePlayConfig()
    config.play.max_model_calls_per_session = 3
    harness = Harness(tmp_path, FakeDevice(), [action(action="wait", seconds=5, thought="看看")] * 5, config=config)
    record = await harness.run()
    # 决定玩什么 1 次 + 看屏幕 2 次，第 3 次看屏幕前就停：不再调用模型
    assert record.end_reason == "这次的模型调用预算用完了（3 次）"
    assert len(harness.vision_messages) == 2
    assert record.stats["calls"] == {"planner": 1, "vision": 2, "failed": 0}


async def test_daily_call_budget_blocks_starting(tmp_path):
    config = PhonePlayConfig()
    config.play.daily_model_call_limit = 2
    harness = Harness(tmp_path, FakeDevice(), [action(action="wait", seconds=5)] * 3, config=config)
    record = await harness.run()
    assert record.end_reason == "今天的模型调用预算用完了（2 次）"
    with pytest.raises(PlayRefused, match="模型调用预算"):
        await harness.agent.start(trigger="chat")
    saved = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert saved["model_calls"] == 2


async def test_failed_calls_count_and_only_real_token_usage_is_summed(tmp_path):
    replies = [
        AgentError("视觉模型调用失败：超时"),
        ModelReply(action(action="wait", seconds=5), prompt_tokens=3600, completion_tokens=60, total_tokens=3660),
        action(action="finish", summary="看了一会儿猫猫视频就放下了"),
    ]
    harness = Harness(tmp_path, FakeDevice(), [])

    async def vision(messages):
        harness.vision_messages.append(messages)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    harness.agent._vision = vision
    record = await harness.run()
    stats = record.stats
    # 失败的那次也算一次调用；token 只累计宿主给出的那一次，另外两次（含决定玩什么）记为没提供
    assert stats["calls"] == {"planner": 1, "vision": 3, "failed": 1}
    assert stats["tokens"] == {"total": 3660, "prompt": 3600, "completion": 60, "missing": 3}
    assert set(stats["seconds"]) == {"screenshot", "ui", "model", "action"}


async def test_token_usage_is_unavailable_when_the_host_never_reports_it(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [action(action="finish", summary="看了一会儿猫猫视频就放下了")])
    record = await harness.run()
    assert record.stats["tokens"]["total"] is None and record.stats["tokens"]["missing"] == 2


def colors(*names: str):
    return [(scene_png(((0, 0, 540, 1200), name)), []) for name in names]


async def test_tapping_the_same_spot_without_effect_is_stuck(tmp_path):
    harness = Harness(tmp_path, FakeDevice(), [action(action="tap", point=[500, 500], summary="点这里")] * 8)
    record = await harness.run()
    # 第 3 次起提示词里提醒她换做法；连续 5 次画面都没变就停，不再浪费调用
    assert record.end_reason == "卡住了：连续 5 次点击 (500, 500)，画面一直没变"
    assert len(harness.vision_messages) == 5


async def test_turning_pages_with_the_same_tap_is_not_stuck(tmp_path):
    # 看小说：一直点屏幕右侧翻页，动作一样，但每次画面都变了
    scenes = colors("#111111", "#333333", "#555555", "#777777", "#999999", "#bbbbbb", "#dddddd", "#ffffff")
    harness = Harness(
        tmp_path,
        ScenePhone(scenes),
        [action(action="tap", point=[900, 500], summary="翻页")] * 7 + [action(action="finish", summary="读了七页")],
    )
    record = await harness.run()
    assert record.end_reason == "自己不想玩了"


async def test_bouncing_between_two_pages_is_stuck(tmp_path):
    scenes = colors(*["#336699", "#ff6699"] * 5)
    replies = [action(action="tap", point=[500, 300], summary="点进去"), action(action="back", summary="返回")] * 5
    harness = Harness(tmp_path, ScenePhone(scenes), replies)
    record = await harness.run()
    assert record.end_reason == "卡住了：最近 6 步一直在两个画面之间来回切换"
    assert len(harness.vision_messages) == 6


async def test_watching_a_video_is_not_stuck(tmp_path):
    # 视频暂停在同一个画面、连着等很多次：等待不算无效操作
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [action(action="wait", seconds=20, thought="接着看")] * 7
        + [action(action="finish", summary="看完了一个长视频")],
    )
    record = await harness.run()
    assert record.end_reason == "自己不想玩了"


async def test_the_same_failure_coming_back_is_stuck(tmp_path):
    # 中间夹着成功的步骤，连续出错计数会清零；同一种失败反复出现也要停
    bad = action(action="launch", app="com.example.nope", summary="打开别的应用")
    harness = Harness(tmp_path, FakeDevice(), [bad, action(action="wait", seconds=5)] * 4)
    record = await harness.run()
    assert record.end_reason.startswith(
        "卡住了：最近 7 步里同一个问题出现了 4 次：「com.example.nope」不在可以打开的应用里"
    )
    assert len(harness.vision_messages) == 7


def novel_seen(detail: str = "", certain: bool = True) -> Dict[str, Any]:
    seen: Dict[str, Any] = {"type": "novel", "title": "田园小当家", "certain": certain}
    if detail:
        seen["detail"] = detail
    return seen


async def test_content_seen_on_screen_is_remembered_with_its_source(tmp_path):
    memory = MemoryStore(tmp_path / "memory.json")
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [
            action(action="wait", seconds=20, seen=novel_seen("第12章")),
            # 这次没认出章节：保留上次的第 12 章，不用空的盖掉
            action(action="wait", seconds=20, seen=novel_seen()),
            # 没说确定的不记
            action(action="wait", seconds=20, seen={"type": "novel", "title": "大概是另一本", "certain": False}),
            action(action="finish", summary="读了几页田园小当家"),
        ],
    )
    harness.agent._memory = memory
    await harness.run()
    items = memory.items()
    assert [(item.kind, item.title, item.detail, item.app, item.source) for item in items] == [
        ("content", "田园小当家", "第12章", "哔哩哔哩", "screen")
    ]
    # 画面上的东西不会变成对方的兴趣
    assert not any(item.kind == "interest" for item in items)
    saved = json.loads((tmp_path / "memory.json").read_text(encoding="utf-8"))
    assert saved["items"][0]["title"] == "田园小当家"


async def test_unfinished_goal_is_offered_next_time_and_cleared_when_done(tmp_path):
    memory = MemoryStore(tmp_path / "memory.json")
    memory.remember_interest("对方喜欢看种田小说", datetime.now())
    config = PhonePlayConfig()
    config.play.max_steps_per_session = 1
    harness = Harness(tmp_path, FakeDevice(), [action(action="wait", seconds=5)], config=config)
    harness.agent._memory = memory
    record = await harness.run()
    assert record.end_reason == "达到单次 1 步的上限"
    unfinished = [item for item in memory.items() if item.kind == "unfinished"]
    assert [(item.title, item.detail) for item in unfinished] == [("看猫猫视频", "达到单次 1 步的上限")]

    # 下次决定玩什么时想得起来：没做完的放最前，并分清来源
    config.play.max_steps_per_session = 80
    harness.responses = [action(action="finish", summary="接着看完了猫猫视频")]
    await harness.run()
    prompt = harness.plan_prompts[-1]
    assert "上次没做完：在哔哩哔哩里看猫猫视频（达到单次 1 步的上限）" in prompt
    assert "对方说过：对方喜欢看种田小说" in prompt
    # 她自己玩够了：算做完，清掉
    assert not any(item.kind == "unfinished" for item in memory.items())


async def test_broken_memory_file_is_backed_up_and_old_fields_are_ignored(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text(
        '{"items": [{"id": "a1", "kind": "interest", "title": "喜欢猫", "future_field": 1}]}', encoding="utf-8"
    )
    memory = MemoryStore(path)
    await memory.load()
    assert memory.recovered == "" and [item.title for item in memory.items()] == ["喜欢猫"]
    path.write_text("{坏掉的", encoding="utf-8")
    broken = MemoryStore(path)
    await broken.load()
    assert "已损坏" in broken.recovered and broken.items() == []
    assert len(list(tmp_path.glob("memory.json.broken-*"))) == 1


class VideoPhone(FakeDevice):
    """放着视频的手机：每次截图画面都不一样；可以在某个时刻跳一次广告（前台换成别的应用）。"""

    def __init__(self) -> None:
        super().__init__()
        self.clock = lambda: 0.0
        self.ad_at: Optional[float] = None
        self.frames = 0

    async def screenshot(self) -> bytes:
        self.frames += 1
        self.png = make_png(color="#111111" if self.frames % 2 else "#eeeeee")
        return await super().screenshot()

    async def foreground_package(self) -> str:
        if self.ad_at is not None and self.clock() >= self.ad_at:
            self.ad_at = None
            return "com.ad.app"
        return self.default_foreground


@pytest.mark.parametrize("ad_at,expected", [(None, 3 + 90), (10, 3 + 10)])
async def test_watching_a_long_video_is_not_woken_by_the_changing_picture(tmp_path, ad_at, expected):
    # 视频画面一直在变：按画面比对的长等待 3 秒就醒，长视频根本看不下去
    device = VideoPhone()
    harness = Harness(
        tmp_path,
        device,
        [action(action="wait", seconds=90, video="long", summary="看现场"), action(action="finish", summary="看完了")],
    )
    device.clock = lambda: harness.seconds
    device.ad_at = ad_at
    await harness.run()
    # 看满 90 秒才再问她；中途点到广告跳去了别的应用就提前醒来
    assert harness.seconds == expected
    # 看视频期间不截图比对，只每隔几秒看一眼前台
    assert device.frames == 2
    system_text = harness.vision_messages[0][0]["content"]
    assert '{"action":"wait","seconds":20,"video":"short"}' in system_text
    assert "短视频（竖屏、一两分钟以内，一条接一条）" in system_text


def kitten_seen(title: str = "【要乐奈】唱春日影 吉他弹唱") -> Dict[str, Any]:
    return {"type": "video", "title": title, "certain": True}


SONG_CARD = UIElement(
    index=1, label="【要乐奈】唱春日影 吉他弹唱", kind="Cell", clickable=True, bounds=(0, 100, 270, 400)
)
KITTEN_CARD = UIElement(index=2, label="小奶猫第一次睁眼", kind="Cell", clickable=True, bounds=(270, 100, 540, 400))


async def test_videos_she_watched_are_not_opened_again(tmp_path):
    memory = MemoryStore(tmp_path / "memory.json")
    harness = Harness(
        tmp_path,
        FakeDevice(elements=[SONG_CARD, KITTEN_CARD]),
        [
            action(action="tap", element=1, summary="点开春日影"),
            action(action="wait", seconds=40, video="long", seen=kitten_seen()),
            # 同一个视频，她这次写的标题少了括号：接着看，不拦
            action(action="wait", seconds=60, video="long", seen=kitten_seen("要乐奈唱春日影 吉他弹唱")),
            action(action="finish", summary="听完了春日影"),
        ],
    )
    harness.agent._memory = memory
    await harness.run()
    watched = [item for item in memory.items() if item.kind == "watched"]
    assert [(item.title, item.form, item.seconds, item.app) for item in watched] == [
        ("【要乐奈】唱春日影 吉他弹唱", "long", 100, "哔哩哔哩")
    ]
    assert "正在看：《【要乐奈】唱春日影 吉他弹唱》（长视频），已经看了约 2 分钟" in harness.state_texts()[3]
    # 视频不再记成「看过的内容」，规划只在最后一行提一下
    assert not any(item.kind == "content" for item in memory.items())

    # 下一次：列表里又看到这张卡片不点；在播放器里刷到也不看
    harness.device.calls.clear()
    harness.responses = [
        action(action="tap", element=1, summary="点开春日影"),
        action(action="tap", element=2, summary="点开小奶猫"),
        action(action="wait", seconds=20, video="short", seen=kitten_seen()),
        action(action="finish", summary="看了小奶猫"),
    ]
    record = await harness.run()
    assert [call for call in harness.device.actions() if call[0] == "tap"] == [("tap", 405, 250)]
    assert record.end_reason == "自己不想玩了"
    state = harness.state_texts()
    assert (
        "这个应用里你最近看过这些视频，再刷到就直接划走，列表里看到也别点开：《【要乐奈】唱春日影 吉他弹唱》"
        in state[4]
    )
    assert "→ 没点：《【要乐奈】唱春日影 吉他弹唱》刚才看过了，换一个没看过的" in state[5]
    assert "→ 没看：《【要乐奈】唱春日影 吉他弹唱》刚才看过了" in state[7]
    assert "最近看过的视频（不用再专门找来看）：《【要乐奈】唱春日影 吉他弹唱》" in harness.plan_prompts[-1]


async def test_she_rewatches_when_asked_and_the_switch_turns_skipping_off(tmp_path):
    memory = MemoryStore(tmp_path / "memory.json")
    memory.remember_watched(Seen("video", "【要乐奈】唱春日影 吉他弹唱"), "哔哩哔哩", "long", datetime.now())
    asked = json.dumps(
        {"app": "tv.danmaku.bili", "goal": "再看一遍要乐奈唱春日影吉他弹唱", "minutes": 5}, ensure_ascii=False
    )
    harness = Harness(
        tmp_path,
        FakeDevice(elements=[SONG_CARD]),
        [action(action="tap", element=1, summary="点开春日影"), action(action="finish", summary="又听了一遍")],
        plan=asked,
    )
    harness.agent._memory = memory
    await harness.run()
    assert ("tap", 135, 250) in harness.device.actions()

    harness.device.calls.clear()
    harness.plans = [PLAN]
    harness.config.play.skip_watched_videos = False
    harness.responses = [
        action(action="tap", element=1, summary="点开春日影"),
        action(action="finish", summary="又听了一遍"),
    ]
    await harness.run()
    assert ("tap", 135, 250) in harness.device.actions()
    assert "你最近看过这些视频" not in harness.state_texts()[-2]
    assert "看过的视频不要再点开" not in harness.vision_messages[-2][0]["content"]


async def test_player_sized_elements_are_not_mistaken_for_video_cards(tmp_path):
    # B 站播放器整屏那一格的文字是搜索推荐或 UP 主名字，和看过的标题对上了也不能拦住点评论、详情页
    memory = MemoryStore(tmp_path / "memory.json")
    memory.remember_watched(Seen("video", "当我不停的欺负小猫时"), "哔哩哔哩", "short", datetime.now())
    player = UIElement(index=1, label="当我不停的欺负小猫时", kind="Cell", clickable=True, bounds=(0, 0, 540, 1200))
    details = UIElement(index=2, label="", kind="Button", clickable=True, bounds=(440, 1100, 500, 1160))
    harness = Harness(
        tmp_path,
        FakeDevice(elements=[player, details]),
        [action(action="tap", element=2, summary="进详情页"), action(action="finish", summary="看了详情")],
    )
    harness.agent._memory = memory
    await harness.run()
    assert ("tap", 470, 1130) in harness.device.actions()


async def test_long_video_titles_are_kept_and_shortened_in_the_list(tmp_path):
    # B 站标题常常超过 40 个字，之前整个被丢掉，看过也记不住
    title = "剧场版「BanG Dream! Ave Mujica one Last Story」正式预告公开！2026年冬季上映决定"
    memory = MemoryStore(tmp_path / "memory.json")
    harness = Harness(
        tmp_path,
        FakeDevice(),
        [
            action(action="wait", seconds=20, video="long", seen=kitten_seen(title)),
            action(action="finish", summary="看了预告"),
        ],
    )
    harness.agent._memory = memory
    await harness.run()
    assert [item.title for item in memory.items()] == [title]
    harness.responses = [action(action="finish", summary="不看了")]
    await harness.run()
    assert f"《{title[:30]}…》" in harness.state_texts()[-1]


async def test_bilibili_tips_only_show_up_in_bilibili(tmp_path):
    config = PhonePlayConfig()
    config.safety.allowed_apps = [
        AllowedApp(package="tv.danmaku.bili", name="哔哩哔哩"),
        AllowedApp(package="com.xingin.xhs", name="小红书"),
    ]
    device = FakeDevice(foregrounds=["tv.danmaku.bili", "com.xingin.xhs"])
    harness = Harness(
        tmp_path, device, [action(action="wait", seconds=5), action(action="finish", summary="逛完了")], config=config
    )
    await harness.run()
    state = harness.state_texts()
    assert "B 站：首页、搜索结果里视频卡片的右下角写着时长" in state[0]
    assert "竖屏的沉浸式播放器" not in state[1]


async def test_time_on_the_next_video_without_a_title_is_not_added_to_the_previous_one(tmp_path):
    # 实测：看完第一个视频（写了标题）回首页点开第二个，第二个一直没写标题，140 秒全记到了第一个头上
    memory = MemoryStore(tmp_path / "memory.json")
    harness = Harness(
        tmp_path,
        FakeDevice(elements=[SONG_CARD, KITTEN_CARD]),
        [
            action(action="wait", seconds=40, video="long", seen=kitten_seen()),
            action(action="tap", element=2, summary="点开小奶猫"),
            action(action="wait", seconds=20, video="short"),
            action(action="wait", seconds=30, video="short"),
            action(action="finish", summary="看完了"),
        ],
    )
    harness.agent._memory = memory
    await harness.run()
    assert [(item.title, item.seconds) for item in memory.items()] == [("【要乐奈】唱春日影 吉他弹唱", 40)]
    state = harness.state_texts()
    assert "正在看：《【要乐奈】唱春日影 吉他弹唱》（长视频），已经看了 40 秒" in state[1]
    # 点开了别的视频：不再说在看春日影；没写标题时只说这一段看了多久，并提醒写上标题
    assert "正在看" not in state[2]
    assert "正在看：这个短视频（没写标题），已经看了 20 秒" in state[3]
    assert "（没写 seen 标题，插件记不住看过这个视频；标题看得清就写上）" in state[3]
    assert "正在看：这个短视频（没写标题），已经看了 50 秒" in state[4]


def bilibili_phone(elements: Sequence[UIElement]) -> FakeDevice:
    """停在 B 站某个页面的手机：截图和真机一样大，界面元素用真机上读到的。"""
    device = FakeDevice(elements=elements)
    device.png = make_png(*BILI_SCREEN)
    return device


async def test_video_opened_from_search_is_watched_on_the_detail_page(tmp_path):
    # 搜索结果点开的是详情页：上滑只是在翻评论，视频还在上面播；标题就在界面元素里
    memory = MemoryStore(tmp_path / "memory.json")
    harness = Harness(
        tmp_path,
        bilibili_phone(BILI_DETAIL),
        [
            action(action="wait", seconds=30, video="long", summary="看春日影现场"),
            action(action="swipe", direction="up", summary="看看评论"),
            action(action="wait", seconds=60, video="long", summary="接着看"),
            action(action="finish", summary="看完了"),
        ],
    )
    harness.agent._memory = memory
    await harness.run()
    state = harness.state_texts()
    assert "现在是 B 站的视频详情页（从搜索结果、相关推荐、UP 主主页点开的样子）" in state[0]
    assert "竖屏播放器（从首页推荐点开的样子）" not in state[0]
    # 她没写 seen，插件从详情页上读到了标题
    title = "【中字4K·HiRes】「春日影」- MyGO!!!!!｜M"
    assert [(item.title, item.form, item.seconds) for item in memory.items()] == [(title, "long", 90)]
    assert "没写 seen 标题" not in state[1]
    assert "（在详情页上下滑只是在翻简介、评论和相关推荐，视频没换，还在上面接着播）" in state[2]
    # 翻评论不算换视频，看了多久接着算
    assert f"正在看：《{title}》（长视频），已经看了 30 秒" in state[2]
    assert f"正在看：《{title}》（长视频），已经看了约 2 分钟" in state[3]


async def test_swiping_in_the_player_opened_from_home_changes_the_video(tmp_path):
    harness = Harness(
        tmp_path,
        bilibili_phone(BILI_PLAYER),
        [
            action(action="wait", seconds=20, video="short", summary="看小猫"),
            action(action="swipe", direction="up", summary="下一个"),
            action(action="wait", seconds=15, video="short", summary="看这个"),
            action(action="finish", summary="刷完了"),
        ],
    )
    await harness.run()
    state = harness.state_texts()
    assert "现在是 B 站的竖屏播放器（从首页推荐点开的样子）" in state[0]
    assert "（换到了下一个推荐视频）" in state[2]
    assert "正在看：" not in state[2]
    assert "正在看：这个短视频（没写标题），已经看了 15 秒" in state[3]


async def test_cards_bilibili_marks_as_recently_watched_are_not_opened(tmp_path):
    # 她自己没记下也不要紧：B 站在搜索结果卡片上标着「最近看过」
    harness = Harness(
        tmp_path,
        bilibili_phone(BILI_SEARCH),
        [
            action(action="tap", element=3, summary="点开 4K 的春日影"),
            action(action="tap", element=6, summary="点开初登场"),
            action(action="finish", summary="看完了"),
        ],
    )
    harness.agent._memory = MemoryStore(tmp_path / "memory.json")
    await harness.run()
    assert [call for call in harness.device.actions() if call[0] == "tap"] == [("tap", 585, 949)]
    assert (
        "→ 没点：《【中字4K·HiRes】「春日影」- MyGO!!!!!｜M》B 站标着「最近看过」，换一个没看过的"
        in harness.state_texts()[1]
    )


async def test_page_is_forgotten_when_she_switches_apps(tmp_path):
    config = PhonePlayConfig()
    config.safety.allowed_apps = [
        AllowedApp(package="tv.danmaku.bili", name="哔哩哔哩"),
        AllowedApp(package="com.xingin.xhs", name="小红书"),
    ]
    harness = Harness(
        tmp_path,
        bilibili_phone(BILI_PLAYER),
        [action(action="launch", app="com.xingin.xhs", summary="去小红书"), action(action="finish", summary="逛完了")],
        config=config,
    )
    pages: List[str] = []
    original = harness.agent._enter_app

    def record(session: Session, app: AllowedApp) -> None:
        original(session, app)
        pages.append(harness.agent.status()["page"])

    harness.agent._enter_app = record
    await harness.run()
    # 打开小红书时，刚才在 B 站认出来的竖屏播放器就清掉了
    assert pages[-1] == ""


def two_device_config() -> PhonePlayConfig:
    config = PhonePlayConfig()
    config.device.name = "iPhone"
    config.device.backend = "ios_wda"
    config.multi_device.devices = [DeviceSettings(name="OPPO", serial="192.168.1.23")]
    config.safety.allowed_apps = [
        AllowedApp(package="tv.danmaku.bilianime", name="哔哩哔哩", devices=["iPhone"]),
        AllowedApp(package="tv.danmaku.bili", name="哔哩哔哩", devices=["OPPO"]),
        AllowedApp(package="com.xingin.xhs", name="小红书"),
    ]
    return config


def two_device_harness(tmp_path, responses: Sequence[str], plan: str = PLAN) -> Tuple[Harness, Dict[str, FakeDevice]]:
    phones = {
        "iPhone": FakeDevice(installed={"tv.danmaku.bilianime", "com.xingin.xhs"}),
        "OPPO": FakeDevice(installed={"tv.danmaku.bili", "com.xingin.xhs"}),
    }
    phones["iPhone"].default_foreground = "tv.danmaku.bilianime"
    harness = Harness(tmp_path, phones["iPhone"], responses, config=two_device_config(), plan=plan)
    harness.agent._device_factory = lambda settings: phones[settings.name]
    return harness, phones


@pytest.mark.parametrize(
    "query,expected",
    [
        ("OPPO", "OPPO"),
        ("oppo", "OPPO"),
        ("OPPO手机", "OPPO"),
        ("安卓手机", "OPPO"),
        ("苹果", "iPhone"),
        ("iphone 14", "iPhone"),
        ("华为", None),
        ("", None),
    ],
)
def test_match_device(query, expected):
    assert match_device(query, two_device_config().device_choices()) == expected


def test_match_device_is_not_guessed_when_ambiguous():
    config = two_device_config()
    config.multi_device.devices.append(DeviceSettings(name="模拟器", serial="127.0.0.1:16416"))
    # 两台安卓：「安卓」认不出是哪台
    assert match_device("安卓", config.device_choices()) is None


async def test_switched_device_is_used_and_its_apps_only(tmp_path):
    plan = json.dumps({"app": "tv.danmaku.bili", "goal": "看猫猫视频", "minutes": 5}, ensure_ascii=False)
    harness, phones = two_device_harness(tmp_path, [action(action="finish", summary="看了一会儿")], plan=plan)
    assert await harness.agent.switch_device("OPPO") == ("OPPO", True)
    record = await harness.run()
    assert ("launch", "tv.danmaku.bili") in phones["OPPO"].actions()
    assert phones["iPhone"].calls == []
    # 只标了 iPhone 的 B 站不会出现在 OPPO 的计划里
    assert "tv.danmaku.bilianime" not in harness.plan_prompts[0]
    assert "tv.danmaku.bili" in harness.plan_prompts[0] and "com.xingin.xhs" in harness.plan_prompts[0]
    assert record.device == "OPPO"
    assert "用OPPO" not in harness.agent.activity_text()
    # 换的设备存进了使用记录，重启后还在
    saved = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert saved["current_device"] == "OPPO"


IPHONE_PLAN = json.dumps({"app": "tv.danmaku.bilianime", "goal": "看猫猫视频", "minutes": 5}, ensure_ascii=False)


async def test_cannot_switch_while_playing(tmp_path):
    harness, _ = two_device_harness(
        tmp_path, [action(action="wait", seconds=5), action(action="finish")], plan=IPHONE_PLAN
    )
    gate, deciding = asyncio.Event(), asyncio.Event()
    harness.agent._vision = gated_vision(harness, gate, deciding)
    await harness.agent.start(trigger="command")
    await asyncio.wait_for(deciding.wait(), 5)
    assert "用iPhone：" in harness.agent.activity_text()
    with pytest.raises(SwitchRefused, match="正在用iPhone玩手机，先停下再换"):
        await harness.agent.switch_device("OPPO")
    with pytest.raises(SwitchRefused, match="没有叫「华为」的设备"):
        await harness.agent.switch_device("华为")
    gate.set()
    await harness.wait_done()
    assert harness.agent.status()["device_name"] == "iPhone"


async def test_removed_device_falls_back_to_the_main_one(tmp_path):
    harness, phones = two_device_harness(tmp_path, [action(action="finish")])
    await harness.agent.switch_device("OPPO")
    harness.config.multi_device.devices = []
    harness.config.safety.allowed_apps = [AllowedApp(package="tv.danmaku.bilianime", name="哔哩哔哩")]
    harness.plans = [IPHONE_PLAN]
    await harness.run()
    assert harness.agent.status()["device_name"] == "iPhone"
    assert ("launch", "tv.danmaku.bilianime") in phones["iPhone"].actions()


def test_device_names_are_checked():
    with pytest.raises(ValueError, match="设备名字重复了：OPPO"):
        PhonePlayConfig.model_validate(
            {"device": {"name": "OPPO"}, "multi_device": {"devices": [{"name": "oppo", "serial": "a"}]}}
        )
    with pytest.raises(ValueError, match="每台手机都要写 name"):
        PhonePlayConfig.model_validate({"multi_device": {"devices": [{"serial": "a"}]}})
    with pytest.raises(ValueError, match="不存在的设备：小米"):
        PhonePlayConfig.model_validate(
            {"safety": {"allowed_apps": [{"package": "com.a.b", "name": "A", "devices": ["小米"]}]}}
        )
    # 只有一台、没写名字时叫「默认」，旧配置照常能用
    assert [name for name, _ in PhonePlayConfig().device_choices()] == ["默认"]


async def test_device_label_names_the_device_once(tmp_path):
    harness, phones = two_device_harness(tmp_path, [])
    phones["OPPO"].__class__ = type(
        "NamedPhone", (FakeDevice,), {"description": property(lambda self: "安卓 · 1.2.3.4:5")}
    )
    await harness.agent.switch_device("OPPO")
    harness.agent._get_device()
    assert harness.agent.status()["device"] == "OPPO · 安卓 · 1.2.3.4:5"
    phones["iPhone"].__class__ = type("IPhone", (FakeDevice,), {"description": property(lambda self: "iPhone · url")})
    await harness.agent.switch_device("iPhone")
    harness.agent._get_device()
    assert harness.agent.status()["device"] == "iPhone · url"
