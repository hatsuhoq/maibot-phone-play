import pytest

from phone_play.life import LifeLinkError, parse_share_result, parse_snapshot, share_text
from phone_play.settings import LifeLinkSettings
from phone_play.store import SessionRecord


def record(**changes) -> SessionRecord:
    fields = dict(
        started_at="2026-11-06T19:20:00",
        ended_at="2026-11-06T19:30:00",
        trigger="自己想玩",
        app="抖音",
        goal="刷几个短视频",
        summary="这只布偶叫得好认真；这只小狸花看着好乖",
        steps=13,
        end_reason="玩够 10 分钟了",
    )
    fields.update(changes)
    return SessionRecord(**fields)


def test_share_says_where_she_saw_what():
    topic, motive = share_text(record())
    assert topic == "刚才自己刷抖音看到的：这只布偶叫得好认真；这只小狸花看着好乖"
    assert motive == "空闲时自己拿起手机刷到的，觉得有意思可以当见闻随口聊聊；不想说也可以不说"
    topic, motive = share_text(record(summary="", interactions=["点赞 2 次"]))
    assert topic == "刚才自己刷抖音看到的：刷几个短视频"
    assert motive.endswith("。还用自己的账号点赞 2 次")
    # 上班摸鱼的那几局
    topic, motive = share_text(record(trigger="摸鱼"))
    assert topic == "刚才摸鱼偷偷刷抖音看到的：这只布偶叫得好认真；这只小狸花看着好乖"
    assert motive.startswith("上班或上课中途偷偷摸鱼刷到的")


def test_share_is_clipped_to_what_mai_life_keeps():
    topic, _ = share_text(record(summary="猫" * 300))
    assert len(topic) == 160 and topic.endswith("…")


@pytest.mark.parametrize(
    "response,message",
    [
        ({"success": False, "error": "no_current_framework"}, "麦生活的日程这会儿是空档"),
        ({"success": False, "error": "not_ready_or_empty_topic"}, "麦生活还没就绪"),
        (
            {"success": False, "error": "未找到 API 提供方插件: maibot-community.mai-life"},
            "没交给麦生活：未找到 API 提供方插件",
        ),
        ({"success": True}, "没交给麦生活：调用失败"),
    ],
)
def test_share_results(response, message):
    assert parse_share_result({"success": True, "opportunity_id": "f00d"}) == "f00d"
    with pytest.raises(LifeLinkError, match=message):
        parse_share_result(response)


# 麦生活 get_schedule_snapshot 的成功回复（经 SDK 拆掉宿主那一层之后），照它 docs/schedule-provider-api.md 的示例
SNAPSHOT = {
    "success": True,
    "error": None,
    "data": {
        "timezone": "Asia/Shanghai",
        "as_of": "2026-10-25T10:00:00+08:00",
        "revision": "9f3ab02c1d8e4f5a",
        "schedule_available": True,
        "current": {
            "id": "a1",
            "kind": "study",
            "name": "图书馆自习",
            "description": "",
            "start_at": "2026-10-25T09:00:00+08:00",
            "end_at": "2026-10-25T11:00:00+08:00",
            "location": "图书馆",
            "scene": "坐在窗边整理今天的笔记",
        },
        "next_activities": [],
    },
}


def snapshot(**data_changes):
    return {**SNAPSHOT, "data": {**SNAPSHOT["data"], **data_changes}}


def test_current_activity_and_time_left():
    activity = parse_snapshot(SNAPSHOT)
    assert activity is not None
    assert (activity.kind, activity.name, activity.location, activity.scene) == (
        "study",
        "图书馆自习",
        "图书馆",
        "坐在窗边整理今天的笔记",
    )
    assert activity.remaining_seconds == 3600
    assert activity.describe() == "「图书馆自习」（学习 · 图书馆）"


def test_time_left_does_not_depend_on_the_offsets_used():
    # 同一时刻写成不同时区：只用两边时刻相减，结果一样
    current = {**SNAPSHOT["data"]["current"], "end_at": "2026-10-25T12:30:00+09:00"}
    activity = parse_snapshot(snapshot(current=current))
    assert activity is not None and activity.remaining_seconds == 5400


def test_gap_between_activities_has_no_current():
    assert parse_snapshot(snapshot(current=None)) is None


@pytest.mark.parametrize(
    "response,message",
    [
        # 宿主那一层失败：没装麦生活时就是这样
        (
            {"success": False, "error": "未找到 API 提供方插件: maibot-community.mai-life"},
            "没连上麦生活：未找到 API 提供方插件",
        ),
        ({"success": False, "error": "日程服务尚未初始化", "data": None}, "麦生活：日程服务尚未初始化"),
        (snapshot(schedule_available=False, current=None, revision=""), "麦生活今天的日程还没生成"),
        (snapshot(as_of="昨天"), "as_of 不是 ISO 时间"),
        (snapshot(as_of="2026-10-25T10:00:00"), "as_of 没带时区"),
        ("不是字典", "返回的不是对象"),
    ],
)
def test_unusable_replies_say_why(response, message):
    with pytest.raises(LifeLinkError, match=message):
        parse_snapshot(response)


def test_play_kinds_accept_chinese_and_drop_repeats():
    assert LifeLinkSettings().play_kinds == ["leisure", "rest", "meal", "travel"]
    assert LifeLinkSettings().slack_kinds == ["work", "study"]
    settings = LifeLinkSettings(play_kinds=["休闲", "Leisure", " 在路上 ", "work"], slack_kinds=["学习"])
    assert (settings.play_kinds, settings.slack_kinds) == (["leisure", "travel", "work"], ["study"])
    with pytest.raises(ValueError, match="play_kinds 里的「打游戏」不认识"):
        LifeLinkSettings(play_kinds=["打游戏"])
    with pytest.raises(ValueError, match="slack_kinds 里的「摸鱼」不认识"):
        LifeLinkSettings(slack_kinds=["摸鱼"])
