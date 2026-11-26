"""看视频时中途截几张，下一步只裁视频那块给她看，她写下这段画面和字幕上真有什么（D1）。"""

import base64
import io
import json
from typing import Any, List, Optional, Tuple
from unittest.mock import AsyncMock

from PIL import Image, ImageDraw

from phone_play.actions import parse_action
from phone_play.imaging import video_keyframes, video_rows
from phone_play.life import share_text
from phone_play.memory import MemoryStore
from phone_play.settings import PhonePlayConfig
from phone_play.store import SessionRecord

from conftest import FakeDevice
from test_agent import Harness, action
from test_plugin import make_plugin

# 竖屏截图（540x1200）：上面 96~496 行是播放器，其他地方是不动的界面
PLAYER = (96, 496)
TITLE = "番茄炒蛋教程"


def screen(index: int, *, clock: bool = False, rotated: bool = False) -> Image.Image:
    """第 index 张截图：播放器里的画面每张都不一样；clock 为真时状态栏的时间已经跳到下一分钟。"""
    size = (1200, 540) if rotated else (540, 1200)
    image = Image.new("RGB", size, "#f4f4f4")
    draw = ImageDraw.Draw(image)
    level = (30, 120, 210, 75, 165, 255)[index % 6]
    draw.rectangle((0, PLAYER[0], size[0], PLAYER[1]), fill=(level, level, level))
    draw.rectangle((100 + 20 * index, 300, 200 + 20 * index, 400), fill="white")
    if clock:
        draw.rectangle((10, 10, 120, 40), fill="black")
    return image


def png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_the_player_is_where_most_frames_change_and_the_clock_does_not_count():
    # 中途时钟跳了一分钟（之后一直是新的时间）：只变了一次，不算视频
    frames = [screen(index, clock=index >= 2) for index in range(5)]
    top, bottom = video_rows(frames)
    assert PLAYER[0] - 16 <= top <= PLAYER[0] and PLAYER[1] <= bottom <= PLAYER[1] + 16
    # 暂停、黑场：几张都一样，认不出视频在哪
    assert video_rows([screen(0)] * 5) is None
    assert video_rows([screen(0)]) is None


def test_keyframes_keep_only_the_player_and_drop_frames_from_before_a_rotation():
    frames = [(10.0, screen(1, rotated=True)), (20.0, screen(2)), (40.0, screen(3)), (55.0, screen(4))]
    crops = video_keyframes(frames, screen(5), 512, 75)
    assert [at for at, _ in crops] == [20.0, 40.0, 55.0]
    with Image.open(io.BytesIO(crops[0][1])) as image:
        # 只裁播放器那几行（约 400 行高、540 宽），缩到最长边 512
        assert image.width == 512 and 380 <= image.height <= 420
    assert video_keyframes([(10.0, screen(0)), (20.0, screen(0))], screen(0), 512, 75) == []
    assert video_keyframes([], screen(0), 512, 75) == []


class PlayingPhone(FakeDevice):
    """放着视频的手机：每截一张，播放器里的画面就换一下。"""

    def __init__(self) -> None:
        super().__init__()
        self.frames = 0

    async def screenshot(self) -> bytes:
        self.frames += 1
        self.png = png(screen(self.frames))
        return await super().screenshot()


def watching(tmp_path, responses: List[str], device: Optional[FakeDevice] = None, **config: Any) -> Harness:
    settings = PhonePlayConfig()
    for name, value in config.items():
        setattr(settings.play, name, value)
    harness = Harness(tmp_path, device or PlayingPhone(), responses, config=settings)
    harness.agent._memory = MemoryStore(tmp_path / "memory.json")
    return harness


def seen(title: str = TITLE) -> dict:
    return {"type": "video", "title": title, "certain": True}


def frames_in(messages: List[dict]) -> Tuple[List[str], int]:
    """这一步给她看的中途截图：（每张前面写的第几秒，一共几张图）。"""
    content = messages[1]["content"]
    labels = [part["text"] for part in content if part["type"] == "text" and part["text"].startswith("这段的第")]
    return labels, sum(part["type"] == "image_url" for part in content)


