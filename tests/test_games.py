"""她想装游戏：先问主人，主人同意后自己去应用商店装，装好的和白名单里的应用一样能玩（C6）。"""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import AsyncMock

import json
import re
import pytest

from phone_play import plugin as plugin_module
from phone_play.actions import PhoneAction
from phone_play.agent import APP_STORES, PhoneAgent, PlayRefused, _check_store_tap
from phone_play.games import (
    WISH_APPROVED,
    WISH_ASKING,
    WISH_DECLINED,
    WISH_EXPIRED,
    WISH_FAILED,
    WISH_INSTALLED,
    GameStore,
    GameWish,
)
from phone_play.memory import MemoryStore
from phone_play.settings import PhonePlayConfig
from phone_play.store import SessionRecord
from phone_play.ui_tree import UIElement

from conftest import FakeDevice
from test_agent import Harness, action, life
from test_plugin import command, make_plugin

NOW = datetime(2026, 11, 26, 20, 0)
GAME = "com.example.mahjong"
MARKET = "com.heytap.market"
WISH_PLAN = json.dumps({"wish": "《雀魂麻将》", "reason": "想打麻将"}, ensure_ascii=False)
GET_BUTTON = UIElement(index=1, label="获取", kind="Button", clickable=True, bounds=(400, 300, 520, 360))
PRICE_BUTTON = UIElement(index=2, label="¥6.00", kind="Button", clickable=True, bounds=(400, 500, 520, 560))


# ===== 游戏记录 =====


def test_she_asks_at_most_once_a_day_and_only_one_game_at_a_time():
    store = GameStore(path=None)  # type: ignore[arg-type]
    assert store.can_wish(NOW)
    wish = store.add_wish(" 雀魂麻将 ", " 想打麻将 ", "iPhone", NOW)
    assert (wish.name, wish.reason, wish.status) == ("雀魂麻将", "想打麻将", WISH_ASKING)
    assert store.open_wish(NOW) is wish
    # 还在等回答、正在装的时候都不再问；今天问过了明天才能再问
    assert not store.can_wish(NOW)
    store.answer(wish, True, NOW)
    assert wish.status == WISH_APPROVED and not store.can_wish(NOW + timedelta(days=2))
    store.finish(wish, "", "iPhone", "找不到这款", NOW)
    assert (wish.status, wish.note) == (WISH_FAILED, "找不到这款")
    assert not store.can_wish(NOW + timedelta(hours=1))
    assert store.can_wish(NOW + timedelta(days=1))


def test_wishes_the_owner_never_answered_expire_after_a_day():
    store = GameStore(path=None)  # type: ignore[arg-type]
    wish = store.add_wish("雀魂麻将", "", "iPhone", NOW)
    assert store.open_wish(NOW + timedelta(hours=23)) is wish
    assert store.open_wish(NOW + timedelta(days=1)) is None
    assert wish.status == WISH_EXPIRED and wish.label == "主人没回，算了"


def test_declined_games_are_not_asked_again_for_30_days():
    store = GameStore(path=None)  # type: ignore[arg-type]
    wish = store.add_wish("雀魂麻将", "", "iPhone", NOW)
    store.answer(wish, False, NOW)
    assert wish.status == WISH_DECLINED
    assert store.declined_names(NOW + timedelta(days=29)) == ["雀魂麻将"]
    # 书名号、空格不同也认得是同一款
    assert store.is_declined("《雀魂麻将》", NOW + timedelta(days=1))
    assert store.declined_names(NOW + timedelta(days=30)) == []


def test_installed_games_are_kept_per_phone_and_can_be_removed():
    store = GameStore(path=None)  # type: ignore[arg-type]
    wish = store.add_wish("雀魂麻将", "", "iPhone", NOW)
    store.answer(wish, True, NOW)
    # 问的时候用的 iPhone，主人同意时换成了安卓：记在真正装上的那台
    game = store.finish(wish, GAME, "安卓", "装好了", NOW)
    assert game is not None and (game.name, game.package, game.device) == ("雀魂麻将", GAME, "安卓")
    assert (wish.status, wish.note, wish.device) == (WISH_INSTALLED, GAME, "安卓")
    [app] = store.apps_for("安卓")
    assert (app.package, app.name) == (GAME, "雀魂麻将") and "你自己装的游戏《雀魂麻将》" in app.note
    assert store.apps_for("iPhone") == []
    assert store.remove_game(GAME) and store.games() == []
    assert not store.remove_game(GAME)


