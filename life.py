"""和麦生活（Mai_life）联动：自动玩按它的日程决定玩不玩、玩多久，玩完把看到的交给它分享。

麦生活通过公开 API 提供日程（maibot.schedule 协议 v1），见它仓库里的 ``docs/schedule-provider-api.md``；
``create_proactive_opportunity`` 收外部的分享契机，由它按私聊用户的额度、免打扰决定提不提、跟谁提。
插件之间经宿主 ``api.call`` 调用：调用失败（没装、被禁用、接口不存在）时 SDK 返回宿主的
``{"success": false, "error": ...}``；调用成功时返回麦生活自己的结果（日程是 ``{"success", "error", "data"}``
信封，分享契机是 ``{"success", "opportunity_id"}`` 或 ``{"success": false, "error": 错误码}``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from .store import SessionRecord

MAI_LIFE_PLUGIN = "maibot-community.mai-life"
SNAPSHOT_API = f"{MAI_LIFE_PLUGIN}.get_schedule_snapshot"
SNAPSHOT_API_VERSION = "1"
SHARE_API = f"{MAI_LIFE_PLUGIN}.create_proactive_opportunity"
SHARE_API_VERSION = "1"
# 交给麦生活的契机多久后作废：「刚才看到的」过了两个小时就不新鲜了（和麦生活的默认值一样）
SHARE_EXPIRES_MINUTES = 120
# 上班、上课时偷偷玩的那几局：操作记录、游玩记录和状态里的触发来源都写这个
SLACK_LABEL = "摸鱼"
# 麦生活截断的长度
_TOPIC_LIMIT = 160
_MOTIVE_LIMIT = 240
_SHARE_ERRORS = {
    "not_ready_or_empty_topic": "麦生活还没就绪",
    "no_current_framework": "麦生活的日程这会儿是空档，它只在有日程时收分享",
    "opportunity_id_conflict": "麦生活那边的契机编号冲突了",
}

# 日程段型（协议里的枚举）和给人看的名字
KIND_LABELS: Dict[str, str] = {
    "sleep": "睡觉",
    "nap": "午睡",
    "rest": "休息",
    "study": "学习",
    "work": "工作",
    "leisure": "休闲",
    "meal": "吃饭",
    "travel": "在路上",
}


class LifeLinkError(Exception):
    """查不到能用的日程：没装麦生活、它还没就绪、当天日程还没生成，或者回复格式不对。"""


@dataclass(frozen=True)
class LifeActivity:
    """麦生活日程里当前这一段。"""

    kind: str
    name: str
    location: str
    scene: str
    # 查询那一刻这段还剩多少秒；用两边各自给的时刻相减，不受两边时区设置不同影响
    remaining_seconds: float

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def describe(self) -> str:
        """「看悬疑剧」（休闲 · 客厅）"""
        where = f" · {self.location}" if self.location else ""
        return f"「{self.name}」（{self.kind_label}{where}）"


def _time(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise LifeLinkError(f"麦生活返回的 {field} 不是 ISO 时间：{value!r}") from exc
    if parsed.tzinfo is None:
        raise LifeLinkError(f"麦生活返回的 {field} 没带时区：{value!r}")
    return parsed


def parse_snapshot(response: Any) -> Optional[LifeActivity]:
    """解析 ``get_schedule_snapshot`` 经 SDK 返回的结果，得到当前这一段；两段之间的空档返回 None。

    Raises:
        LifeLinkError: 调用失败、麦生活报错、当天日程还没生成或格式不对。
    """
    if not isinstance(response, dict):
        raise LifeLinkError(f"麦生活返回的不是对象：{response!r}"[:200])
    if "data" not in response:
        # 宿主那一层就失败了：通常是没装麦生活或它被禁用
        raise LifeLinkError(f"没连上麦生活：{response.get('error') or '调用失败'}")
    if response.get("success") is not True:
        raise LifeLinkError(f"麦生活：{response.get('error') or '查询失败'}")
    data = response["data"]
    if not isinstance(data, dict):
        raise LifeLinkError("麦生活返回的 data 不是对象")
    if not data.get("schedule_available"):
        raise LifeLinkError("麦生活今天的日程还没生成")
    current = data.get("current")
    if current is None:
        return None
    if not isinstance(current, dict):
        raise LifeLinkError("麦生活返回的 current 不是对象")
    as_of = _time(data.get("as_of"), "as_of")
    end_at = _time(current.get("end_at"), "end_at")
    return LifeActivity(
        kind=str(current.get("kind") or ""),
        name=str(current.get("name") or "").strip(),
        location=str(current.get("location") or "").strip(),
        scene=str(current.get("scene") or "").strip(),
        remaining_seconds=max(0.0, (end_at - as_of).total_seconds()),
    )


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def share_text(record: SessionRecord) -> Tuple[str, str]:
    """玩完交给麦生活的契机：话题是在哪看到了什么（她自己的总结），动机说明是她自己玩的、可以不说。"""
    seen = record.summary or record.goal
    if record.trigger == SLACK_LABEL:
        topic = _clip(f"刚才摸鱼偷偷刷{record.places_text()}看到的：{seen}", _TOPIC_LIMIT)
        motive = "上班或上课中途偷偷摸鱼刷到的，觉得有意思可以当见闻随口聊聊；不想说也可以不说"
    else:
        topic = _clip(f"刚才自己刷{record.places_text()}看到的：{seen}", _TOPIC_LIMIT)
        motive = "空闲时自己拿起手机刷到的，觉得有意思可以当见闻随口聊聊；不想说也可以不说"
    if record.interactions:
        motive += f"。还用自己的账号{'、'.join(record.interactions)}"
    return topic, _clip(motive, _MOTIVE_LIMIT)


def parse_share_result(response: Any) -> str:
    """解析 ``create_proactive_opportunity`` 经 SDK 返回的结果，返回契机编号。

    Raises:
        LifeLinkError: 调用失败或麦生活没收下。
    """
    if not isinstance(response, dict):
        raise LifeLinkError(f"麦生活返回的不是对象：{response!r}"[:200])
    if response.get("success") is True and response.get("opportunity_id"):
        return str(response["opportunity_id"])
    error = str(response.get("error") or "")
    if error in _SHARE_ERRORS:
        raise LifeLinkError(_SHARE_ERRORS[error])
    # 不是麦生活的错误码：宿主那一层就失败了（没装、被禁用）
    raise LifeLinkError(f"没交给麦生活：{error or '调用失败'}")
