"""每一步只带用得上的：规则按当前这类应用带，B 站详情页只说画面上的哪几个看过（D2）。"""

from datetime import datetime
from typing import List

from phone_play.actions import Seen
from phone_play.memory import MemoryStore
from phone_play.prompts import (
    APP_GAME,
    APP_READING,
    APP_VIDEO,
    DecisionContext,
    Persona,
    app_kind,
    build_decision_messages,
)
from phone_play.settings import AllowedApp, PhonePlayConfig
from phone_play.ui_tree import UIElement

from conftest import FakeDevice
from test_agent import Harness, action

VIDEO_RULE = "看视频分短视频和长视频"
VIDEO_WAIT = '{"action":"wait","seconds":20,"video":"short"}'
SEEN_RULE = "看视频时在 seen 里写上画面上的视频标题"
SEEN_GUIDE = '可以加 "seen"'
ROOM_RULE = "在联机游戏的房间里（好友房、组队房间）"
ROOM_WAIT = "在游戏房间里等人进来、等开局、等匹配时可以一次等 60~120 秒"


def system_text(kind: str) -> str:
    context = DecisionContext(
        persona=Persona(nickname="小睦"),
        goal="随便玩玩",
        apps=[AllowedApp(package="tv.danmaku.bili", name="哔哩哔哩")],
        allow_text_input=True,
        ascii_text_only=False,
        max_text_length=30,
        now=datetime(2026, 11, 27, 10, 0),
        elapsed_minutes=1,
        max_minutes=10,
        step=2,
        max_steps=60,
        foreground="",
        history=[],
        elements=[],
        elements_note="",
        image_url="data:image/jpeg;base64,",
        app_kind=kind,
    )
    return build_decision_messages(context)[0]["content"]


def present(text: str, *parts: str) -> List[bool]:
    return [part in text for part in parts]


def test_rules_follow_the_kind_of_app():
    parts = (VIDEO_RULE, VIDEO_WAIT, SEEN_RULE, SEEN_GUIDE, ROOM_RULE, ROOM_WAIT)
    # 认不出的应用、在桌面上：什么都可能，全带上
    unknown = system_text("")
    assert present(unknown, *parts) == [True] * 6
    # 刷视频：不说联机房间怎么等
    video = system_text(APP_VIDEO)
    assert present(video, *parts) == [True, True, True, True, False, False]
    # 听歌、看小说：要记 seen，不说看视频那套，也不说联机房间
    reading = system_text(APP_READING)
    assert present(reading, *parts) == [False, False, True, True, False, False]
    # 游戏：只说联机房间怎么等
    game = system_text(APP_GAME)
    assert present(game, *parts) == [False, False, False, False, True, True]
    assert len(game) < len(video) < len(unknown) and len(reading) < len(video)
    # 普通的 wait、结束、示例谁都有
    for text in (unknown, video, reading, game):
        assert '{"action":"wait","seconds":20}：停下来看一会儿（1~120 秒）' in text
        assert '可以加 "moment":true' in text and "示例：" in text


def test_known_apps():
    assert app_kind("tv.danmaku.bilianime") == app_kind("tv.danmaku.bili") == APP_VIDEO
    assert app_kind("com.ss.iphone.ugc.Aweme") == app_kind("com.xingin.discover") == APP_VIDEO
    assert app_kind("com.netease.cloudmusic") == app_kind("com.dragon.read") == APP_READING
    assert app_kind("com.tencent.ioshlddz") == APP_GAME
    assert app_kind("com.example.unknown") == app_kind("com.apple.springboard") == ""


DETAIL = [
    UIElement(index=1, label="简介", kind="Button", clickable=True, bounds=(10, 400, 100, 440)),
    UIElement(index=2, label="评论", kind="Button", clickable=True, bounds=(110, 400, 200, 440)),
    UIElement(index=3, label="猫猫第一次见到雪的反应太可爱了", kind="Cell", clickable=True, bounds=(0, 500, 540, 600)),
    UIElement(index=4, label="新出的小奶猫踩奶合集", kind="Cell", clickable=True, bounds=(0, 600, 540, 700)),
]


def detail_page(tmp_path, backend: str) -> Harness:
    config = PhonePlayConfig()
    config.device.backend = backend
    harness = Harness(tmp_path, FakeDevice(elements=DETAIL), [action(action="finish", summary="不看了")], config=config)
    memory = MemoryStore(tmp_path / "memory.json")
    for title in ("猫猫第一次见到雪的反应太可爱了", "橘猫偷吃被抓包的一百种表情"):
        # 看过的短视频（只点开过的长视频算「可以接着看」，不在这个列表里）
        memory.remember_watched(Seen(media="video", title=title), "哔哩哔哩", "short", datetime.now())
    harness.agent._memory = memory
    return harness


async def test_on_the_bilibili_detail_page_only_watched_videos_on_screen_are_listed(tmp_path):
    harness = detail_page(tmp_path, "ios_wda")
    await harness.run()
    state = harness.state_texts()[0]
    # 画面上这个看过；另一个不在画面上，这一步点不到，不说
    assert "画面上这几个视频你最近看过，别点开：《猫猫第一次见到雪的反应太可爱了》\n" in state
    assert "橘猫偷吃" not in state and "这个应用里你最近看过这些视频" not in state


async def test_elsewhere_the_whole_watched_list_is_still_sent(tmp_path):
    # 安卓上的详情页没实测过推荐视频的标题读不读得到：还是全说
    harness = detail_page(tmp_path / "android", "adb")
    (tmp_path / "android").mkdir()
    await harness.run()
    state = harness.state_texts()[0]
    assert "这个应用里你最近看过这些视频" in state and "橘猫偷吃被抓包的一百种表情" in state
    assert "画面上这几个" not in state