async def test_game_records_survive_a_reload_and_broken_files_are_backed_up(tmp_path):
    path = tmp_path / "games.json"
    store = GameStore(path)
    wish = store.add_wish("雀魂麻将", "想打麻将", "iPhone", NOW)
    store.answer(wish, True, NOW)
    store.finish(wish, GAME, "iPhone", "装好了", NOW)
    await store.save()
    loaded = GameStore(path)
    await loaded.load()
    assert [item.status for item in loaded.wishes()] == [WISH_INSTALLED]
    assert [item.package for item in loaded.games()] == [GAME]

    path.write_text("{坏掉了", encoding="utf-8")
    broken = GameStore(path)
    await broken.load()
    assert broken.games() == [] and "已损坏" in broken.recovered
    assert not path.exists() and len(list(tmp_path.glob("games.json.broken-*"))) == 1


# ===== 想装游戏 =====


def wish_harness(tmp_path, plan: Any, **kwargs: Any) -> Harness:
    config = PhonePlayConfig()
    config.auto_play.game_wishes = True
    harness = Harness(tmp_path, FakeDevice(), kwargs.pop("responses", []), config=config, plan=plan, **kwargs)
    harness.games = GameStore(tmp_path / "games.json")
    harness.asked: List[GameWish] = []

    async def ask(wish: GameWish) -> None:
        harness.asked.append(wish)

    harness.agent._games = harness.games
    harness.agent._wish_asker = ask
    return harness


async def start_and_wait(harness: Harness, **start_args: Any) -> None:
    await harness.agent.start(**start_args)
    await harness.wait_done()
    await harness.settle()


async def test_when_she_plays_on_her_own_she_can_ask_for_a_game_instead(tmp_path):
    harness = wish_harness(tmp_path, WISH_PLAN)
    await start_and_wait(harness, trigger="auto")
    assert '{"wish": "游戏名"' in harness.plan_prompts[0] and "塔防这类太快" in harness.plan_prompts[0]
    # 这次不玩：没打开应用，不算一次；记下来、去问主人
    assert ("launch", "tv.danmaku.bili") not in harness.device.actions()
    harness.assert_not_counted()
    [wish] = harness.games.wishes()
    assert (wish.name, wish.reason, wish.status) == ("雀魂麻将", "想打麻将", WISH_ASKING)
    assert harness.asked == [wish]
    logs = harness.agent.status()["logs"]
    assert any(log["summary"] == "结束：想装《雀魂麻将》，先问主人能不能装" for log in logs)
    assert json.loads((tmp_path / "games.json").read_text(encoding="utf-8"))["wishes"][0]["name"] == "雀魂麻将"


@pytest.mark.parametrize(
    ("trigger", "enabled", "slack"),
    [("command", True, False), ("chat", True, False), ("auto", False, False), ("auto", True, True)],
)
async def test_she_only_asks_when_playing_on_her_own(tmp_path, trigger, enabled, slack):
    harness = wish_harness(tmp_path, harness_plan(), responses=[action(summary="不看了", action="finish")])
    harness.config.auto_play.game_wishes = enabled
    if slack:
        # 摸鱼只有几分钟，不去问
        await start_and_wait(harness, trigger=trigger, life=life("work", "上班", 60), slack=True)
    else:
        await start_and_wait(harness, trigger=trigger)
    assert '"wish"' not in harness.plan_prompts[0]


def harness_plan() -> str:
    return json.dumps({"app": "tv.danmaku.bili", "goal": "看猫猫视频", "minutes": 5}, ensure_ascii=False)


