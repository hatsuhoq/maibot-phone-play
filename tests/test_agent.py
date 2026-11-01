from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import asyncio
import json
import logging
import pytest
import threading
import time

from phone_play.agent import PhoneAgent, PlayRefused, Session, in_active_hours
from phone_play.devices import DeviceError
from phone_play.prompts import Persona
from phone_play.settings import AllowedApp, PhonePlayConfig
from phone_play.store import SessionRecord, UsageStore
from phone_play.ui_tree import UIElement

from conftest import FakeDevice, make_png, scene_png

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
    ) -> None:
        self.config = config or PhonePlayConfig()
        self.device = device
        self.responses = list(responses)
        self.vision_messages: List[List[Dict[str, Any]]] = []
        self.plan_prompts: List[str] = []
        self.notified: List[tuple] = []
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
        )

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

    # 先关掉应用再打开（从首页开始），玩完回桌面
    assert harness.device.actions() == [
        ("stop", "tv.danmaku.bili"),
        ("launch", "tv.danmaku.bili"),
        ("tap", 270, 200),
        ("swipe", 270, 600, 270, 120),
        ("key", "home"),
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
    assert harness.device.actions() == [("launch", "tv.danmaku.bili")]


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
    assert record.end_reason == "连续 3 步操作失败"
    assert harness.agent.status()["last_error"] == "连续 3 步操作失败"


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
    with pytest.raises(PlayRefused, match="都没有安装") as refused:
        await harness.agent.start(trigger="chat")
    assert refused.value.chat_text == "手机上没有你能玩的应用"
    assert harness.agent.state == "idle"


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
    with pytest.raises(PlayRefused, match="积极拒绝") as refused:
        await harness.agent.start(trigger="chat", origin_stream="s")
    # 聊天里只说手机连不上，adb 细节留在 WebUI 的最近错误里
    assert refused.value.chat_text == "手机连不上，可能没开机或者没连好"
    assert harness.agent.state == "idle"
    assert "积极拒绝" in harness.agent.status()["last_error"]
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
    assert record.summary == "结束刷抖音；奶奶找吃的给孙女，看看。；刷得差不多了，奶奶的视频很暖。"
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
    await harness.run()
    assert harness.device.actions()[0] == ("stop", "tv.danmaku.bili")
    harness.device.calls.clear()
    harness.seconds += 5 * 60
    await harness.run()
    assert harness.device.actions()[0] == ("launch", "tv.danmaku.bili")
    harness.device.calls.clear()
    harness.seconds += 16 * 60
    await harness.run()
    assert harness.device.actions()[0] == ("stop", "tv.danmaku.bili")


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


@pytest.mark.parametrize(
    "foregrounds,default,swiped", [(["tv.danmaku.bili", ""], "tv.danmaku.bili", False), ([], "", True)]
)
async def test_unknown_foreground_right_before_acting(tmp_path, foregrounds, default, swiped):
    # 刚才认得出、做之前认不出（切应用、系统界面）就先不做；一直认不出的设备照常做，不然玩不了
    device = FakeDevice(foregrounds=foregrounds)
    device.default_foreground = default
    harness = Harness(tmp_path, device, [action(action="swipe", direction="up"), action(action="finish")])
    await harness.run()
    assert any(call[0] == "swipe" for call in harness.device.actions()) is swiped
    assert ("做之前看不出现在在哪个应用" in harness.state_texts()[1]) is not swiped


class HangingPhone(FakeDevice):
    """断开后连接请求一直不返回（真机上要等到请求超时）。"""

    async def ensure_ready(self) -> None:
        await asyncio.Event().wait()


async def test_reconnect_wait_is_bounded_even_if_a_request_hangs(tmp_path, monkeypatch):
    import phone_play.agent as agent_module

    monkeypatch.setattr(agent_module, "_RECONNECT_SECONDS", 1.0)
    monkeypatch.setattr(agent_module, "_RECONNECT_INTERVAL", 0.4)
    harness = Harness(tmp_path, HangingPhone(), [])
    started = time.perf_counter()
    assert await asyncio.wait_for(harness.agent._wait_for_device(harness.device), 5) is False
    assert time.perf_counter() - started < 2


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
