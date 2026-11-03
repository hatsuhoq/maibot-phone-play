from datetime import datetime, timedelta
from PIL import Image

import json

from phone_play.diagnostics import DiagnosticStore


def screen() -> Image.Image:
    return Image.new("RGB", (1170, 2532), "#336699")


async def test_keeps_only_recent_small_records(tmp_path):
    store = DiagnosticStore(tmp_path / "diagnostics", max_items=3)
    ids = [await store.add(screen(), (585, 1200), {"step": step, "result": f"失败 {step}"}) for step in range(5)]
    entries = await store.entries()
    # 只留最近 3 条，最新的在前；多出来的图片文件也删掉
    assert [entry["id"] for entry in entries] == list(reversed(ids[-3:]))
    assert sorted(path.stem for path in (tmp_path / "diagnostics").glob("*.jpg")) == sorted(ids[-3:])
    image = await store.image(ids[-1])
    # 画面缩小后存，不留原始大图
    assert image[:2] == b"\xff\xd8" and len(image) < 60 * 1024


async def test_expired_records_are_pruned_and_can_be_cleared(tmp_path):
    folder = tmp_path / "diagnostics"
    store = DiagnosticStore(folder, max_age_seconds=3600)
    old_id = await store.add(screen(), None, {"step": 1})
    index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    index[0]["created_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    (folder / "index.json").write_text(json.dumps(index), encoding="utf-8")
    new_id = await store.add(screen(), (1, 2, 300, 400), {"step": 2})
    assert [entry["id"] for entry in await store.entries()] == [new_id]
    assert await store.image(old_id) is None
    assert await store.clear() == 1
    assert await store.entries() == [] and list(folder.glob("*.jpg")) == []


async def test_unknown_ids_never_reach_the_file_system(tmp_path):
    store = DiagnosticStore(tmp_path / "diagnostics")
    await store.add(screen(), None, {"step": 1})
    assert await store.image("../usage") is None
    assert await store.image("nope") is None