async def test_she_does_not_ask_twice_a_day_or_for_a_declined_game(tmp_path):
    harness = wish_harness(tmp_path, harness_plan(), responses=[action(summary="不看了", action="finish")])
    harness.games.add_wish("别的游戏", "", "手机", datetime.now())
    await start_and_wait(harness, trigger="auto")
    assert '"wish"' not in harness.plan_prompts[0]

    harness = wish_harness(tmp_path / "declined", WISH_PLAN)
    old = harness.games.add_wish("雀魂麻将", "", "手机", datetime.now() - timedelta(days=3))
    harness.games.answer(old, False, datetime.now() - timedelta(days=3))
    await start_and_wait(harness, trigger="auto")
    assert "这些主人说过不装，别再问：《雀魂麻将》" in harness.plan_prompts[0]
    # 照样想要这款：不去问，这次也不玩
    assert harness.asked == [] and len(harness.games.wishes()) == 1
    assert any("主人说过不装" in log["summary"] for log in harness.agent.status()["logs"])


# ===== 主人同意后去装 =====


class StorePhone(FakeDevice):
    """安卓：查得到装了哪些应用；在商店里点「获取」后游戏装好并打开。"""

    supports_lock = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(installed={MARKET, "tv.danmaku.bili"}, elements=[GET_BUTTON, PRICE_BUTTON], **kwargs)
        self.default_foreground = MARKET

    async def all_packages(self):
        return set(self.installed)

    async def tap(self, x: int, y: int) -> None:
        await super().tap(x, y)
        self.installed.add(GAME)
        self.default_foreground = GAME


def download_harness(tmp_path, device: FakeDevice, responses: List[str]) -> Harness:
    harness = wish_harness(tmp_path, harness_plan(), responses=responses)
    harness.device = device
    harness.wish = harness.games.add_wish("雀魂麻将", "想打麻将", "iPhone", datetime.now())
    harness.games.answer(harness.wish, True, datetime.now())
    harness.agent._memory = MemoryStore(tmp_path / "memory.json")
    return harness


async def test_after_the_owner_agrees_she_installs_the_game_from_the_store(tmp_path):
    harness = download_harness(
        tmp_path, StorePhone(), [action(thought="是免费的", summary="点获取", action="tap", element=1)]
    )
    record = await harness.run(
        trigger="download", download="雀魂麻将", download_wish=harness.wish.id, origin_stream="s"
    )
    actions = harness.device.actions()
    assert ("launch", MARKET) in actions and ("tap", 460, 330) in actions
    # 只打开商店，不问模型玩什么
    assert harness.plan_prompts == []
    assert (record.trigger, record.app, record.end_reason) == ("下载游戏", "软件商店", "装好了《雀魂麻将》")
    assert record.goal == "在软件商店里找到《雀魂麻将》，确认是免费的再安装，装好后点「打开」"
    # 实测：总结别用中途「等它装好」的想法；也不记成没做完，下次规划时她不会想回商店接着装
    assert record.summary == "在应用商店里装好了《雀魂麻将》，还没开始玩"
    assert not any(item.kind == "unfinished" for item in harness.agent._memory.items())
    # 只有一步问了模型：第二步看到前台换成了新游戏就算装好
    assert len(harness.vision_messages) == 1
    rules = json.dumps(harness.vision_messages[0], ensure_ascii=False)
    assert "这次是主人同意你装游戏《雀魂麻将》" in rules and "软件商店" in rules
    assert harness.wish.status == WISH_INSTALLED
    [game] = harness.games.games()
    assert (game.package, game.device) == (GAME, harness.agent.status()["device_name"])
    assert harness.agent.status()["games"]["games"][0]["name"] == "雀魂麻将"

    # 以后她自己想玩时，装好的游戏和白名单里的应用一起给她挑
    harness.plans = [json.dumps({"app": GAME, "goal": "打一局麻将", "minutes": 5}, ensure_ascii=False)]
    harness.responses = [action(summary="打完了", action="finish")]
    harness.device.default_foreground = GAME
    record = await harness.run(trigger="command")
    assert "你自己装的游戏《雀魂麻将》" in harness.plan_prompts[-1]
    # 她自己装的游戏算游戏：不带看视频那套规则，说联机房间怎么等
    game_rules = harness.vision_messages[-1][0]["content"]
    assert "看视频分短视频和长视频" not in game_rules and "在联机游戏的房间里" in game_rules
    assert (record.app, record.trigger) == ("雀魂麻将", "命令")


