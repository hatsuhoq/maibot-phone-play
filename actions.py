"""视觉模型输出的动作：解析、校验与坐标换算。

坐标约定：模型用 ``"point": [x, y]`` 给出 0~1000 的相对坐标（左上角为原点），
这样即使宿主为了适配模型缩放了截图，坐标也不会错位。用坐标对而不是分开的 x、y 字段，
是因为 Qwen-VL 这类模型习惯按坐标对输出，分开写时常漏掉 y 的键名（``"x":933,910``）。
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

import json
import re

from .settings import AllowedApp
from .ui_tree import UIElement

ACTION_KINDS = (
    "tap",
    "long_press",
    "swipe",
    "type",
    "enter",
    "back",
    "home",
    "launch",
    "wait",
    "wait_turn",
    "like",
    "comment",
    "follow",
    "finish",
)
# 点在某个位置上的动作：点赞、关注也是点一下，单独列出来是为了让代码能按额度限制
TAP_KINDS = ("tap", "long_press", "like", "follow")
SWIPE_DIRECTIONS = ("up", "down", "left", "right")
COORDINATE_SCALE = 1000.0
MAX_WAIT_SECONDS = 30.0

_ACTION_LABELS: Dict[str, str] = {
    "tap": "点击",
    "long_press": "长按",
    "swipe": "滑动",
    "type": "输入",
    "enter": "回车",
    "back": "返回",
    "home": "回桌面",
    "launch": "打开应用",
    "wait": "看一会儿",
    "wait_turn": "等对方",
    "like": "点赞",
    "comment": "评论",
    "follow": "关注",
    "finish": "结束",
}
_DIRECTION_LABELS: Dict[str, str] = {"up": "上滑", "down": "下滑", "left": "左滑", "right": "右滑"}
# 滑动距离占屏幕对应边长的比例
_SWIPE_RATIO = 0.4
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class ActionError(ValueError):
    """动作无法执行；消息会写进操作记录并反馈给模型。"""


@dataclass(frozen=True)
class PhoneAction:
    """一步手机操作。"""

    kind: str
    thought: str = ""
    summary: str = ""
    element: Optional[int] = None
    x: Optional[float] = None
    y: Optional[float] = None
    direction: str = ""
    text: str = ""
    app: str = ""
    seconds: float = 0.0

    def describe(self) -> str:
        label = _ACTION_LABELS[self.kind]
        if self.kind in TAP_KINDS:
            target = f"[{self.element}]" if self.element is not None else f"({self.x:g}, {self.y:g})"
            return f"{label} {target}"
        if self.kind == "swipe":
            return _DIRECTION_LABELS[self.direction]
        if self.kind == "type":
            return f"输入「{self.text}」"
        if self.kind == "comment":
            return f"评论「{self.text}」"
        if self.kind == "launch":
            return f"打开 {self.app}"
        if self.kind == "wait":
            return f"看 {self.seconds:g} 秒"
        return label


def extract_json_object(response: str) -> Dict[str, Any]:
    """从模型回复中取出 JSON 对象，兼容 ```json 代码块和前后多余文字。

    Raises:
        ValueError: 找不到或无法解析 JSON 对象时抛出。
    """
    text = response.strip()
    fenced = _JSON_FENCE.search(text)
    if fenced is not None:
        candidate = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("回复里没有 JSON 对象")
        candidate = text[start : end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 无法解析：{exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError("回复必须是 JSON 对象")
    return data


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _optional_number(data: Dict[str, Any], key: str) -> Optional[float]:
    value = data.get(key)
    if value is None:
        return None
    if not _is_number(value):
        raise ActionError(f"{key} 必须是数字")
    return float(value)


def _optional_point(data: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    value = data.get("point")
    if value is None:
        return None, None
    if not isinstance(value, list) or len(value) != 2 or not all(_is_number(item) for item in value):
        raise ActionError("point 必须是 [x, y] 两个数字")
    x, y = float(value[0]), float(value[1])
    if not (0 <= x <= COORDINATE_SCALE and 0 <= y <= COORDINATE_SCALE):
        raise ActionError("point 的 x、y 必须在 0~1000 之间")
    return x, y


def parse_action(response: str) -> PhoneAction:
    """解析并校验视觉模型给出的动作。

    Raises:
        ActionError: 动作格式不正确或缺少必要参数时抛出。
    """
    try:
        data = extract_json_object(response)
    except ValueError as exc:
        raise ActionError(f"没有得到有效的动作：{exc}") from exc

    kind = str(data.get("action", "")).strip().lower()
    if kind not in ACTION_KINDS:
        raise ActionError(f"不认识的动作：{kind or '空'}")

    element = data.get("element")
    if element is not None and (isinstance(element, bool) or not isinstance(element, int)):
        raise ActionError("element 必须是整数编号")
    x, y = _optional_point(data)
    if kind in TAP_KINDS and element is None and x is None:
        raise ActionError(f"{kind} 需要 element 编号或 point 坐标")
    if kind == "comment" and element is None and x is None:
        # 实测她不点输入框就写，字没写进去；插件先替她点开输入框再写
        raise ActionError("comment 需要评论输入框的 element 编号或 point 坐标")

    direction = str(data.get("direction", "")).strip().lower()
    if kind == "swipe" and direction not in SWIPE_DIRECTIONS:
        raise ActionError("swipe 的 direction 必须是 up、down、left、right 之一")
    text = str(data.get("text", ""))
    if kind in ("type", "comment") and not text.strip():
        raise ActionError(f"{kind} 需要 text")
    app = str(data.get("app", "")).strip()
    if kind == "launch" and not app:
        raise ActionError("launch 需要 app")
    seconds = _optional_number(data, "seconds") or 0.0
    if kind == "wait":
        seconds = min(max(seconds, 1.0), MAX_WAIT_SECONDS)

    return PhoneAction(
        kind=kind,
        thought=str(data.get("thought", "")).strip()[:200],
        summary=str(data.get("summary", "")).strip()[:60],
        element=element,
        x=x,
        y=y,
        direction=direction,
        text=text,
        app=app,
        seconds=seconds,
    )


def to_pixels(x: float, y: float, width: int, height: int) -> Tuple[int, int]:
    """把 0~1000 的相对坐标换算为截图像素。"""
    return (
        min(width - 1, round(x / COORDINATE_SCALE * width)),
        min(height - 1, round(y / COORDINATE_SCALE * height)),
    )


def resolve_point(action: PhoneAction, elements: Sequence[UIElement], width: int, height: int) -> Tuple[int, int]:
    """求出点击或长按的像素位置。

    Raises:
        ActionError: 元素编号不存在时抛出。
    """
    if action.element is not None:
        for element in elements:
            if element.index == action.element:
                return element.center
        raise ActionError(f"界面元素 [{action.element}] 不存在")
    if action.x is None or action.y is None:
        raise ActionError("缺少点击坐标")
    return to_pixels(action.x, action.y, width, height)


def swipe_points(action: PhoneAction, width: int, height: int) -> Tuple[int, int, int, int]:
    """求出滑动的起点和终点像素；未给起点时从屏幕中央开始。"""
    if action.x is not None and action.y is not None:
        start_x, start_y = to_pixels(action.x, action.y, width, height)
    else:
        start_x, start_y = width // 2, height // 2
    delta_x, delta_y = {
        "up": (0, -_SWIPE_RATIO * height),
        "down": (0, _SWIPE_RATIO * height),
        "left": (-_SWIPE_RATIO * width, 0),
        "right": (_SWIPE_RATIO * width, 0),
    }[action.direction]
    end_x = min(width - 2, max(1, round(start_x + delta_x)))
    end_y = min(height - 2, max(1, round(start_y + delta_y)))
    return start_x, start_y, end_x, end_y


def find_app(apps: Sequence[AllowedApp], value: str) -> Optional[AllowedApp]:
    """按包名或显示名称在允许的应用里查找。"""
    normalized = value.strip().lower()
    for app in apps:
        if normalized in (app.package.lower(), app.name.strip().lower()):
            return app
    return None
