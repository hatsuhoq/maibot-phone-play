from datetime import date, timedelta

import json

from phone_play.memory import MemoryItem
from phone_play.report import build_report, report_html
from phone_play.store import SessionRecord, UsageStore, place_and_goal

TODAY = date(2026, 11, 13)


def record(started: str, ended: str, app: str = "哔哩哔哩", **fields) -> SessionRecord:
    return SessionRecord(
        started_at=started,
        ended_at=ended,
        trigger=fields.pop("trigger", "自己想玩"),
        app=app,
        goal="看猫猫视频",
        summary=fields.pop("summary", "看了三只橘猫"),
        steps=10,
        end_reason="自己不想玩了",
        **fields,
    )


async def test_daily_totals_are_kept_beyond_the_last_30_sessions(tmp_path):
    store = UsageStore(tmp_path / "usage.json")
    store.add_record(
        record(
            "2026-11-12T20:00:00",
            "2026-11-12T20:12:00",
            apps=[
                {"name": "欢乐斗地主", "package": "a", "seconds": 300},
                {"name": "哔哩哔哩", "package": "b", "seconds": 420},
            ],
            trigger="桌宠",
        )
    )
    # 旧记录没有 apps：整次都算在起始应用上
    store.add_record(record("2026-11-12T21:00:00", "2026-11-12T21:05:00"))
    assert store.daily == {
        "2026-11-12": {
            "sessions": 2,
            "seconds": 1020.0,
            "apps": {"欢乐斗地主": 300.0, "哔哩哔哩": 720.0},
            "triggers": {"桌宠": 1, "自己想玩": 1},
        }
    }
    # 游玩记录只留 30 次，每天的汇总留 35 天
    for offset in range(40):
        day = (date(2026, 9, 1) + timedelta(days=offset)).isoformat()
        store.add_record(record(f"{day}T08:00:00", f"{day}T08:01:00"))
    assert len(store.history) == 30
    assert len(store.daily) == 35 and "2026-11-12" in store.daily and "2026-09-01" not in store.daily

    await store.save()
    reloaded = UsageStore(tmp_path / "usage.json")
    await reloaded.load()
    assert reloaded.daily == store.daily


async def test_old_usage_files_get_daily_totals_from_their_history(tmp_path):
    path = tmp_path / "usage.json"
    old = record("2026-11-10T19:05:46", "2026-11-10T19:17:46")
    path.write_text(
        json.dumps({"date": "2026-11-10", "sessions": 1, "steps": 12, "history": [old.to_dict()]}, ensure_ascii=False),
        encoding="utf-8",
    )
    store = UsageStore(path)
    await store.load()
    assert store.recovered == ""
    assert store.daily["2026-11-10"]["sessions"] == 1 and store.daily["2026-11-10"]["seconds"] == 720.0


def watched(title: str, updated: str, seconds: int = 0, app: str = "哔哩哔哩", kind: str = "watched") -> MemoryItem:
    return MemoryItem(id=title, kind=kind, title=title, app=app, updated_at=updated, seconds=seconds)


def test_weekly_report_counts_the_last_seven_days():
    daily = {
        # 8 天前的不算
        "2026-11-06": {"sessions": 9, "seconds": 9000.0, "apps": {"抖音": 9000.0}, "triggers": {"自己想玩": 9}},
        "2026-11-07": {"sessions": 1, "seconds": 600.0, "apps": {"网易云音乐": 600.0}, "triggers": {"自己想玩": 1}},
        "2026-11-13": {
            "sessions": 2,
            "seconds": 1500.0,
            "apps": {"哔哩哔哩": 1200.0, "小红书": 300.0},
            "triggers": {"桌宠": 1, "聊天中决定": 1},
        },
    }
    history = [
        record("2026-11-06T08:00:00", "2026-11-06T08:30:00", summary="太久以前的不提"),
        record("2026-11-07T08:00:00", "2026-11-07T08:10:00", summary="木吉他的声音好舒服。；再听一会儿"),
        record("2026-11-13T20:00:00", "2026-11-13T20:20:00", summary="猫" * 60),
    ]
    memory = [
        watched("橘猫踩奶合集", "2026-11-13T20:05:00", seconds=90),
        watched("春日影", "2026-11-08T10:00:00", app="网易云音乐", kind="content"),
        watched("上个月看的", "2026-10-01T10:00:00", seconds=999),
        watched("对方说过喜欢猫", "2026-11-13T10:00:00", kind="interest"),
    ]
    report = build_report("小睦", daily, history, memory, TODAY)
    assert (report.start, report.end) == (date(2026, 11, 7), TODAY)
    assert [minutes for _, minutes in report.days] == [10, 0, 0, 0, 0, 0, 25]
    assert (report.sessions, report.minutes) == (3, 35)
    assert report.apps == [("哔哩哔哩", 20), ("网易云音乐", 10), ("小红书", 5)]
    assert report.triggers == [("自己想玩", 1), ("桌宠", 1), ("聊天中决定", 1)]
    assert report.watched == ["哔哩哔哩 · 《橘猫踩奶合集》", "网易云音乐 · 《春日影》"]
    assert report.quotes == ["猫" * 39 + "…", "木吉他的声音好舒服"]


def test_weekly_report_card_escapes_text_and_needs_nothing_from_the_network():
    daily = {
        "2026-11-13": {"sessions": 1, "seconds": 4200.0, "apps": {"<b>斗地主</b>": 4200.0}, "triggers": {"自己想玩": 1}}
    }
    history = [record("2026-11-13T20:00:00", "2026-11-13T21:10:00", summary="赢了两把<script>")]
    html = report_html(build_report("小睦", daily, history, [], TODAY))
    assert "小睦的手机周报" in html and "11.07 – 11.13" in html
    assert "1 小时 10 分" in html and "今天" in html
    assert "&lt;b&gt;斗地主&lt;/b&gt;" in html and "<b>斗地主" not in html
    assert "<script>" not in html
    assert "http" not in html
    # 没有看过的内容就不放那一节
    assert "看过的" not in html


def test_the_app_is_not_said_twice_when_her_goal_already_names_it():
    # 实测放下手机后的近况写成了「在番茄小说里在番茄小说里搜索《反转练习生》」
    assert place_and_goal("番茄小说", "在番茄小说里搜索《反转练习生》") == "在番茄小说里搜索《反转练习生》"
    assert place_and_goal("网易云音乐", "打开网易云音乐听安静的歌") == "打开网易云音乐听安静的歌"
    assert place_and_goal("哔哩哔哩", "看猫猫视频") == "在哔哩哔哩里看猫猫视频"
    assert place_and_goal("", "看猫猫视频") == "看猫猫视频"
    # 中途去过别的应用：去过哪些接在后面，不再套一层「在……里」
    places = "欢乐斗地主（约 3 分钟）、哔哩哔哩（约 1 分钟）"
    assert (
        place_and_goal(places, "在欢乐斗地主里打一局", "欢乐斗地主")
        == "在欢乐斗地主里打一局，这次去过欢乐斗地主（约 3 分钟）、哔哩哔哩（约 1 分钟）"
    )
    assert place_and_goal(places, "打一局", "欢乐斗地主") == f"在{places}里打一局"