async def test_she_does_not_tap_paid_buttons_while_installing(tmp_path):
    harness = download_harness(
        tmp_path,
        StorePhone(),
        [
            action(thought="要买吗", summary="点价格", action="tap", element=2),
            action(thought="要付费，算了", summary="这款要付费，没装", action="finish"),
        ],
    )
    record = await harness.run(trigger="download", download="雀魂麻将", download_wish=harness.wish.id)
    assert not any(call[0] == "tap" for call in harness.device.actions())
    assert any("「¥6.00」要花钱" in log["result"] for log in harness.agent.status()["logs"])
    assert harness.wish.status == WISH_FAILED and harness.games.games() == []
    assert record.summary.startswith("这款要付费，没装")
    assert not any(item.kind == "unfinished" for item in harness.agent._memory.items())


# iPhone 实测 App Store 搜索「数独」的界面元素（英文按钮；第一条是广告）
STORE_RESULTS = [
    UIElement(
        index=6, label="广告, Sudoku - Brain Puzzle Game", kind="Cell", clickable=True, bounds=(0, 519, 1170, 1305)
    ),
    UIElement(
        index=7, label="广告, Sudoku - Brain Puzzle Game", kind="Button", clickable=True, bounds=(60, 519, 1110, 783)
    ),
    UIElement(index=8, label="Get, In-App Purchases", kind="Button", clickable=True, bounds=(888, 519, 1110, 699)),
    UIElement(
        index=11, label="Sudoku.com - Number Games, Num", kind="Cell", clickable=True, bounds=(0, 1449, 1170, 2472)
    ),
    UIElement(
        index=12, label="Sudoku.com - Number Games, Num", kind="Button", clickable=True, bounds=(60, 1449, 1110, 1713)
    ),
    UIElement(index=13, label="Get, In-App Purchases", kind="Button", clickable=True, bounds=(888, 1449, 1110, 1629)),
]


def test_free_games_with_in_app_purchases_can_be_installed_but_ads_cannot():
    # 免费、带 App 内购买的：「Get」能点，点开详情也能点
    _check_store_tap(PhoneAction(kind="tap", element=13), STORE_RESULTS, 1170, 2532)
    _check_store_tap(PhoneAction(kind="tap", element=12), STORE_RESULTS, 1170, 2532)
    # 广告卡片里的「Get」自己没写广告，外面那张卡片写着：按编号、按坐标点都拦
    for tap in (PhoneAction(kind="tap", element=8), PhoneAction(kind="tap", x=854, y=241)):
        with pytest.raises(Exception, match="是广告，不是要装的那款"):
            _check_store_tap(tap, STORE_RESULTS, 1170, 2532)
    with pytest.raises(Exception, match="是广告"):
        _check_store_tap(PhoneAction(kind="tap", element=7), STORE_RESULTS, 1170, 2532)


def test_paid_buttons_are_blocked_even_with_notes_after_them():
    cell = UIElement(
        index=3, label="雀魂麻将 休闲益智 ¥6.00 App 内购买", kind="Cell", clickable=True, bounds=(0, 0, 540, 200)
    )
    _check_store_tap(PhoneAction(kind="tap", element=3), [cell], 540, 1200)
    _check_store_tap(PhoneAction(kind="tap", element=1), [GET_BUTTON], 540, 1200)
    for label in ("¥6.00", "￥ 30", "$0.99", "¥6.00, In-App Purchases", "购买", "订阅", "立即支付", "Buy", "Subscribe"):
        button = UIElement(index=4, label=label, kind="Button", clickable=True, bounds=(400, 300, 520, 360))
        with pytest.raises(Exception, match="要花钱"):
            # 按坐标点也认得
            _check_store_tap(PhoneAction(kind="tap", x=850, y=275), [button], 540, 1200)


async def test_she_only_opens_the_store_while_installing(tmp_path):
    harness = download_harness(
        tmp_path,
        StorePhone(),
        [
            action(thought="先刷会儿B站", summary="打开B站", action="launch", app="tv.danmaku.bili"),
            action(summary="装不了", action="finish"),
        ],
    )
    await harness.run(trigger="download", download="雀魂麻将", download_wish=harness.wish.id)
    assert ("launch", "tv.danmaku.bili") not in harness.device.actions()
    assert any("不在可以打开的应用里" in log["result"] for log in harness.agent.status()["logs"])


