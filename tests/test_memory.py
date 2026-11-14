from datetime import datetime, timedelta

import json

from phone_play.actions import Seen
from phone_play.memory import MemoryItem, MemoryStore, titles_match

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
    assert [item.title for item in store.watched_items("哔哩哔哩", NOW)[:2]] == [
        "猫猫视频第304个超可爱合集",
        "猫猫视频第303个超可爱合集",
    ]
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


async def test_watched_videos_remember_how_far_she_got(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    song = store.remember_watched(Seen("video", "要乐奈唱春日影"), "哔哩哔哩", "long", NOW)
    assert (song.progress, song.watch_state(), song.resumable) == ("opened", "只点开过", True)
    store.add_watched_seconds(song.id, 90)
    assert (song.progress, song.watch_state(), song.resumable) == ("partial", "看了一部分，约 2 分钟", True)
    # 画面上看得出播完了才算看完
    store.remember_watched(Seen("video", "要乐奈唱春日影", finished=True), "哔哩哔哩", "long", NOW)
    assert (song.progress, song.watch_state(), song.resumable) == ("finished", "看完了", False)
    # 短视频划走了就不再回去看；不感兴趣的另记，之后再刷到也不会抹掉
    kitten = store.remember_watched(Seen("video", "小奶猫第一次睁眼"), "抖音", "short", NOW)
    store.add_watched_seconds(kitten.id, 8)
    assert kitten.progress == "partial" and not kitten.resumable
    ad = store.remember_watched(Seen("video", "九块九包邮", skipped=True), "哔哩哔哩", "long", NOW)
    store.remember_watched(Seen("video", "九块九包邮"), "哔哩哔哩", "long", NOW)
    assert (ad.watch_state(), ad.resumable) == ("不感兴趣", False)
    movie = store.remember_watched(Seen("video", "迷途之子 第3话"), "哔哩哔哩", "long", NOW)
    store.add_watched_seconds(movie.id, 200)
    assert [item.title for item in store.resumable(NOW, 3)] == ["迷途之子 第3话"]
    lines = store.prompt_lines(NOW)
    assert lines[-2] == "看了一部分的长视频（想看可以接着看）：《迷途之子 第3话》（哔哩哔哩，看了一部分，约 3 分钟）"
    assert lines[-1].startswith("最近看过的视频（不用再专门找来看）：") and "迷途之子" not in lines[-1]
    assert movie.describe() == "在哔哩哔哩看过长视频《迷途之子 第3话》（看了一部分，约 3 分钟）"

    # 旧版本记的「看过」：看到哪一步是空的，照旧当看过，不往看完上猜，也不当成可以接着看
    path = tmp_path / "old.json"
    old = {
        "id": "old",
        "kind": "watched",
        "title": "旧视频",
        "media": "video",
        "app": "哔哩哔哩",
        "form": "long",
        "seconds": 300,
        "updated_at": NOW.isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps({"items": [old]}, ensure_ascii=False), encoding="utf-8")
    reloaded = MemoryStore(path)
    await reloaded.load()
    item = reloaded.items()[0]
    assert reloaded.recovered == "" and (item.progress, item.watch_state(), item.resumable) == ("", "", False)
    assert item.describe() == "在哔哩哔哩看过长视频《旧视频》"
    assert isinstance(item, MemoryItem)


def test_unfinished_goal_does_not_repeat_the_app():
    item = MemoryItem(id="u", kind="unfinished", title="在番茄小说里搜索《反转练习生》", app="番茄小说")
    assert item.describe() == "上次没做完：在番茄小说里搜索《反转练习生》"
    item = MemoryItem(id="u", kind="unfinished", title="看猫猫视频", app="哔哩哔哩", detail="时间到了")
    assert item.describe() == "上次没做完：在哔哩哔哩里看猫猫视频（时间到了）"


async def test_things_you_ask_her_to_watch_later_follow_what_she_watches(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    episode = store.add_later("迷途之子 第3话", "B站", NOW)
    novel = store.add_later("田园小当家", "", NOW)
    tiny = store.add_later("猫", "", NOW)
    # 同样的只留一条
    assert store.add_later("迷途之子第3话", "", NOW) is episode and episode.app == "B站"
    assert (episode.watch_state(), episode.describe()) == ("还没看", "对方让你有空看：迷途之子 第3话（B站，还没看）")

    # 只点开过不算看了；停下来看了一会儿算看了一部分；画面上看得出播完才算看完
    video = store.remember_watched(Seen("video", "【MyGO】迷途之子 第3话 完整版"), "哔哩哔哩", "long", NOW)
    assert episode.progress == ""
    store.add_watched_seconds(video.id, 90, NOW)
    assert episode.watch_state() == "看了一部分"
    store.remember_watched(Seen("video", "【MyGO】迷途之子 第3话 完整版", finished=True), "哔哩哔哩", "long", NOW)
    assert (episode.watch_state(), episode.waiting) == ("看完了", False)
    # 在看对方说的那本小说
    store.remember_content(Seen("novel", "田园小当家", "第12章"), "番茄小说", NOW)
    assert novel.watch_state() == "看了一部分"
    # 名字太短（「猫」）不会把什么都当成它
    store.remember_watched(Seen("video", "小奶猫第一次睁眼"), "抖音", "short", NOW)
    assert tiny.watch_state() == "还没看"

    assert [item.title for item in store.later_items()] == ["田园小当家", "猫"]
    lines = store.prompt_lines(NOW)
    assert lines[0] == "对方让你有空看的（还没看完，挑一个去看对方会高兴）：田园小当家（看了一部分）、猫（还没看）"

    # 对方说不用看了；再说一次又算上
    assert store.cancel_later("那个猫", NOW) is None
    assert store.cancel_later("田园小当家", NOW) is novel and novel.watch_state() == "不用看了"
    assert [item.title for item in store.later_items()] == ["猫"]
    assert store.add_later("田园小当家", "", NOW) is novel and novel.waiting


async def test_a_whole_sentence_still_matches_by_the_name_in_book_marks(tmp_path):
    # 实测聊天里的她记成了一整句，和 B 站前后都带字的标题谁也不包含谁，看完了也没对上
    store = MemoryStore(tmp_path / "memory.json")
    wanted = store.add_later("主人让我有空看B站上《春日影》的现场版", "B站", NOW)
    store.remember_watched(
        Seen("video", "【中字4K·HiRes】「春日影」- MyGO!!!!!｜MyGO!!!!! 3rd LIVE", finished=True),
        "哔哩哔哩",
        "long",
        NOW + timedelta(minutes=2),
    )
    assert wanted.watch_state() == "看完了"


async def test_loading_catches_up_on_what_she_already_watched(tmp_path):
    path = tmp_path / "memory.json"
    items = [
        {
            "id": "a",
            "kind": "later",
            "title": "主人让我有空看《春日影》",
            "source": "user",
            "created_at": "2026-11-14T11:22:46",
            "updated_at": "2026-11-14T11:22:46",
        },
        {
            "id": "b",
            "kind": "later",
            "title": "《迷途之子》",
            "source": "user",
            "created_at": "2026-11-14T11:30:00",
            "updated_at": "2026-11-14T11:30:00",
        },
        # 记下之后看完的：补上
        {
            "id": "c",
            "kind": "watched",
            "title": "【MyGO】春日影 现场",
            "app": "哔哩哔哩",
            "form": "long",
            "progress": "finished",
            "seconds": 240,
            "updated_at": "2026-11-14T11:24:18",
        },
        # 记下之前就看过的：不算
        {
            "id": "d",
            "kind": "watched",
            "title": "迷途之子 第1话",
            "app": "哔哩哔哩",
            "form": "long",
            "progress": "partial",
            "seconds": 120,
            "updated_at": "2026-11-14T10:00:00",
        },
    ]
    path.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    store = MemoryStore(path)
    await store.load()
    states = {item.title: item.watch_state() for item in store.items() if item.kind == "later"}
    assert states == {"主人让我有空看《春日影》": "看完了", "《迷途之子》": "还没看"}
