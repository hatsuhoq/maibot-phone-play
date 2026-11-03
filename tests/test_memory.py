from datetime import datetime, timedelta

import json

from phone_play.actions import Seen
from phone_play.memory import MemoryStore, titles_match

NOW = datetime(2026, 11, 2, 14, 0)


def test_titles_match_ignores_brackets_and_cut_off_labels_but_not_short_names():
    assert titles_match("【要乐奈】唱春日影 吉他弹唱完整版", "要乐奈唱春日影吉他弹唱")
    # B 站卡片的文字会被截短
    assert titles_match(
        "颂乐人偶：第4话 演出已然落幕。（Acta est fabula）", "颂乐人偶：第4话 演出已然落幕。（Acta est fabu"
    )
    assert titles_match("迷途之子!!!!!", "迷途之子")
    # 只有歌名这样的短标题：只认完全一样的，不然同名的现场、翻唱全被当成看过
    assert not titles_match("春日影", "【MyGO】春日影 现场版")
    assert not titles_match("", "春日影")


def test_watched_videos_have_their_own_limit_and_are_forgotten_after_a_month(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    store.remember_interest("对方喜欢看种田小说", NOW - timedelta(days=1))
    for index in range(305):
        seen = Seen("video", f"猫猫视频第{index}个超可爱合集")
        store.remember_watched(seen, "哔哩哔哩", "short", NOW - timedelta(minutes=305 - index))
    kinds = [item.kind for item in store.items()]
    # 刷了三百多个视频，对方说过的话也不会被挤掉
    assert kinds.count("watched") == 300 and kinds.count("interest") == 1
    assert store.find_watched("哔哩哔哩", "猫猫视频第0个超可爱合集", NOW) is None
    assert store.find_watched("哔哩哔哩", "猫猫视频第304个超可爱合集", NOW) is not None
    # 只在同一个应用里算，过了 30 天就忘了
    assert store.find_watched("抖音", "猫猫视频第304个超可爱合集", NOW) is None
    assert store.find_watched("哔哩哔哩", "猫猫视频第304个超可爱合集", NOW + timedelta(days=31)) is None
    assert store.watched_titles("哔哩哔哩", NOW, 2) == ["猫猫视频第304个超可爱合集", "猫猫视频第303个超可爱合集"]
    # 规划只看到最近几个视频，不会被淹没
    lines = store.prompt_lines(NOW)
    assert lines[0] == "对方说过：对方喜欢看种田小说"
    assert lines[1].startswith("最近看过的视频（不用再专门找来看）：《猫猫视频第304个超可爱合集》、")
    assert len(lines) == 2
    assert store.clear("watched") == 300
    assert [item.kind for item in store.items()] == ["interest"]


def test_same_video_seen_again_updates_one_record(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    first = store.remember_watched(Seen("video", "【要乐奈】唱春日影 吉他弹唱", "P1"), "哔哩哔哩", "long", NOW)
    again = store.remember_watched(Seen("video", "要乐奈唱春日影 吉他弹唱"), "哔哩哔哩", "", NOW + timedelta(minutes=1))
    store.add_watched_seconds(first.id, 95.4)
    assert again is first
    assert (first.title, first.detail, first.form, first.seconds) == ("【要乐奈】唱春日影 吉他弹唱", "P1", "long", 95)


async def test_videos_saved_as_content_by_the_earlier_version_become_watched(tmp_path):
    # 真实 Bot 用上一版记下的样子：视频也是 content
    path = tmp_path / "memory.json"
    raw = {
        "source": "screen",
        "app": "哔哩哔哩",
        "created_at": "2026-11-02T13:55:44",
        "updated_at": "2026-11-02T13:55:44",
    }
    path.write_text(
        json.dumps(
            {
                "items": [
                    {"id": "a1", "kind": "content", "title": "迷途之子!!!!!", "media": "video", **raw},
                    {"id": "b2", "kind": "content", "title": "田园小当家", "media": "novel", **raw},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = MemoryStore(path)
    await store.load()
    assert {(item.title, item.kind) for item in store.items()} == {
        ("迷途之子!!!!!", "watched"),
        ("田园小当家", "content"),
    }
    assert store.find_watched("哔哩哔哩", "迷途之子", NOW) is not None
