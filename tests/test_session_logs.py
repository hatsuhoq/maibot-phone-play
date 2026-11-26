from datetime import datetime

import json
import logging

from phone_play.session_logs import SessionLogStore


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_lines_are_written_in_order_and_only_recent_sessions_are_kept(tmp_path):
    store = SessionLogStore(tmp_path / "logs", logging.getLogger("phone_play_test"), max_sessions=2)
    names = []
    for minute in range(3):
        name = store.begin(datetime(2026, 11, 26, 13, minute), {"trigger": "聊天中决定"})
        for step in range(5):
            store.add(name, {"type": "step", "step": step})
        store.add(name, {"type": "end", "end_reason": "玩够 10 分钟了"})
        names.append(name)
    await store.flush()
    assert sorted(path.name for path in (tmp_path / "logs").iterdir()) == [f"{names[1]}.jsonl", f"{names[2]}.jsonl"]
    lines = read_lines(tmp_path / "logs" / f"{names[2]}.jsonl")
    assert [line["type"] for line in lines] == ["session"] + ["step"] * 5 + ["end"]
    assert [line["step"] for line in lines if line["type"] == "step"] == [0, 1, 2, 3, 4]
    assert lines[0] == {"type": "session", "started_at": "2026-11-26T13:02:00", "trigger": "聊天中决定"}


async def test_a_log_that_cannot_be_written_is_reported_and_does_not_raise(tmp_path, caplog):
    # 文件夹的位置被一个文件占了，建不了文件夹
    blocked = tmp_path / "logs"
    blocked.write_text("不是文件夹", encoding="utf-8")
    store = SessionLogStore(blocked, logging.getLogger("phone_play_test"))
    store.add(store.begin(datetime(2026, 11, 26, 13, 0), {}), {"type": "step"})
    with caplog.at_level(logging.WARNING):
        await store.flush()
    assert "操作记录写不进" in caplog.text