async def test_without_a_known_store_she_cannot_install(tmp_path):
    harness = download_harness(tmp_path, FakeDevice(installed={"tv.danmaku.bili"}), [])
    await harness.agent.start(trigger="download", download="雀魂麻将", download_wish=harness.wish.id)
    with pytest.raises(PlayRefused) as refused:
        await harness.agent.wait_for_plan(5)
    assert refused.value.chat_text == "手机上没找到应用商店，装不了"
    await harness.wait_done()
    assert harness.wish.status == WISH_FAILED and "没找到认得的应用商店" in harness.wish.note
    harness.assert_not_counted()


def test_new_apps_are_recognized_on_both_kinds_of_phone():
    android = SimpleNamespace(download="雀魂麻将", current=object(), packages_before={MARKET, "com.android.settings"})
    assert PhoneAgent._newly_installed(android, GAME)
    assert not PhoneAgent._newly_installed(android, "com.android.settings")
    # iPhone 查不到装了哪些：系统自带的（比如商店跳去设置）不算
    iphone = SimpleNamespace(download="雀魂麻将", current=object(), packages_before=None)
    assert PhoneAgent._newly_installed(iphone, GAME)
    assert not PhoneAgent._newly_installed(iphone, "com.apple.Preferences")
    # 不是下载、还没打开商店的都不算
    assert not PhoneAgent._newly_installed(SimpleNamespace(download="", current=object(), packages_before=None), GAME)
    assert not PhoneAgent._newly_installed(SimpleNamespace(download="x", current=None, packages_before=None), GAME)
    assert [app.package for app in APP_STORES["ios_wda"]] == ["com.apple.AppStore"]


# ===== 插件：问主人、主人回答 =====


async def loaded_plugin(tmp_path, monkeypatch, **overrides: Any):
    monkeypatch.setattr(plugin_module, "create_device", lambda settings: FakeDevice())
    plugin = make_plugin(tmp_path, **overrides)
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": True})
    plugin.ctx.chat.get_stream_by_user_id = AsyncMock(return_value={"session_id": "owner-stream"})
    await plugin.on_load()
    return plugin