async def test_after_watching_she_sees_frames_from_the_middle_and_notes_what_was_there(tmp_path):
    harness = watching(
        tmp_path,
        [
            action(action="wait", seconds=90, video="long", summary="看做菜", seen=seen()),
            action(action="finish", summary="看完了", thought="学到了", saw="  挑偏粉的番茄，烫过去皮\n切小块  "),
        ],
    )
    record = await harness.run()
    first, second = harness.vision_messages
    # 看之前那一步只有当前截图，也不让她写 saw
    assert frames_in(first) == ([], 1) and '"saw"' not in first[0]["content"]
    # 看完 90 秒：中途均匀截了 4 张（每 5 秒看一眼前台时截，第 20、40、55、75 秒），再加现在的整个屏幕
    labels, images = frames_in(second)
    assert labels == ["这段的第 20 秒：", "这段的第 40 秒：", "这段的第 55 秒：", "这段的第 75 秒："]
    assert images == 5
    assert '这一步加 "saw"' in second[0]["content"] and "没有字幕就是不知道里面说了什么" in second[0]["content"]
    url = next(part for part in second[1]["content"] if part["type"] == "image_url")["image_url"]["url"]
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as crop:
        # 只裁了播放器那块
        assert crop.width == 512 and crop.height < 450
    # 写下的记在截图时在看的那个视频名下，进了玩完的记录和聊天里看到的近况
    assert record.saw == [f"《{TITLE}》：挑偏粉的番茄，烫过去皮 切小块"]
    assert any(log["summary"] == "这段视频看到的" for log in harness.agent.status()["logs"])
    assert "那次看视频时画面、字幕上真看到的：《番茄炒蛋教程》：挑偏粉的番茄" in harness.agent.activity_text()


async def test_what_she_saw_stays_with_that_video_and_frames_are_not_reused(tmp_path):
    harness = watching(
        tmp_path,
        [
            action(action="wait", seconds=30, video="long", summary="看做菜", seen=seen()),
            # 这一步划到了下一个视频：写的仍是刚才那段，记在刚才那个视频名下
            action(action="swipe", direction="up", summary="换一个", saw="在切番茄"),
            # 没给中途画面时写的不算（只凭一张截图）
            action(action="finish", summary="不看了", saw="下一个在讲煎蛋"),
        ],
    )
    record = await harness.run()
    # 看 30 秒截 3 张（至少隔 10 秒一张）
    assert frames_in(harness.vision_messages[1])[1] == 4
    assert frames_in(harness.vision_messages[2]) == ([], 1)
    assert record.saw == [f"《{TITLE}》：在切番茄"]
    # 下一步的操作记录里写着上一段看到了什么
    assert "（上一段看到：在切番茄）" in harness.state_texts()[2]


async def test_no_frames_when_turned_off_or_when_the_picture_does_not_move(tmp_path):
    turned_off = watching(
        tmp_path,
        [action(action="wait", seconds=90, video="long", seen=seen()), action(action="finish", saw="看到了")],
        video_keyframes=0,
    )
    record = await turned_off.run()
    assert turned_off.device.frames == 2 and frames_in(turned_off.vision_messages[1]) == ([], 1)
    assert record.saw == []

    # 画面一直没变（暂停了）：截了也认不出视频在哪，不给她看，也就不记她写的
    paused = watching(
        tmp_path / "paused",
        [action(action="wait", seconds=15, video="short", seen=seen()), action(action="finish", saw="看到了")],
        device=FakeDevice(),
    )
    (tmp_path / "paused").mkdir()
    record = await paused.run()
    # 看 15 秒只截 1 张
    assert sum(call == ("screenshot",) for call in paused.device.calls) == 3
    assert frames_in(paused.vision_messages[1]) == ([], 1) and record.saw == []


def test_saw_is_one_short_line():
    raw = {"action": "wait", "seconds": 30, "saw": " 先挑番茄\n 再打蛋 "}
    reply = parse_action(json.dumps(raw, ensure_ascii=False))
    assert reply.saw == "先挑番茄 再打蛋"
    assert len(parse_action(json.dumps({"action": "finish", "saw": "长" * 300})).saw) == 120
    assert parse_action(json.dumps({"action": "finish"})).saw == ""


def record_with(saw: List[str]) -> SessionRecord:
    return SessionRecord(
        started_at="2026-11-26T21:00:00",
        ended_at="2026-11-26T21:10:00",
        trigger="自己想玩",
        app="哔哩哔哩",
        goal="找点做饭视频看看",
        summary="学到了番茄炒蛋的小技巧",
        steps=8,
        end_reason="自己不想玩了",
        saw=saw,
    )


async def test_what_she_saw_reaches_the_chat_and_mai_life(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.ctx.maisaka.proactive.trigger = AsyncMock(return_value={"success": True})
    await plugin._notify_finished(record_with([f"《{TITLE}》：挑偏粉的番茄，去皮切块"]), "stream-1")
    intent = plugin.ctx.maisaka.proactive.trigger.await_args.args[1]
    assert f"看视频时画面、字幕上真看到的：《{TITLE}》：挑偏粉的番茄，去皮切块。说到视频里的内容只按这些说" in intent
    await plugin._notify_finished(record_with([]), "stream-1")
    assert "真看到的" not in plugin.ctx.maisaka.proactive.trigger.await_args.args[1]

    topic, _ = share_text(record_with([f"《{TITLE}》：挑偏粉的番茄"]))
    assert topic == f"刚才自己刷哔哩哔哩看到的：学到了番茄炒蛋的小技巧（《{TITLE}》：挑偏粉的番茄）"
    # 旧记录没有这个字段
    old = SessionRecord.from_dict({key: value for key, value in record_with([]).to_dict().items() if key != "saw"})
    assert old.saw == []