@pytest.mark.parametrize("style", ["natural", "template"])
async def test_she_asks_the_owner_in_their_private_chat(tmp_path, monkeypatch, style):
    plugin = await loaded_plugin(tmp_path, monkeypatch, play={"finish_notice": style})
    try:
        wish = plugin._games.add_wish("雀魂麻将", "想打麻将", "手机", datetime.now())
        await plugin._ask_owner_for_game(wish)
        assert wish.stream_id == "owner-stream"
        if style == "natural":
            stream, intent = plugin.ctx.maisaka.proactive.trigger.await_args.args
            assert stream == "owner-stream" and "《雀魂麻将》（想打麻将）" in intent and "只装免费的" in intent
        else:
            text = plugin.ctx.send.text.await_args.args[0]
            assert text.startswith("📱 小睦：想玩《雀魂麻将》（想打麻将）") and "回我「可以」或「不装」" in text
        # 在那个聊天里每一轮提醒她在等回答；别的聊天不提
        hooked = await plugin.inject_planner_context(items=[], session_id="owner-stream")
        note = hooked["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert "《雀魂麻将》" in note and "answer_game_wish" in note
        assert await plugin.inject_planner_context(items=[], session_id="group") == {"action": "continue"}
        replied = await plugin.inject_replyer_context(extra_prompt="", session_id="owner-stream")
        assert "在等回答" in replied["modified_kwargs"]["extra_prompt"]
        assert "🎮 想装《雀魂麻将》，在等主人回" in await command(plugin, "/手机")
    finally:
        await plugin.on_unload()


async def test_the_owner_answers_in_chat(tmp_path, monkeypatch):
    plugin = await loaded_plugin(tmp_path, monkeypatch)
    try:
        start = AsyncMock()
        monkeypatch.setattr(plugin.agent, "start", start)
        reply = await plugin.tool_answer_game_wish(approved=True, stream_id="owner-stream", user_id="10001")
        assert reply == {"success": False, "error": "现在没有在等主人回答的游戏"}

        wish = plugin._games.add_wish("雀魂麻将", "", "手机", datetime.now())
        # 只有主人的回答算数
        reply = await plugin.tool_answer_game_wish(approved=True, stream_id="group", user_id="20002")
        assert reply["success"] is False and wish.status == WISH_ASKING
        # 现在开始不了：还在等，主人等会儿再说一次
        start.side_effect = PlayRefused("已经在玩手机了", chat_text="你已经在玩手机了（看猫猫）")
        reply = await plugin.tool_answer_game_wish(approved=True, stream_id="owner-stream", user_id="10001")
        assert reply["error"] == "现在去装不了：你已经在玩手机了（看猫猫）" and wish.status == WISH_ASKING

        start.side_effect = None
        reply = await plugin.tool_answer_game_wish(approved=True, stream_id="owner-stream", user_id="10001")
        assert reply["success"] and "去应用商店装《雀魂麻将》" in reply["content"]
        start.assert_awaited_with(
            trigger="download", origin_stream="owner-stream", download="雀魂麻将", download_wish=wish.id
        )
        assert wish.status == WISH_APPROVED
        saved = json.loads((tmp_path / "data" / "games.json").read_text(encoding="utf-8"))
        assert saved["wishes"][0]["status"] == WISH_APPROVED
    finally:
        await plugin.on_unload()


async def test_the_owner_can_answer_and_remove_games_in_the_webui(tmp_path, monkeypatch):
    plugin = await loaded_plugin(tmp_path, monkeypatch)
    try:
        wish = plugin._games.add_wish("雀魂麻将", "", "手机", datetime.now())
        answered = await plugin.webui_game_wish(approved=False)
        assert "30 天内不会再问这款" in answered["content"] and wish.status == WISH_DECLINED
        with pytest.raises(ValueError, match="没有在等主人回答"):
            await plugin.webui_game_wish(approved=True)
        status = await plugin.webui_status()
        assert status["games"]["wishes"][0]["label"] == "主人说不装"

        plugin._games.add_game("雀魂麻将", GAME, "手机", datetime.now())
        assert await plugin.webui_game_remove(package=GAME) == {"removed": GAME}
        with pytest.raises(ValueError, match="已经不在了"):
            await plugin.webui_game_remove(package=GAME)
    finally:
        await plugin.on_unload()


async def test_she_says_whether_the_game_got_installed(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": True})
    goal = "在App Store里找到《雀魂麻将》，确认是免费的再安装，装好后点「打开」"
    done = SessionRecord("", "", "下载游戏", "App Store", goal, "点了获取", 4, "装好了《雀魂麻将》")
    await plugin._notify_finished(done, "owner-stream")
    intent = plugin.ctx.maisaka.proactive.trigger.await_args.args[1]
    assert "装好了游戏《雀魂麻将》" in intent and "谢谢对方" in intent and "别编玩起来怎么样" in intent

    failed = SessionRecord("", "", "下载游戏", "App Store", goal, "要输 Apple ID 密码", 30, "玩够 15 分钟了")
    await plugin._notify_finished(failed, "owner-stream")
    intent = plugin.ctx.maisaka.proactive.trigger.await_args.args[1]
    assert "没装上" in intent and "要输 Apple ID 密码" in intent and "别说装好了" in intent


def test_every_page_button_reaches_a_plugin_api(tmp_path):
    """页面上点的按钮都在 webui.json 里登记过，登记的接口插件里都有：新加的「同意」「不装」「不让她玩了」不是空壳。"""
    root = Path(plugin_module.__file__).parent
    page = json.loads((root / "webui.json").read_text(encoding="utf-8"))["pages"][0]
    html = (root / "web" / "live.html").read_text(encoding="utf-8")
    used = set(re.findall(r"\brun\('([a-z_]+)'", html))
    assert {"game_wish", "game_remove"} <= used <= set(page["actions"])
    apis = {component["name"] for component in make_plugin(tmp_path).get_components()}
    assert {binding["api"] for binding in page["actions"].values()} <= apis
