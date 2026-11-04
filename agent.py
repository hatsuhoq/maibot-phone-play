"""玩手机的核心循环：决定玩什么 → 看屏幕 → 做决定 → 执行动作。"""

from collections import Counter, deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from logging import Logger
from PIL import Image
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Sequence, Set, Tuple, Union

import asyncio
import math
import random
import re
import time
import weakref

from .actions import (
    PLAIN_WAIT_SECONDS,
    VIDEO_KINDS,
    ActionError,
    PhoneAction,
    Seen,
    find_app,
    parse_action,
    resolve_point,
    swipe_points,
)
from .devices import AppNotInstalledError, DeviceError, KeyName, PhoneDevice
from .diagnostics import DiagnosticStore, Mark
from .memory import MemoryItem, MemoryStore, normalize_title
from .pages import BILI_DETAIL, BILI_PLAYER, BILIBILI_PACKAGES, PAGE_LABELS, detect_page, marked_watched, page_title
from .imaging import (
    draw_element_marks,
    encode_jpeg,
    jpeg_data_url,
    open_image,
    region_distance,
    screen_signature,
    signature_distance,
)
from .prompts import (
    DecisionContext,
    InteractionQuota,
    Persona,
    Plan,
    PlanDeclined,
    app_tips,
    build_decision_messages,
    build_plan_prompt,
    parse_plan,
)
from .settings import AllowedApp, DeviceSettings, PhonePlayConfig
from .store import SessionRecord, UsageStore
from .ui_tree import UIElement, element_at, find_same

STATE_LABELS: Dict[str, str] = {"idle": "空闲", "starting": "准备中", "playing": "正在玩", "paused": "已暂停"}
CONTROLLER_LABELS: Dict[str, str] = {"bot": "她在操作", "user": "你在操作"}
CONNECTION_LABELS: Dict[str, str] = {
    "unknown": "还没连过",
    "ok": "连接正常",
    "reconnecting": "重连中",
    "needs_unlock": "需要解锁",
    "needs_action": "需要在手机上操作",
    "config": "配置有误",
    "failed": "连接失败",
}
TRIGGER_LABELS: Dict[str, str] = {"auto": "自己想玩", "chat": "聊天中决定", "command": "命令", "webui": "WebUI"}

_LOG_LIMIT = 60
# WebUI 查询结果上限 512 KiB，Base64 会膨胀约 4/3，再给其它字段留出余量
_WEBUI_FRAME_MAX_BYTES = 340 * 1024
_MODEL_JPEG_QUALITY = 80
_CHAT_JPEG_QUALITY = 85
_SWIPE_DURATION_MS = 350
_LONG_PRESS_MS = 800
_KEY_ACTIONS: Dict[str, KeyName] = {"enter": "enter", "back": "back", "home": "home"}
_INTERACTION_LABELS: Dict[str, str] = {"like": "点赞", "comment": "评论", "follow": "关注"}
_INPUT_FOCUS_SECONDS = 1.0
_REPEAT_KINDS = frozenset({"tap", "long_press", "like", "follow", "comment", "type", "enter"})
_REPEAT_DISTANCE = 25
# 同一个操作连续做到这么多次，下一步提醒她换个做法
_REPEAT_WARNING = 3
# 卡住检测（只看操作和结果，不只看截图像不像）：
# 连续这么多次点同一个地方、画面一直没变；最近这么多步在两个画面之间来回；最近 8 步里同一种失败出现这么多次
_STUCK_SAME_TAPS = 5
_STUCK_PING_PONG = 6
_STUCK_SAME_FAILURE = 4
_STUCK_TRACE = 8
# 判断「又回到同一个画面」「换了一个画面」的缩略图差值
_SAME_SCREEN_DISTANCE = 3.0
_OTHER_SCREEN_DISTANCE = 10.0
# 在页面之间跳转的操作；看视频的等待、刷短视频的滑动、输入文字都不算
_NAVIGATION_KINDS = frozenset({"tap", "long_press", "back", "launch", "enter"})
# 只看单纯的点击、长按：评论每次写的不一样，点赞、关注点两次会取消，都不算「点同一个地方」
_TAP_KINDS = frozenset({"tap", "long_press"})
_PIXEL_POINT = re.compile(r"\s*\(\d+, \d+\)")
# 结束时写的总结短于这么多字，就用她看的时候的想法补上
_SHORT_SUMMARY = 12
# 玩到一半手机断开时，每隔几秒试一次，最多等多久
_RECONNECT_INTERVAL = 5.0
_RECONNECT_SECONDS = 45.0
# 开始玩时手机暂时连不上（WDA 刚重启、Wi-Fi 抖了一下）最多等这么久；要比聊天里等计划的 30 秒短
_START_CONNECT_SECONDS = 20.0
# 暂停、人工接管不算进这次的游玩时长，但从开始到结束最多比计划多这么久，免得忘了交还一直挂着
_PAUSE_ALLOWANCE_MINUTES = 30
_PIXEL_SWIPE = re.compile(r"已从 \(\d+, \d+\) 滑到 \(\d+, \d+\)")
# 评论以她的账号公开发出：链接、@、长串数字（电话、QQ 号）一律不让发，不靠模型自觉
_COMMENT_FORBIDDEN = re.compile(r"https?://|www\.|\.(com|cn|net)\b|@|\d{6,}", re.IGNORECASE)
# 缩略图平均差值低于这个数就算画面没变（转圈加载、时钟跳动都在这之下）
_UNCHANGED_DISTANCE = 1.5
# 等对方操作时，缩略图差值超过这个数就算对方动过了
_PARTNER_CHANGE_DISTANCE = 0.5
# 点之前要点的那块和做决定时的截图比，差值低于这个数就直接点；高了才重新读界面确认（多花一两秒）
_TARGET_CHANGE_DISTANCE = 4.0
# 按坐标点、那里又没有界面元素时，比对点周围多大一块（屏幕宽度的比例，半边长）
_POINT_BOX_RATIO = 0.04
_TURN_POLL_SECONDS = 1.5
# 玩完后这么久之内，发起的那个聊天每一轮还提醒她现在没在玩手机
_RECENT_CHAT_SECONDS = 30 * 60
# 对方在聊天里提了新要求时，这次至少还留这么多分钟给她去做
_REQUEST_MINUTES = 3
# 之前交给她的要求，每一步最多再列出最近这几条
_REQUEST_LOG_SHOWN = 3
# 在当前应用里操作的动作：执行前再确认一次前台还在白名单里（launch、home 自己会换应用，wait 什么都不做）
_IN_APP_KINDS = frozenset({"tap", "long_press", "swipe", "type", "enter", "back", "like", "follow", "comment"})
# 上次玩的应用在这么久之内又打开，就接着上次的画面，不先关掉
_RESUME_SECONDS = 15 * 60
# 长等待期间每隔几秒截图比对一次；和等待开始时的缩略图差值超过这个数就算画面大变，提前醒来。
# 实测欢乐斗地主好友房里人物待机动画最多约 1.1，有人进房间约 2.7，开局（房间换成牌桌）约 19，对局里有人出牌 2~10
_WAIT_POLL_SECONDS = 3.0
# 看视频时隔这么久看一眼前台：点到广告跳去了别的应用就提前醒来（画面一直在变，不按画面比对）
_VIDEO_POLL_SECONDS = 5.0
# 每一步告诉她这个应用里最近看过的视频，最多这么多个
_WATCHED_SHOWN = 30
# 列给她看时每个标题只留前面这么多字，认得出就够了，三十个长标题每步都带上太占 token
_WATCHED_TITLE_SHOWN = 30
# 盖住大半个屏幕的元素（比如 B 站播放器整屏的那一格）不是视频卡片，它的文字不拿来比对看没看过
_CARD_MAX_SCREEN_RATIO = 0.5
_WAKE_CHANGE_DISTANCE = 2.0


@dataclass(frozen=True)
class ModelReply:
    """模型的回答和宿主给出的 token 用量；宿主没给（或给的是 0）时用量为 None，不拿估算冒充。"""

    text: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


VisionCall = Callable[[List[Dict[str, Any]]], Awaitable[Union[str, ModelReply]]]
TextCall = Callable[[str], Awaitable[Union[str, ModelReply]]]
PersonaProvider = Callable[[], Awaitable[Persona]]
FinishNotifier = Callable[[SessionRecord, str], Awaitable[None]]
DeviceFactory = Callable[[DeviceSettings], PhoneDevice]


class PlayRefused(Exception):
    """现在不能开始玩手机。

    ``str(exc)`` 是给管理员看的完整原因（命令、WebUI）；``chat_text`` 是聊天中告诉 Bot 的说法，
    不带 adb 地址这类技术细节，免得她照着技术报错编出不存在的情况。
    """

    def __init__(self, message: str, chat_text: str = "") -> None:
        super().__init__(message)
        self.chat_text = chat_text or message


class SwitchRefused(Exception):
    """现在不能切换设备（正在玩、没有这台设备），``str(exc)`` 就是原因。"""


# 聊天里常说「安卓手机」「苹果」而不是设备名：只有一台这种设备时就认它
_PLATFORM_WORDS: Dict[str, Tuple[str, ...]] = {
    "ios_wda": ("iphone", "苹果", "ios"),
    "adb": ("安卓", "android"),
}


def match_device(query: str, choices: Sequence[Tuple[str, DeviceSettings]]) -> Optional[str]:
    """按名字认出要切换的设备，认不出或者有歧义时返回 None。

    先比整个名字（不分大小写），再看名字和说法互相包含（只有一台对得上才算），最后看「安卓」「iPhone」这类说法。
    """
    text = query.strip().casefold()
    if not text:
        return None
    for name, _ in choices:
        if name.casefold() == text:
            return name
    partial = [name for name, _ in choices if name.casefold() in text or text in name.casefold()]
    if len(partial) == 1:
        return partial[0]
    for backend, words in _PLATFORM_WORDS.items():
        if any(word in text for word in words):
            same = [name for name, settings in choices if settings.backend == backend]
            if len(same) == 1:
                return same[0]
    return None


class AgentError(RuntimeError):
    """本次游玩无法继续。"""


class StaleTarget(Exception):
    """点之前画面变了，要点的东西已经不在原处：这一步不点，让她看了新画面再决定。"""


class ControlTaken(Exception):
    """做决定期间对方接管了手机：已经想好的动作作废，交还后按新画面重新决定。"""


class SessionEnd(Exception):
    """按规则有序结束这次（调用预算用完、卡住了），不是出错。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class SessionStats:
    """一次玩手机的模型调用和各环节耗时。调用次数每次尝试算一次（失败的也算），token 只累计宿主给出的。"""

    planner_calls: int = 0
    vision_calls: int = 0
    failed_calls: int = 0
    calls_with_usage: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    seconds: Dict[str, float] = field(
        default_factory=lambda: {"screenshot": 0.0, "ui": 0.0, "model": 0.0, "action": 0.0}
    )

    @property
    def calls(self) -> int:
        return self.planner_calls + self.vision_calls

    def to_dict(self) -> Dict[str, Any]:
        missing = self.calls - self.calls_with_usage
        return {
            "calls": {"planner": self.planner_calls, "vision": self.vision_calls, "failed": self.failed_calls},
            # 一次都没给就是 None，WebUI 显示「未提供」；部分给了就同时说明有几次没给
            "tokens": {
                "total": self.total_tokens if self.calls_with_usage else None,
                "prompt": self.prompt_tokens if self.calls_with_usage else None,
                "completion": self.completion_tokens if self.calls_with_usage else None,
                "missing": missing,
            },
            "seconds": {name: round(value, 1) for name, value in self.seconds.items()},
        }


@dataclass
class ConnectionStatus:
    """给 WebUI 看的手机连接状态。"""

    # unknown 还没连过、ok 正常、reconnecting 重连中、needs_unlock 要手动解锁、needs_action 要在手机上点允许、
    # config 配置有误、failed 等到截止时间也没连上
    state: str = "unknown"
    last_ok_at: Optional[datetime] = None
    attempt: int = 0
    # 这一轮重连的截止时刻（_clock 的值），0 表示没在重连
    deadline: float = 0.0
    error: str = ""


@dataclass(frozen=True)
class StepLog:
    """WebUI 和状态里展示的一条操作记录。"""

    time: str
    step: int
    summary: str
    thought: str
    action: str
    ok: bool
    result: str
    # 失败步骤的诊断记录编号，WebUI 里点开能看当时的画面
    diagnostic: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Frame:
    """WebUI 实时画面的一帧。"""

    frame_id: int
    jpeg: bytes
    width: int
    height: int
    captured_at: datetime


@dataclass
class Visit:
    """这次去过的一个应用和大致停留的秒数（从切进去到切走，按墙钟算）。"""

    app: AllowedApp
    seconds: float = 0.0
    # 正在这个应用里时是切进来的时刻，切走后为 None
    since: Optional[float] = None


@dataclass
class Session:
    """一次玩手机的运行状态。"""

    trigger: str
    hint: str
    origin_stream: str
    minutes_override: int
    started_at: datetime
    started_clock: float
    # 白名单里且手机上已安装的应用
    apps: List[AllowedApp] = field(default_factory=list)
    # 和对方在这台手机上一起玩（比如同一台手机上两个人轮流玩的游戏）
    together: bool = False
    goal: str = ""
    app: Optional[AllowedApp] = None
    max_minutes: int = 0
    step: int = 0
    paused_seconds: float = 0.0
    paused_since: Optional[float] = None
    last_thought: str = ""
    summaries: List[str] = field(default_factory=list)
    # 每步的想法里记着她看到了什么，比操作摘要更适合拿来聊天和总结
    thoughts: List[str] = field(default_factory=list)
    # 停下来看内容（wait）时的想法最能代表看到了什么；最后几步可能只是在返回、关弹窗
    watch_thoughts: List[str] = field(default_factory=list)
    finish_summary: str = ""
    finish_thought: str = ""
    # 用她自己的账号做的互动：点赞只计数，评论和关注记下内容
    likes: int = 0
    interactions: List[str] = field(default_factory=list)
    end_reason: str = ""
    # 想做的事手机上做不到时，告诉她的说法
    decline_text: str = ""
    last_signature: bytes = b""
    unchanged_steps: int = 0
    # 连续做同一个操作（点同一个位置、同一个元素）的次数：视频一直在播时画面总在变，只比截图发现不了她在原地打转
    last_action: Optional[PhoneAction] = None
    repeat_count: int = 0
    # 在聊天里被叫停时她已经在接话了，不需要再发结束通知
    notify: bool = True
    # 对方在聊天里让她在手机上做的事（点准备、退出房间……），下一步先照着做
    requests: List[str] = field(default_factory=list)
    # 这次已经交给她的要求，之后每一步都还看得到，免得操作记录一长就忘了
    request_log: List[str] = field(default_factory=list)
    # 这次操作的设备和它的配置：玩到一半改了设备配置，截图、回桌面也还发给这台，下一步前结束这次
    device: Optional[PhoneDevice] = None
    device_key: str = ""
    # 这次用的设备名字和设置（多设备时切换不会影响正在进行的这次）
    device_name: str = ""
    device_settings: Optional[DeviceSettings] = None
    # 现在实际在用的应用：她可能中途切到别的应用，结束回不回桌面、下次接着玩、操作节奏都按它算
    current: Optional[AllowedApp] = None
    # 这次去过的应用，按第一次去的先后；app 是起始应用，current 是现在的应用
    visits: List[Visit] = field(default_factory=list)
    # 已经在收尾（回桌面、保存记录）：这时再叫停只等它做完，不再取消
    finishing: bool = False
    # 每次人工接管加一：发出模型调用时记下，返回时对不上就说明中途被接管过，那个决定作废
    control_epoch: int = 0
    # 刚交还控制：下一步先把之前的画面比对、重复操作记录清掉，按新画面重新决定
    handed_back: bool = False
    # 最近一次失败的原因，连续失败结束时告诉用户卡在哪
    last_failure: str = ""
    # 已经提醒过这台手机给不出前台应用
    foreground_noted: bool = False
    stats: SessionStats = field(default_factory=SessionStats)
    # 最近几步的（这一步开始时的画面，做了什么操作，失败的话是什么失败），用来发现卡住了
    trace: Deque[Tuple[bytes, str, str]] = field(default_factory=lambda: deque(maxlen=_STUCK_TRACE))
    # 这一步的等待是不是在看视频（short、long），_play 按它决定怎么等
    wait_video: str = ""
    # 这次连上过手机；没连上就结束的不用锁屏
    connected: bool = False
    # 这次替她解开过锁屏：还没打开应用就结束了也要锁回去
    unlocked: bool = False
    # 认出来的页面（见 pages.detect_page），比如 B 站竖屏播放器、视频详情页
    page: str = ""
    # 最近认出来的视频（内容记忆里那一条的编号和标题）：点开评论再回来接着看它不算重看
    video_item: str = ""
    video_title: str = ""
    # 连着看的这一段：上一步也是看视频才算接着看；中间点了、滑了、返回了就是新的一段。
    # 实测换到下一个视频后她没写标题，时长全记到了上一个视频头上，所以没认出标题时 watch_item 是空的
    watching: bool = False
    watch_item: str = ""
    watch_title: str = ""
    watch_form: str = ""
    watch_seconds: float = 0.0
    # 这一步做的操作和失败，_play 收进 trace 后清空
    step_kind: str = ""
    step_failure: str = ""
    # 决定好玩什么（或会话提前结束）时置位，供聊天工具等待计划
    planned: asyncio.Event = field(default_factory=asyncio.Event)


def _parse_minutes(text: str) -> int:
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def in_active_hours(spec: str, now: datetime) -> bool:
    """判断当前时间是否在 ``HH:MM-HH:MM`` 时间段内，支持跨午夜；起止相同表示全天。"""
    start_text, end_text = spec.split("-")
    start, end = _parse_minutes(start_text), _parse_minutes(end_text)
    current = now.hour * 60 + now.minute
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _recent_unique(items: Sequence[str], count: int) -> List[str]:
    return list(dict.fromkeys(items))[-count:]


def _history_result(result: str) -> str:
    """给模型看的操作结果：去掉截图像素坐标（WebUI 的操作记录里照样保留）。

    实测模型会把上一步「已点击 (585, 2158)」里的像素坐标照抄成 point，超出 0~1000，这一步就白费了。
    """
    return _PIXEL_POINT.sub("", _PIXEL_SWIPE.sub("已滑动", result))


def _same_action(previous: Optional[PhoneAction], action: PhoneAction) -> bool:
    """两步是不是同一个操作；滑动、等待这类本来就会连着做的操作不算。"""
    if previous is None or action.kind not in _REPEAT_KINDS or previous.kind != action.kind:
        return False
    if action.element is not None or previous.element is not None:
        return action.element == previous.element
    if None not in (action.x, action.y, previous.x, previous.y):
        # 相对坐标差几十以内算同一个位置
        return abs(action.x - previous.x) <= _REPEAT_DISTANCE and abs(action.y - previous.y) <= _REPEAT_DISTANCE
    return action.text == previous.text


def _foreground_unknown_text(support: str) -> str:
    if support == "supported":
        return "看不出现在在哪个应用（这台手机之前能认出来，这次暂时没认出，等认出来再接着操作）"
    return "看不出现在在哪个应用（还没确认这台手机能不能识别前台，认出来之前不操作）"


def _connection_hint(state: str, device: DeviceSettings) -> str:
    """连不上时告诉用户该去查什么；无线、有线的 iPhone 都要照顾到，不再一律让人查数据线。"""
    if state == "needs_unlock":
        return (
            "请手动解锁手机。设了锁屏密码的手机插件解不开：要让她自己解开，就去掉锁屏密码；"
            "不想去掉就关掉「玩完锁屏」（play.lock_after），iPhone 的自动锁定也改成「永不」。"
        )
    if state == "needs_action":
        return "请按上面的提示在手机上操作（比如允许 USB 调试），弄好后再开始。"
    if state == "config":
        return "请检查插件配置里的设备设置。"
    if state not in ("reconnecting", "failed"):
        return ""
    if device.backend == "ios_wda":
        return (
            "无线连接：确认 iPhone 和电脑连着同一个 Wi-Fi、启动 WDA 的 pymobiledevice3 和端口转发还在运行；"
            "有线连接：确认数据线连好、ios runwda 和 ios forward 还在运行（用脚本启动的就看脚本窗口）。"
            f"浏览器打开 {device.wda_url}/status 能看到 ready 就是通的。插件不会替你重启它们。"
        )
    return "确认模拟器已启动或手机已连好：adb devices 里能看到它；无线调试时确认还在同一个网络、端口没变。"


def _watching_text(session: Session) -> str:
    """正在看的视频和看了多久；没在看视频（或还没看）时是空字符串。"""
    if not session.watching or session.watch_seconds < 1:
        return ""
    seconds = round(session.watch_seconds)
    spent = f"已经看了 {seconds} 秒" if seconds < 60 else f"已经看了约 {round(seconds / 60)} 分钟"
    form = VIDEO_KINDS.get(session.watch_form, "视频")
    if not session.watch_title:
        return f"这个{form}（没写标题），{spent}"
    return f"《{session.watch_title}》（{form}），{spent}"


def _page_note(page: str, action: PhoneAction) -> str:
    """按页面说明这一步滑动的结果：详情页里滑是在翻简介和评论，竖屏播放器里滑是换视频。"""
    if action.kind != "swipe" or action.direction not in ("up", "down"):
        return ""
    if page == BILI_DETAIL:
        return "（在详情页上下滑只是在翻简介、评论和相关推荐，视频没换，还在上面接着播）"
    if page == BILI_PLAYER:
        return "（换到了下一个推荐视频）" if action.direction == "up" else "（回到了上一个视频）"
    return ""


def _is_detail_tab(action: PhoneAction, elements: Sequence[UIElement]) -> bool:
    """点的是详情页的「简介」「评论」标签。"""
    if action.kind != "tap" or action.element is None:
        return False
    element = next((item for item in elements if item.index == action.element), None)
    return element is not None and (element.label == "简介" or element.label.startswith("评论"))


def _card(action: PhoneAction, elements: Sequence[UIElement], width: int, height: int) -> Optional[UIElement]:
    """要点的那张卡片：点的是没有文字的按钮时看盖住它的卡片；只看能点的元素，整屏那么大的不算卡片。"""
    try:
        x, y = resolve_point(action, elements, width, height)
    except ActionError:
        return None
    screen = width * height
    cards = [
        element
        for element in elements
        if element.label
        and element.clickable
        and element.bounds[0] <= x < element.bounds[2]
        and element.bounds[1] <= y < element.bounds[3]
        and (element.bounds[2] - element.bounds[0]) * (element.bounds[3] - element.bounds[1])
        < screen * _CARD_MAX_SCREEN_RATIO
    ]
    card = min(
        cards,
        key=lambda element: (element.bounds[2] - element.bounds[0]) * (element.bounds[3] - element.bounds[1]),
        default=None,
    )
    return card


def _ago(then: datetime, now: datetime) -> str:
    """「刚才」「今天」「3 天前」。"""
    days = (now.date() - then.date()).days
    if days <= 0:
        return "刚才" if (now - then).total_seconds() < 3600 else "今天"
    return "昨天" if days == 1 else f"{days} 天前"


def _app_name(session: Optional[Session]) -> str:
    """她现在实际在用的应用；中途切到了别的应用，WebUI 和聊天里也跟着换，不然又是在说没在发生的事。"""
    if session is None:
        return ""
    app = session.current or session.app
    return app.name if app else ""


def _interaction_list(session: Session) -> List[str]:
    """这次用她自己的账号做的互动，例如 ["点赞 3 次", "评论「好圆」"]。"""
    likes = [f"点赞 {session.likes} 次"] if session.likes else []
    return likes + session.interactions


class PhoneAgent:
    """管理一次次玩手机的会话，同一时间只运行一个会话。"""

    def __init__(
        self,
        *,
        config: Callable[[], PhonePlayConfig],
        device_factory: DeviceFactory,
        vision: VisionCall,
        planner: TextCall,
        persona: PersonaProvider,
        store: UsageStore,
        notifier: FinishNotifier,
        logger: Logger,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        diagnostics: Optional[DiagnosticStore] = None,
        memory: Optional[MemoryStore] = None,
    ) -> None:
        self._config = config
        self._diagnostics = diagnostics
        self._memory = memory
        self._device_factory = device_factory
        self._vision = vision
        self._planner = planner
        self._persona = persona
        self._store = store
        self._notifier = notifier
        self._logger = logger
        self._clock = clock
        self._now = now
        self._sleep = sleep
        self._rng = rng

        self._state = "idle"
        self._session: Optional[Session] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._auto_task: Optional[asyncio.Task[None]] = None
        self._next_auto_check: Optional[datetime] = None
        self._start_lock = asyncio.Lock()
        self._resume = asyncio.Event()
        self._resume.set()
        self._stop_reason = ""
        self._last_error = ""
        self._logs: Deque[StepLog] = deque(maxlen=_LOG_LIMIT)
        # 对方在聊天里提了新要求时叫醒等待中的会话
        self._wake = asyncio.Event()
        # 上一次玩手机是从哪个聊天发起的、玩的哪个应用、什么时候结束的
        self._last_stream = ""
        self._last_package = ""
        self._last_ended_clock = 0.0
        # 谁在操作手机：bot 她自己，user 人工接管中（她不发任何操作，自动玩也不会抢回来）
        self._controller = "bot"
        self._connection = ConnectionStatus()
        # 每个设备连接识别前台的能力：supported 认出来过，unsupported 设备明确给不出，没有记录就是还不确定。
        # 跟着设备对象走：改了设备配置、换了连接，就重新判断
        self._foreground_support: "weakref.WeakKeyDictionary[PhoneDevice, str]" = weakref.WeakKeyDictionary()
        # 自动解锁没成功过的设备（多半设了锁屏密码）：玩完不再锁屏，免得下次又解不开；再自动解开一次就去掉
        self._cannot_unlock: "weakref.WeakSet[PhoneDevice]" = weakref.WeakSet()
        # 按了关屏屏幕还亮着的设备：说一次，之后玩完不再试
        self._cannot_lock: "weakref.WeakSet[PhoneDevice]" = weakref.WeakSet()

        # 每台设备（按名字）的连接和它的配置：切回来时还用原来那个，记住的前台能力、锁屏情况都还在
        self._devices: Dict[str, Tuple[str, PhoneDevice]] = {}
        # 现在用哪台设备（名字）；配置里没有这台时用「设备」那一台
        self._current_device = store.current_device
        # 换下来的旧设备在后台关闭连接，留着引用免得任务被回收
        self._closing: Set[asyncio.Task] = set()
        self._frame: Optional[Frame] = None
        self._frame_clock = 0.0
        self._frame_seq = 0
        self._frame_error = ""
        self._frame_lock = asyncio.Lock()

    # ===== 状态 =====

    @property
    def state(self) -> str:
        return self._state

    def _today(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def _elapsed_seconds(self, session: Session) -> float:
        paused = session.paused_seconds
        if session.paused_since is not None:
            paused += self._clock() - session.paused_since
        return max(0.0, self._clock() - session.started_clock - paused)

    def _check_limits(self, config: PhonePlayConfig) -> None:
        """每日额度用完时抛出 PlayRefused。"""
        sessions, steps = self._store.usage(self._today())
        if sessions >= config.play.daily_session_limit:
            raise PlayRefused(
                f"今天已经玩了 {sessions} 次手机，达到每日上限",
                chat_text=f"今天已经玩了 {sessions} 次手机，到每天的上限了，今天不能再玩",
            )
        if steps >= config.play.daily_step_limit:
            raise PlayRefused(
                f"今天的操作步数（{config.play.daily_step_limit} 步）已经用完",
                chat_text="今天玩手机的时间用完了，今天不能再玩",
            )
        if self._store.model_calls(self._today()) >= config.play.daily_model_call_limit:
            raise PlayRefused(
                f"今天的模型调用预算（{config.play.daily_model_call_limit} 次）已经用完",
                chat_text="今天玩手机的时间用完了，今天不能再玩",
            )

    def _log(
        self,
        step: int,
        summary: str,
        *,
        thought: str = "",
        action: str = "",
        ok: bool = True,
        result: str = "",
        diagnostic: str = "",
    ) -> None:
        self._logs.append(
            StepLog(
                time=self._now().strftime("%H:%M:%S"),
                step=step,
                summary=summary,
                thought=thought,
                action=action,
                ok=ok,
                result=result,
                diagnostic=diagnostic,
            )
        )

    async def _record_failure(
        self,
        session: Session,
        step: int,
        image: Image.Image,
        action: Optional[PhoneAction],
        elements: Sequence[UIElement],
        result: str,
    ) -> str:
        """失败步骤存一份诊断：当时的画面（标出要点的位置或滑动方向）、计划动作和目标、结果；没开诊断时返回空串。"""
        if self._diagnostics is None:
            return ""
        mark: Optional[Mark] = None
        target = ""
        if action is not None and action.kind in ("tap", "long_press", "like", "follow", "comment"):
            try:
                x, y = resolve_point(action, elements, *image.size)
            except ActionError as exc:
                target = f"目标无效：{exc}"
            else:
                mark = (x, y)
                element = next((item for item in elements if item.index == action.element), None)
                target = f"{element.describe()} ({x}, {y})" if element else f"({x}, {y})"
        elif action is not None and action.kind == "swipe":
            mark = swipe_points(action, *image.size)
            target = f"从 ({mark[0]}, {mark[1]}) 滑到 ({mark[2]}, {mark[3]})"
        current = session.current or session.app
        info = {
            "step": step,
            "app": current.name if current else "",
            "goal": session.goal,
            "summary": action.summary if action is not None else "",
            "action": action.describe() if action is not None else "",
            "target": target,
            "result": result,
        }
        try:
            return await self._diagnostics.add(image, mark, info)
        except OSError as exc:
            self._logger.warning(f"保存诊断记录失败：{exc}")
            return ""

    def _current_choice(self, config: PhonePlayConfig) -> Tuple[str, DeviceSettings]:
        """现在用的设备（名字，设置）；记住的那台被从配置里删掉了就用「设备」那一台。"""
        choices = config.device_choices()
        wanted = self._current_device.casefold()
        return next((choice for choice in choices if choice[0].casefold() == wanted), choices[0])

    def _get_device(self) -> PhoneDevice:
        """返回现在用的那台设备；它的配置变了就重新创建。"""
        name, settings = self._current_choice(self._config())
        key = settings.model_dump_json()
        cached = self._devices.get(name)
        if cached is not None and cached[0] == key:
            return cached[1]
        if cached is not None:
            task = asyncio.create_task(cached[1].close())
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)
        device = self._device_factory(settings)
        self._devices[name] = (key, device)
        return device

    def device_list(self) -> List[Dict[str, str]]:
        """所有能切换的设备，给命令和 WebUI 用。"""
        config = self._config()
        current, _ = self._current_choice(config)
        return [
            {
                "name": name,
                "kind": "iPhone" if settings.backend == "ios_wda" else "安卓",
                "address": settings.wda_url if settings.backend == "ios_wda" else (settings.serial or "自动选择"),
                "current": name == current,
            }
            for name, settings in config.device_choices()
        ]

    async def switch_device(self, query: str) -> Tuple[str, bool]:
        """换一台设备，返回（换成的名字，是不是真的换了）；记下来，重启后还用这台。

        Raises:
            SwitchRefused: 正在玩手机，或者认不出是哪台。
        """
        config = self._config()
        choices = config.device_choices()
        name = match_device(query, choices)
        if name is None:
            names = "、".join(choice[0] for choice in choices)
            raise SwitchRefused(f"没有叫「{query.strip()}」的设备，能用的有：{names}")
        if self._state != "idle":
            current = self._session.device_name if self._session else self._current_choice(config)[0]
            raise SwitchRefused(f"正在用{current}玩手机，先停下再换")
        current, _ = self._current_choice(config)
        if name == current:
            return name, False
        self._current_device = name
        self._store.current_device = name
        # 连接状态、画面都是上一台的，换了就清掉
        self._connection = ConnectionStatus()
        self._frame, self._frame_error = None, ""
        await self._save_usage()
        return name, True

    def status(self) -> Dict[str, Any]:
        """WebUI 与命令共用的状态快照。"""
        config = self._config()
        session = self._session
        sessions_today, steps_today = self._store.usage(self._today())
        next_check = ""
        if config.auto_play.enabled and self._next_auto_check is not None:
            next_check = self._next_auto_check.strftime("%H:%M")
        return {
            "state": self._state,
            "state_label": STATE_LABELS[self._state],
            "trigger": TRIGGER_LABELS[session.trigger] if session else "",
            "app": _app_name(session),
            "start_app": session.app.name if session and session.app else "",
            "visited": [visit.app.name for visit in session.visits] if session else [],
            "page": PAGE_LABELS.get(session.page, "") if session else "",
            "goal": session.goal if session else "",
            "thought": session.last_thought if session else "",
            "step": session.step if session else 0,
            "max_steps": config.play.max_steps_per_session,
            "elapsed_minutes": round(self._elapsed_seconds(session) / 60, 1) if session else 0,
            "max_minutes": session.max_minutes if session and session.max_minutes else config.play.max_session_minutes,
            "today": {
                "sessions": sessions_today,
                "session_limit": config.play.daily_session_limit,
                "steps": steps_today,
                "step_limit": config.play.daily_step_limit,
                "interactions": self._store.interactions(self._today()),
            },
            "logs": [log.to_dict() for log in reversed(self._logs)],
            "history": [record.to_dict() for record in reversed(self._store.history[-8:])],
            "last_error": self._last_error,
            "device": self._device_label(config),
            "device_name": session.device_name if session else self._current_choice(config)[0],
            "devices": self.device_list(),
            "controller": self._controller,
            "controller_label": CONTROLLER_LABELS[self._controller],
            "connection": self._connection_payload(config),
            "session_stats": session.stats.to_dict() if session else None,
            "model_calls": {
                "today": self._store.model_calls(self._today()),
                "daily_limit": config.play.daily_model_call_limit,
                "session_limit": config.play.max_model_calls_per_session,
            },
            "auto_play": config.auto_play.enabled,
            "next_auto_check": next_check,
        }

    def _device_label(self, config: PhonePlayConfig) -> str:
        name, settings = self._current_choice(config)
        cached = self._devices.get(name)
        description = cached[1].description if cached is not None else ""
        # 描述本来就以「iPhone」开头时不再重复名字
        if len(config.device_choices()) < 2 or not description or description.startswith(name):
            return description
        return f"{name} · {description}"

    def activity_text(self) -> str:
        """给聊天中的 Bot 看的近况，方便她聊起手机上看到的东西。"""
        session = self._session
        if session is not None:
            if session.app is None:
                return "正在拿起手机，还在想玩什么。"
            recent = "；".join(_recent_unique(session.summaries, 5)) or "刚打开"
            prefix = "（暂停中）" if self._state == "paused" else ""
            others = [visit.app.name for visit in session.visits if visit.app.name != _app_name(session)]
            visited = f"（这次还去过{'、'.join(others)}）" if others else ""
            device = f"用{session.device_name}" if len(self._config().device_choices()) > 1 else "正在用手机"
            text = (
                f"{prefix}{device}：{_app_name(session)}{visited}，想{session.goal}。"
                f"已经玩了 {self._elapsed_seconds(session) / 60:.0f} 分钟，最近在：{recent}。"
            )
            thoughts = "；".join(_recent_unique(session.thoughts, 3))
            if thoughts:
                text += f"这几步的想法：{thoughts}"
            interactions = _interaction_list(session)
            if interactions:
                text += f"\n这次用自己的账号{'、'.join(interactions)}。"
            return text
        # 明说现在没在玩：实测对方问「玩上了吗」，她没去玩却截了张桌面，照着「上一次玩手机是……」编了一段正在玩
        history = self._store.history
        if not history:
            return "现在没在玩手机，最近也没有玩过。"
        last = history[-1]
        sessions_today, _ = self._store.usage(self._today())
        place = f"在{last.places_text()}里" if last.app else ""
        text = (
            f"现在没在玩手机。上一次玩手机是 {last.started_at[5:16].replace('T', ' ')}，"
            f"{place}{last.goal or '没决定好玩什么'}："
            f"{last.summary}（{last.end_reason}）。今天一共玩了 {sessions_today} 次。"
        )
        if last.interactions:
            text += f"那次用自己的账号{'、'.join(last.interactions)}。"
        return text

    def add_request(self, text: str, minutes: int = 0) -> bool:
        """把对方在聊天里的新要求交给手机上的她，下一步先照着做；没在玩时返回 False。

        ``minutes`` 是聊天这边说还要玩多久；没说时至少留几分钟给她去做，都不超过配置的单次上限。
        """
        session = self._session
        text = text.strip()
        if session is None or not text:
            return False
        session.requests.append(text)
        if session.max_minutes:
            # 实测「去打一把路人局」交过去 44 秒后，这次就因为玩够 5 分钟结束了，手机上的她没来得及做
            config = self._config()
            limit = self._minutes_limit(session, session.current or session.app, config)
            needed = math.ceil(self._elapsed_seconds(session) / 60 + max(minutes, _REQUEST_MINUTES))
            session.max_minutes = min(limit, max(session.max_minutes, needed))
        # 正在看视频、等对方时提前醒来，不用等满这一轮
        self._wake.set()
        return True

    def chat_context(self, stream_id: str) -> str:
        """给发起这次玩手机的聊天看的手机实际情况，每一轮都带上；没什么可说的返回空字符串。

        聊天这边看不到手机，实测常照着对方的话编进展：「已经在游戏里了」「点到了，退出房间了」，手机上其实没发生；
        对方说「不玩了」也不放下手机。正在玩时告诉她手机上在做什么、怎么把要求交给手机上的她；
        玩完后的一段时间里提醒她现在没在玩。别的聊天、很久以前玩的不提。
        """
        if not stream_id:
            return ""
        session = self._session
        if session is not None and session.origin_stream == stream_id:
            # 实测「最近在：……开始经典新手场」被她说成「这不就开局了嘛」，其实下一步就因为豆子不够弹了充值
            return (
                f"【手机上的实际情况】{self.activity_text()}\n"
                "手机上的操作是手机上的你自己在一步步做，聊天里看不到画面：说到手机上的事只按上面写的说，"
                "没发生的不要说成已经做了；「最近在」是这几步点了什么、想做什么，成没成要看后面几步，"
                "别说成已经开局、已经领到了。对方让你在手机上做什么（点准备、退出房间、接受邀请、换个视频），"
                "用 play_phone 把要求交给手机上的你，过一会儿用 phone_activity 看做没做成再说；"
                "对方叫你别玩了就用 stop_phone。"
            )
        if stream_id == self._last_stream and self._clock() - self._last_ended_clock < _RECENT_CHAT_SECONDS:
            return (
                f"【手机上的实际情况】{self.activity_text()}\n"
                "要在手机上做什么，得先用 play_phone 真的拿起手机；调用之前别说自己在玩、已经进去了。"
            )
        return ""

    # ===== 控制 =====

    async def start(
        self, *, trigger: str, hint: str = "", origin_stream: str = "", minutes: int = 0, together: bool = False
    ) -> None:
        """在后台开始一次玩手机。

        连接手机放在会话里：暂时连不上会在限定时间内重试，期间可以叫停，WebUI 能看到重连进度；
        连不上、没有能玩的应用时会话直接结束，原因由 ``wait_for_plan`` 抛出，不算作玩了一次。

        Args:
            trigger: 触发来源，见 ``TRIGGER_LABELS``。
            hint: 想做的事；留空由 Bot 自己决定。
            origin_stream: 结束后要通知的聊天流，留空不通知。
            minutes: 指定玩多久，0 表示由 Bot 决定。
            together: 是否和对方在这台手机上一起玩（对方直接在手机上操作，她轮到对方时等着）。

        Raises:
            PlayRefused: 已经在玩、正在人工接管、额度用完时抛出。
        """
        async with self._start_lock:
            if self._controller == "user":
                raise PlayRefused(
                    "你正在操作手机（人工接管中），先交还控制再让她玩", chat_text="对方正在用这台手机，现在不能玩"
                )
            if self._state != "idle":
                goal = self._session.goal if self._session else ""
                raise PlayRefused(
                    f"已经在玩手机了（{goal or '正在准备'}）", chat_text=f"你已经在玩手机了（{goal or '还在想玩什么'}）"
                )
            config = self._config()
            self._check_limits(config)
            device_name, device_settings = self._current_choice(config)
            device = self._get_device()
            session = Session(
                trigger=trigger,
                hint=hint.strip(),
                origin_stream=origin_stream,
                minutes_override=max(0, minutes),
                started_at=self._now(),
                started_clock=self._clock(),
                together=together,
                device=device,
                device_key=device_settings.model_dump_json(),
                device_name=device_name,
                device_settings=device_settings,
            )
            self._session = session
            self._state = "starting"
            self._stop_reason = ""
            self._last_error = ""
            self._resume.set()
            self._logs.clear()
            self._log(0, f"拿起手机（{TRIGGER_LABELS[trigger]}）", result=session.hint)
            self._task = asyncio.create_task(self._run(session), name="phone_play_session")

    @property
    def connecting(self) -> bool:
        """开始玩时手机暂时连不上、正在重试。"""
        return self._state == "starting" and self._connection.state == "reconnecting"

    async def wait_for_plan(self, timeout: float) -> Optional[Plan]:
        """等待刚开始的会话决定玩什么；超时还没决定好时返回 None。

        Raises:
            PlayRefused: 还没决定好玩什么会话就结束了（想做的事手机上做不到、模型出错或被叫停）。
        """
        session = self._session
        if session is None:
            return None
        try:
            await asyncio.wait_for(session.planned.wait(), timeout)
        except TimeoutError:
            return None
        if session.app is None:
            raise PlayRefused(
                session.end_reason, chat_text=session.decline_text or "你拿起手机后没想好玩什么，又放下了"
            )
        return Plan(app=session.app, goal=session.goal, minutes=session.max_minutes)

    async def stop(self, reason: str, *, notify: bool = True) -> bool:
        """立即停止当前会话（包括正在等待的模型调用），返回是否确实停止了会话。

        Args:
            reason: 结束原因。
            notify: 是否在发起的聊天里发结束通知。
        """
        task = self._task
        if task is None or task.done():
            return False
        session = self._session
        if session is not None:
            session.notify = notify
            if session.finishing:
                # 已经在收尾：再取消会打断回桌面、改状态，实测之后一直停在「正在玩」，再也开始不了
                await asyncio.wait({task})
                return True
        self._stop_reason = reason
        task.cancel()
        # 用 wait 而不是直接 await，避免把会话的取消异常抛给调用方
        await asyncio.wait({task})
        return True

    def pause(self) -> bool:
        """暂停：当前这一步做完后停下，直到继续。"""
        session = self._session
        if self._state != "playing" or session is None:
            return False
        self._state = "paused"
        self._sync_running()
        self._log(session.step, "暂停")
        return True

    def resume(self) -> bool:
        session = self._session
        if self._state != "paused" or session is None:
            return False
        self._state = "playing"
        self._sync_running()
        self._log(session.step, "继续")
        return True

    @property
    def controller(self) -> str:
        return self._controller

    def takeover(self) -> bool:
        """人工接管：她马上停手，不再发任何操作（已经发出的模型调用返回后也作废），自动玩不会抢回来。

        和「暂停」的区别：暂停是让她歇一会儿、这一步做完再停；接管是你要亲自用手机，正在想的这一步也不做了。
        空闲时也能接管，免得你用手机时她自己开始玩。
        """
        if self._controller == "user":
            return False
        self._controller = "user"
        session = self._session
        if session is not None:
            session.control_epoch += 1
            self._log(session.step, "你接管了手机，她先停手")
        self._sync_running()
        # 正在看视频、等对方的长等待也马上停下
        self._wake.set()
        return True

    def release(self) -> bool:
        """交还控制：她先重新看前台、截图和界面元素，按现在的画面重新决定，不做接管前想好的动作。"""
        if self._controller == "bot":
            return False
        self._controller = "bot"
        session = self._session
        if session is not None:
            session.handed_back = True
            self._log(session.step, "你交还了控制，她看看现在的画面再接着玩")
        self._sync_running()
        return True

    def _sync_running(self) -> None:
        """按暂停和接管决定会话能不能继续操作；不能操作的时间不算进这次的游玩时长（墙钟上限另算）。"""
        session = self._session
        running = self._controller == "bot" and self._state != "paused"
        if session is not None:
            if running and session.paused_since is not None:
                session.paused_seconds += self._clock() - session.paused_since
                session.paused_since = None
            elif not running and session.paused_since is None:
                session.paused_since = self._clock()
        if running:
            self._resume.set()
        else:
            self._resume.clear()

    async def _wait_running(self, session: Session) -> bool:
        """暂停或接管时在这里等；超过墙钟上限（计划时长再加 30 分钟）就不等了，返回 False 结束这次。"""
        while not self._resume.is_set():
            minutes = session.max_minutes or self._config().play.max_session_minutes
            remaining = session.started_clock + (minutes + _PAUSE_ALLOWANCE_MINUTES) * 60 - self._clock()
            if remaining <= 0:
                session.end_reason = "暂停或接管太久，这次先结束"
                return False
            try:
                await asyncio.wait_for(self._resume.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
        return True

    def restart_auto_play(self) -> None:
        """按最新配置重新启动自动玩调度。"""
        if self._auto_task is not None:
            self._auto_task.cancel()
        self._auto_task = asyncio.create_task(self._auto_loop(), name="phone_play_auto")

    async def shutdown(self) -> None:
        if self._auto_task is not None:
            self._auto_task.cancel()
            await asyncio.wait({self._auto_task})
            self._auto_task = None
        # 插件重载、MaiBot 关闭这类技术原因不该让她在聊天里说什么，实测她会编出「黑屏了」之类的理由
        await self.stop("插件卸载", notify=False)
        for _, device in self._devices.values():
            await device.close()
        self._devices.clear()

    # ===== 画面 =====

    async def _publish_frame(self, image: Image.Image, config: PhonePlayConfig) -> None:
        jpeg = await asyncio.to_thread(
            encode_jpeg, image, config.webui.frame_max_edge, config.webui.frame_jpeg_quality, _WEBUI_FRAME_MAX_BYTES
        )
        self._frame_seq += 1
        self._frame = Frame(
            frame_id=self._frame_seq, jpeg=jpeg, width=image.width, height=image.height, captured_at=self._now()
        )
        self._frame_clock = self._clock()
        self._frame_error = ""

    async def refresh_frame(self) -> None:
        """WebUI 观看时按最短间隔截一张新画面；已经有截图在进行时直接复用。"""
        config = self._config()
        if self._frame is not None and self._clock() - self._frame_clock < config.webui.min_frame_interval_seconds:
            return
        if self._frame_lock.locked():
            return
        async with self._frame_lock:
            try:
                device = self._screen_device()
                image = await asyncio.to_thread(open_image, await device.screenshot())
                await self._publish_frame(image, config)
            except DeviceError as exc:
                self._frame_error = str(exc)

    def frame_payload(self, since: int) -> Dict[str, Any]:
        """WebUI 画面数据；画面没变（``since`` 等于当前编号）时不重复传图片。"""
        frame = self._frame
        if frame is None:
            return {
                "frame_id": 0,
                "image": None,
                "captured_at": "",
                "width": 0,
                "height": 0,
                "error": self._frame_error,
            }
        return {
            "frame_id": frame.frame_id,
            "image": None if frame.frame_id == since else jpeg_data_url(frame.jpeg),
            "captured_at": frame.captured_at.strftime("%H:%M:%S"),
            "width": frame.width,
            "height": frame.height,
            "error": self._frame_error,
        }

    async def chat_screenshot(self) -> bytes:
        """截一张发到聊天里的 JPEG，同时刷新 WebUI 画面。"""
        config = self._config()
        device = self._screen_device()
        image = await asyncio.to_thread(open_image, await device.screenshot())
        await self._publish_frame(image, config)
        return await asyncio.to_thread(encode_jpeg, image, config.webui.chat_image_max_edge, _CHAT_JPEG_QUALITY)

    def _screen_device(self) -> PhoneDevice:
        """给 WebUI、聊天截图用的设备：正在玩时就是这次操作的那台，看到的才是她在玩的画面。"""
        session = self._session
        if session is not None and session.device is not None:
            return session.device
        return self._get_device()

    # ===== 会话 =====

    async def _run(self, session: Session) -> None:
        end_reason = ""
        try:
            await self._play(session)
            end_reason = session.end_reason
        except asyncio.CancelledError:
            end_reason = self._stop_reason or "被停止"
            raise
        except SessionEnd as exc:
            end_reason = exc.reason
        except (AgentError, DeviceError) as exc:
            end_reason = f"出错：{exc}"
            self._last_error = str(exc)
            self._logger.warning(f"玩手机出错：{exc}")
        except Exception as exc:
            self._logger.exception("玩手机过程中出现意外错误")
            end_reason = f"意外错误：{exc}"
            self._last_error = end_reason
        finally:
            await self._finish(session, end_reason or "玩完了")

    async def _finish(self, session: Session, end_reason: str) -> None:
        session.finishing = True
        session.end_reason = end_reason
        app = session.app
        # 她自己结束时用她写的总结；因为时间、步数到了而结束时，用最近的想法（停下来看时的想法最能代表看到了什么）
        seen = _recent_unique(session.watch_thoughts, 2) or _recent_unique(session.thoughts, 2)
        summary = session.finish_summary
        if len(summary) < _SHORT_SUMMARY:
            # 实测 gpt-6.1-terra 结束时只写「结束刷抖音」，聊天里提起这次时没什么可说的；用她看的时候的想法补上
            parts = [text for text in (summary, *seen, session.finish_thought) if text]
            summary = "；".join(dict.fromkeys(parts))
        summary = summary or "；".join(_recent_unique(session.summaries, 3)) or "没做什么"
        failed = end_reason.startswith(("出错", "意外错误", "连续"))
        self._log(session.step, f"结束：{end_reason}", ok=not failed, result=summary if app else "")
        config = self._config()
        current = session.current or app
        if current is not None:
            # 用最新配置里的这个应用（玩的时候可能改了回不回桌面）
            current = find_app(config.safety.allowed_apps, current.package) or current
        now = self._clock()
        for visit in session.visits:
            if visit.since is not None:
                visit.seconds += now - visit.since
                visit.since = None
        try:
            # 回桌面是为了视频别一直放着；联机游戏回桌面会退出房间，别人进来发现她走了
            # 你接管着手机时不替你回桌面，也不锁屏
            go_home = current is not None and current.goes_home(config.play.go_home_after)
            if self._controller == "bot":
                device = session.device or self._get_device()
                if go_home:
                    try:
                        await device.press_key("home")
                    except DeviceError as exc:
                        self._log(session.step, "没能回到桌面", ok=False, result=str(exc))
                await self._lock_after(session, config, device, current)
        finally:
            # 回桌面被打断（插件卸载、宿主退出）也要把状态改回来，不然再也开始不了
            self._state = "idle"
            self._session = None
            self._last_stream, self._last_ended_clock = session.origin_stream, self._clock()
            self._last_package = current.package if current else ""
            # 还没决定玩什么就结束了，让等待计划的调用方立刻拿到原因
            session.planned.set()
            self._resume.set()
        if app is None:
            # 没打开过应用不算玩了一次，也不发结束通知：原因已经由等待计划的调用方告诉对方，
            # 完整原因留在操作记录和最近的错误里
            return

        record = SessionRecord(
            started_at=session.started_at.isoformat(timespec="seconds"),
            ended_at=self._now().isoformat(timespec="seconds"),
            trigger=TRIGGER_LABELS[session.trigger],
            app=app.name,
            apps=[
                {"name": visit.app.name, "package": visit.app.package, "seconds": round(visit.seconds)}
                for visit in session.visits
            ],
            last_app=current.name if current else app.name,
            stats=session.stats.to_dict(),
            goal=session.goal,
            summary=summary,
            steps=session.step,
            end_reason=end_reason,
            together=session.together,
            interactions=_interaction_list(session),
            device=session.device_name if len(config.device_choices()) > 1 else "",
        )
        self._store.add_record(record)
        if self._memory is not None and session.goal:
            # 她自己玩够了算做完；时间到了、被叫停、被接管、出错都记成没做完，下次可以接着做
            if end_reason == "自己不想玩了":
                self._memory.clear_unfinished()
            else:
                self._memory.set_unfinished(
                    session.goal, current.name if current else app.name, end_reason, self._now()
                )
            await self._save_memory()
        try:
            await self._store.save()
            if session.origin_stream and session.notify:
                await self._notifier(record, session.origin_stream)
        except Exception:
            self._logger.exception("保存或通知玩手机结果失败")

    async def _lock_after(
        self, session: Session, config: PhonePlayConfig, device: PhoneDevice, current: Optional[AllowedApp]
    ) -> None:
        """玩完锁屏（play.lock_after，iPhone 和安卓真机）。没打开应用、也没替她解过锁的不动手机；现在连不上就不试了。"""
        if not config.play.lock_after or not device.supports_lock:
            return
        if not session.connected or self._connection.state != "ok":
            return
        if session.app is None and not session.unlocked:
            return
        if (current is not None and current.stays_open()) or device in self._cannot_lock:
            return
        if device in self._cannot_unlock:
            self._log(session.step, "这次不锁屏", result="上次没能自动解锁（可能设了锁屏密码），锁上了下次解不开")
            return
        try:
            locked = await device.lock()
        except DeviceError as exc:
            self._log(session.step, "没能锁屏", ok=False, result=str(exc))
            return
        if not locked:
            self._cannot_lock.add(device)
            self._log(session.step, "这台手机锁不了屏", result="按了关屏，屏幕还亮着，之后玩完不再试")
            return
        self._log(session.step, "锁屏了", result="下次玩时自动解开")

    async def _choose_plan(
        self, persona: Persona, apps: Sequence[AllowedApp], session: Session, config: PhonePlayConfig
    ) -> Plan:
        max_minutes = config.play.together_minutes if session.together else config.play.max_session_minutes
        prompt = build_plan_prompt(
            persona=persona,
            now=self._now(),
            apps=apps,
            recent=self._store.history[-5:],
            hint=session.hint,
            max_minutes=max_minutes,
            together=session.together,
            interaction=self._interaction_quota(config) is not None,
            memories=self._memory.prompt_lines(self._now()) if self._memory else (),
        )
        response = await self._call_model(session, "planner", self._planner, prompt)
        try:
            plan = parse_plan(response, apps, max_minutes)
        except PlanDeclined:
            raise
        except ValueError as exc:
            raise AgentError(f"没能决定玩什么：{exc}") from exc
        # 应用单独设了更短的上限就按应用；不会超过全局
        max_minutes = self._minutes_limit(session, plan.app, config)
        plan = Plan(app=plan.app, goal=plan.goal, minutes=min(plan.minutes, max_minutes))
        if session.together:
            # 一起玩时什么时候结束主要看对方，给满一起玩的时长
            plan = Plan(app=plan.app, goal=plan.goal, minutes=min(session.minutes_override or max_minutes, max_minutes))
        elif session.minutes_override:
            plan = Plan(app=plan.app, goal=plan.goal, minutes=min(session.minutes_override, max_minutes))
        return plan

    async def _play(self, session: Session) -> None:
        config = self._config()
        device = session.device or self._get_device()
        try:
            session.unlocked = await self._connect(device, _START_CONNECT_SECONDS)
            session.connected = True
            installed = await device.installed_packages([app.package for app in self._device_apps(session, config)])
        except DeviceError as exc:
            # 聊天里只说个大概，完整原因在 WebUI 的连接状态和最近错误里
            session.decline_text = {
                "needs_unlock": "手机锁着屏，得先解锁才能玩",
                "needs_action": "手机那边要先点一下允许，现在玩不了",
                "config": "手机的设置有问题，现在玩不了",
            }.get(exc.kind, "手机连不上，可能没开机或者没连好")
            raise
        self._log(0, f"已连接 {device.description}", result="手机锁着屏，已自动解开" if session.unlocked else "")
        apps = session.apps = [app for app in self._device_apps(session, config) if app.package in installed]
        if not apps:
            session.decline_text = "手机上没有你能玩的应用"
            session.end_reason = "白名单里的应用都没有安装在手机上，请先安装，或修改插件配置的 allowed_apps"
            self._last_error = session.end_reason
            return
        home_packages: Set[str] = set()
        try:
            home_packages = await device.home_packages()
        except DeviceError as exc:
            self._log(0, "没能识别桌面", ok=False, result=str(exc))

        persona = await self._persona()
        while True:
            try:
                plan = await self._choose_plan(persona, apps, session, config)
            except PlanDeclined as exc:
                names = "、".join(app.name for app in apps)
                session.decline_text = f"手机上做不到：{exc}（手机上能玩的只有{names}）"
                session.end_reason = f"手机上做不到：{exc}"
                return
            self._log(
                0,
                f"打算打开{plan.app.name}：{plan.goal}",
                action=f"打开 {plan.app.package}",
                result=f"计划玩 {plan.minutes} 分钟",
            )
            # 想玩什么的时候被接管：等交还了再打开应用
            if not await self._wait_running(session):
                return
            try:
                if self._should_reopen(session, plan.app, config):
                    await device.stop_app(plan.app.package)
                await device.launch_app(plan.app.package)
                break
            except AppNotInstalledError as exc:
                # iPhone 查不到装了哪些应用，打开时才知道没装：去掉它，重新决定玩什么
                self._log(0, f"手机上没有装{plan.app.name}", ok=False, result=str(exc))
                apps = session.apps = [app for app in apps if app.package != plan.app.package]
                if not apps:
                    session.decline_text = "手机上没有你能玩的应用"
                    session.end_reason = "白名单里的应用手机上都没有装，请先安装，或修改插件配置的 allowed_apps"
                    self._last_error = session.end_reason
                    return
        session.app, session.goal, session.max_minutes = plan.app, plan.goal, plan.minutes
        self._enter_app(session, plan.app)
        session.planned.set()
        self._state = "playing"
        # 真正打开应用才算玩了一次；马上存下来，玩到一半宿主崩溃也不会丢
        self._store.add_session(self._today())
        await self._save_usage()
        await self._sleep(self._interval(session, config))

        history: Deque[str] = deque(maxlen=config.play.history_steps)
        consecutive_errors = 0
        while True:
            if not await self._wait_running(session):
                return
            config = self._config()
            reason = self._stop_check(session, config)
            if reason:
                session.end_reason = reason
                return
            # 白名单按最新配置：玩的时候删掉的应用下一步就不能用了（新加的没查过装没装，下次再算）
            installed = {app.package for app in session.apps}
            apps = [app for app in self._device_apps(session, config) if app.package in installed]
            if not apps:
                session.end_reason = "白名单里没有这次能用的应用了"
                return
            try:
                ok, finished, wait_seconds = await self._step(
                    device, session, config, apps, home_packages, persona, history
                )
            except ControlTaken:
                # 做决定期间被接管：想好的动作作废，交还后重新看画面
                self._log(session.step, "接管期间想好的这一步作废了", result="交还控制后按新画面重新决定")
                continue
            except DeviceError as exc:
                if not exc.retryable:
                    raise
                # 实测 iPhone 一晚上会断开好几次（数据线松动、WDA 被重启），十几秒就恢复；等它恢复再接着玩，等不到才结束
                self._log(session.step, "手机断开了，等它恢复", ok=False, result=str(exc))
                if await self._connect(device, _RECONNECT_SECONDS):
                    session.unlocked = True
                self._log(session.step, "手机恢复了，接着玩")
                history.append(f"第{session.step}步：手机断开了一会儿，刚恢复，先看看现在是什么画面")
                ok, finished, wait_seconds = False, False, 0.0
            if finished:
                return
            session.trace.append((session.last_signature, session.step_kind, session.step_failure))
            session.step_kind, session.step_failure = "", ""
            stuck = self._stuck_reason(session)
            if stuck:
                session.end_reason = f"卡住了：{stuck}"
                self._last_error = session.end_reason
                return
            consecutive_errors = 0 if ok else consecutive_errors + 1
            if consecutive_errors >= config.play.max_consecutive_errors:
                session.end_reason = f"连续 {consecutive_errors} 步操作失败（最后一次：{session.last_failure}）"
                self._last_error = session.end_reason
                return
            if session.wait_video:
                await self._watch_video(session, wait_seconds, device)
            else:
                # 在房间里等人、等开局这种长等待，期间截图比对，有人进来、开局了就提前醒来
                await self._nap(session, wait_seconds, device if wait_seconds > PLAIN_WAIT_SECONDS else None)

    @staticmethod
    def _note_failure(session: Session, text: str, key: str = "") -> None:
        """记下这一步的失败：``text`` 给用户看，``key`` 用来比对是不是同一种失败（默认就是 text）。"""
        session.last_failure = text
        session.step_failure = key or text

    @staticmethod
    def _stuck_reason(session: Session) -> str:
        """结合最近的操作和结果看是不是卡住了，卡住时返回卡在哪，没有返回空字符串。

        正常阅读（每次翻页画面都变）、看视频（等待不算）、等对方（wait_turn 不算）都不会误判。
        """
        last = session.last_action
        if (
            last is not None
            and last.kind in _TAP_KINDS
            and session.repeat_count >= _STUCK_SAME_TAPS
            and session.unchanged_steps >= _STUCK_SAME_TAPS - 1
        ):
            return f"连续 {session.repeat_count} 次{last.describe()}，画面一直没变"
        recent = list(session.trace)[-_STUCK_PING_PONG:]
        if len(recent) == _STUCK_PING_PONG and all(kind in _NAVIGATION_KINDS for _, kind, _ in recent):
            screens = [signature for signature, _, _ in recent]
            back_and_forth = all(
                signature_distance(screens[index], screens[index - 2]) < _SAME_SCREEN_DISTANCE
                and signature_distance(screens[index], screens[index - 1]) > _OTHER_SCREEN_DISTANCE
                for index in range(2, len(screens))
            )
            if back_and_forth:
                return f"最近 {_STUCK_PING_PONG} 步一直在两个画面之间来回切换"
        failures = Counter(failure for _, _, failure in session.trace if failure)
        if failures:
            failure, count = failures.most_common(1)[0]
            if count >= _STUCK_SAME_FAILURE:
                return f"最近 {len(session.trace)} 步里同一个问题出现了 {count} 次：{failure}"
        return ""

    async def _call_model(
        self, session: Session, kind: str, call: Callable[[Any], Awaitable[Any]], payload: Any
    ) -> str:
        """调用模型，统一计数、计时、查预算，并记下宿主给出的 token 用量。

        每次尝试先算一次再调用：失败、超时的也算，不会漏算；同一次调用只算一次，不会重复算。

        Raises:
            SessionEnd: 这次或今天的模型调用预算用完了。
            AgentError: 模型调用失败。
        """
        config = self._config()
        stats = session.stats
        if stats.calls >= config.play.max_model_calls_per_session:
            raise SessionEnd(f"这次的模型调用预算用完了（{config.play.max_model_calls_per_session} 次）")
        if self._store.model_calls(self._today()) >= config.play.daily_model_call_limit:
            raise SessionEnd(f"今天的模型调用预算用完了（{config.play.daily_model_call_limit} 次）")
        if kind == "planner":
            stats.planner_calls += 1
        else:
            stats.vision_calls += 1
        self._store.add_model_call(self._today())
        started = time.perf_counter()
        try:
            reply = await call(payload)
        except AgentError:
            stats.failed_calls += 1
            raise
        finally:
            stats.seconds["model"] += time.perf_counter() - started
        if isinstance(reply, str):
            return reply
        if reply.total_tokens:
            stats.calls_with_usage += 1
            stats.total_tokens += reply.total_tokens
            stats.prompt_tokens += reply.prompt_tokens or 0
            stats.completion_tokens += reply.completion_tokens or 0
        return reply.text

    async def _read_foreground(self, device: PhoneDevice) -> Tuple[str, str]:
        """查前台应用，并更新这个设备连接识别前台的能力，返回（包名，能力）。

        认出来过就是 supported，之后再认不出也只当暂时失败，不会因为连着几次空值就降成不支持；
        只有设备明确给不出（系统输出里没有相关字段、接口不存在）而且从没认出来过，才是 unsupported。
        """
        reading = await device.foreground()
        support = self._foreground_support.get(device, "unknown")
        if reading.package:
            support = "supported"
        elif reading.unsupported and support != "supported":
            support = "unsupported"
        self._foreground_support[device] = support
        return reading.package, support

    @staticmethod
    def _check_control(session: Session, epoch: int) -> None:
        if session.control_epoch != epoch:
            raise ControlTaken

    def _should_reopen(self, session: Session, app: AllowedApp, config: PhonePlayConfig) -> bool:
        """打开应用时要不要先关掉、从首页开始。

        平时先关掉：应用在后台时直接打开会回到上次停下的页面（比如上次看的视频），容易被带跑。
        但一起玩时上次停下的多半是和对方组好的房间；应用自己设了 reopen_on_start（或旧的 keep_open）就按它；
        刚玩过的同一个应用多半是接着玩（实测群友一喊「回房间」她就重开一次，每次都冷启动）。
        """
        if session.together:
            return False
        recent = app.package == self._last_package and self._clock() - self._last_ended_clock < _RESUME_SECONDS
        return app.reopens(config.play.fresh_start and not recent)

    @staticmethod
    def _interval(session: Session, config: PhonePlayConfig) -> float:
        """每步操作后等几秒：现在在用的应用单独设了就按它（阅读、看视频的节奏不一样），否则用全局的。"""
        app = session.current or session.app
        if app is not None and app.step_interval_seconds:
            return app.step_interval_seconds
        return config.play.step_interval_seconds

    def _minutes_limit(self, session: Session, app: Optional[AllowedApp], config: PhonePlayConfig) -> int:
        """这次最多玩几分钟：全局上限（一起玩用 together_minutes），应用单独设了更短的就按应用，不会超过全局。"""
        limit = config.play.together_minutes if session.together else config.play.max_session_minutes
        if app is not None and app.max_minutes:
            limit = min(limit, app.max_minutes)
        return limit

    def _enter_app(self, session: Session, app: AllowedApp) -> None:
        """记下她切到了哪个应用，以及在上一个应用里大概待了多久。"""
        if session.current is not None and session.current.package == app.package:
            return
        # 换了应用，上一个应用里认出来的页面不作数了（WebUI 上别还显示着 B 站的播放器）
        session.page = ""
        now = self._clock()
        for visit in session.visits:
            if visit.since is not None:
                visit.seconds += now - visit.since
                visit.since = None
        visit = next((visit for visit in session.visits if visit.app.package == app.package), None)
        if visit is None:
            visit = Visit(app=app)
            session.visits.append(visit)
        visit.since = now
        session.current = app

    async def _nap(self, session: Session, seconds: float, device: Optional[PhoneDevice] = None) -> None:
        """等到下一步；对方在聊天里提了新要求就提前醒来。

        给了 ``device`` 时每隔几秒截图比对，画面大变（有人进房间、开局了）也提前醒来；等待期间不调用模型。
        """
        self._wake.clear()
        if session.requests:
            return
        if device is None:
            await self._sleep_or_wake(seconds)
            return
        try:
            baseline = await self._screen_signature(device)
            waited = 0.0
            while waited < seconds:
                interval = min(_WAIT_POLL_SECONDS, seconds - waited)
                if await self._sleep_or_wake(interval):
                    return
                waited += interval
                if signature_distance(await self._screen_signature(device), baseline) > _WAKE_CHANGE_DISTANCE:
                    return
        except DeviceError:
            # 截不了图就不等了，下一步会按断开处理
            return

    async def _watch_video(self, session: Session, seconds: float, device: PhoneDevice) -> None:
        """看视频：画面本来就一直在变，不按画面变化叫醒她，看满她定的时间。

        聊天里有新要求、被叫停或接管时马上醒来；每隔几秒看一眼前台，点到广告跳去了别的应用也提前醒来。
        """
        self._wake.clear()
        if session.requests:
            return
        app = session.current or session.app
        waited = 0.0
        try:
            while waited < seconds:
                interval = min(_VIDEO_POLL_SECONDS, seconds - waited)
                if await self._sleep_or_wake(interval):
                    return
                waited += interval
                if waited < seconds and app is not None:
                    package, _ = await self._read_foreground(device)
                    if package and package != app.package:
                        return
        except DeviceError:
            # 看不了前台就不等了，下一步会按断开处理
            return
        finally:
            session.watch_seconds += waited
            if session.watch_item and self._memory is not None:
                self._memory.add_watched_seconds(session.watch_item, waited)

    async def _sleep_or_wake(self, seconds: float) -> bool:
        """睡一会儿；被聊天里的新要求叫醒时返回 True。"""
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waker.cancel()
        return self._wake.is_set()

    async def _screen_signature(self, device: PhoneDevice) -> bytes:
        image = await asyncio.to_thread(open_image, await device.screenshot())
        # 长等待时 WebUI 的画面也跟着刷新
        await self._publish_frame(image, self._config())
        return await asyncio.to_thread(screen_signature, image)

    @staticmethod
    def _device_apps(session: Session, config: PhonePlayConfig) -> List[AllowedApp]:
        """白名单里这次的设备能用的应用（应用的 devices 没写就是都能用）。"""
        return [app for app in config.safety.allowed_apps if app.available_on(session.device_name)]

    def _stop_check(self, session: Session, config: PhonePlayConfig) -> str:
        settings = dict(config.device_choices()).get(session.device_name)
        if session.device_key and (settings is None or settings.model_dump_json() != session.device_key):
            # 接着玩会操作旧手机、截图和回桌面却发给新手机
            return "设备配置改了，这次先停下"
        if session.step >= config.play.max_steps_per_session:
            return f"达到单次 {config.play.max_steps_per_session} 步的上限"
        if self._elapsed_seconds(session) >= session.max_minutes * 60:
            return f"玩够 {session.max_minutes} 分钟了"
        _, steps = self._store.usage(self._today())
        if steps >= config.play.daily_step_limit:
            return "今天的操作步数用完了"
        return ""

    async def _connect(self, device: PhoneDevice, seconds: float) -> bool:
        """确认手机能用，锁着屏就解开，返回解没解过锁。暂时连不上（网络抖动、WDA 重启）就在 ``seconds`` 秒内
        每隔几秒再试，配置错误、要人处理（解不开锁、允许调试）的不重试。开始玩和玩到一半断开都用这一个。

        按真实时间算总时长，每次连接最多等剩下的时间（一次请求最多要等 20 秒）；被叫停时随会话任务一起取消。

        Raises:
            DeviceError: 到截止时间还连不上，或者遇到重试也没用的错误。
        """
        deadline = self._clock() + seconds
        attempt = 0
        while True:
            attempt += 1
            try:
                unlocked = await asyncio.wait_for(
                    self._ready_and_unlocked(device), timeout=max(deadline - self._clock(), 0.001)
                )
            except DeviceError as exc:
                error = exc
            except asyncio.TimeoutError:
                error = DeviceError(f"连接手机超时：{device.description} {seconds:g} 秒内没有回应")
            else:
                self._mark_connected()
                return unlocked
            if not error.retryable or self._clock() + _RECONNECT_INTERVAL >= deadline:
                self._connection.state = "failed" if error.kind == "transient" else error.kind
                self._connection.error, self._connection.deadline = str(error), 0.0
                raise error
            self._connection.state, self._connection.error = "reconnecting", str(error)
            self._connection.attempt, self._connection.deadline = attempt, deadline
            await self._sleep(_RECONNECT_INTERVAL)

    async def _ready_and_unlocked(self, device: PhoneDevice) -> bool:
        await device.ensure_ready()
        return await self._unlock(device)

    async def _unlock(self, device: PhoneDevice) -> bool:
        """锁着屏就解开，返回解没解过；解不开时记下这台手机（玩完不再锁屏），连接状态改成要解锁。

        Raises:
            DeviceNeedsUserError: 解不开（设了锁屏密码）。
        """
        try:
            unlocked = await device.unlock_if_locked()
        except DeviceError as exc:
            if exc.kind == "needs_unlock":
                self._cannot_unlock.add(device)
                self._connection.state, self._connection.error = exc.kind, str(exc)
                self._connection.attempt, self._connection.deadline = 0, 0.0
            raise
        if unlocked:
            self._cannot_unlock.discard(device)
        return unlocked

    def _mark_connected(self) -> None:
        connection = self._connection
        connection.state, connection.error, connection.attempt, connection.deadline = "ok", "", 0, 0.0
        connection.last_ok_at = self._now()

    def _connection_payload(self, config: PhonePlayConfig) -> Dict[str, Any]:
        connection = self._connection
        remaining = max(0, round(connection.deadline - self._clock())) if connection.deadline else 0
        return {
            "state": connection.state,
            "label": CONNECTION_LABELS[connection.state],
            "last_ok_at": connection.last_ok_at.strftime("%m-%d %H:%M:%S") if connection.last_ok_at else "",
            "attempt": connection.attempt,
            "remaining_seconds": remaining,
            "error": connection.error,
            "hint": _connection_hint(connection.state, self._current_choice(config)[1]),
        }

    async def _save_memory(self) -> None:
        if self._memory is None:
            return
        try:
            await self._memory.save()
        except OSError as exc:
            self._logger.warning(f"保存内容记忆失败：{exc}")

    async def _save_usage(self) -> None:
        """及时保存次数、步数和互动计数；存不了只记日志，不打断玩手机。"""
        try:
            await self._store.save()
        except OSError as exc:
            self._logger.warning(f"保存使用记录失败：{exc}")

    def _interaction_quota(self, config: PhonePlayConfig) -> Optional[InteractionQuota]:
        """开启互动时今天还剩的额度；没开启时返回 None。"""
        settings = config.interaction
        if not settings.enabled:
            return None
        used = self._store.interactions(self._today())

        def left(kind: str, allowed: bool, limit: int) -> int:
            return max(0, limit - used.get(kind, 0)) if allowed else 0

        return InteractionQuota(
            likes=left("like", settings.allow_like, settings.daily_like_limit),
            # 写评论也是输入文字，关了文字输入就不教她评论
            comments=left(
                "comment", settings.allow_comment and config.safety.allow_text_input, settings.daily_comment_limit
            ),
            follows=left("follow", settings.allow_follow, settings.daily_follow_limit),
            comment_max_length=settings.comment_max_length,
        )

    def _check_interaction(self, config: PhonePlayConfig, kind: str) -> None:
        """点赞、评论、关注前检查开关和今天的额度。

        Raises:
            ActionError: 没开启、这一类不允许或今天的额度用完时抛出。
        """
        quota = self._interaction_quota(config)
        if quota is None:
            raise ActionError("没有开启互动，不能用自己的账号点赞、评论、关注")
        left = {"like": quota.likes, "comment": quota.comments, "follow": quota.follows}[kind]
        if left <= 0:
            raise ActionError(f"今天不能再{_INTERACTION_LABELS[kind]}了")

    @staticmethod
    def _foreground_label(package: str, apps: Sequence[AllowedApp], home_packages: Set[str]) -> str:
        if not package:
            return ""
        if package in home_packages:
            return "桌面"
        app = find_app(apps, package)
        return f"{app.name}（{package}）" if app else package

    async def _step(
        self,
        device: PhoneDevice,
        session: Session,
        config: PhonePlayConfig,
        apps: Sequence[AllowedApp],
        home_packages: Set[str],
        persona: Persona,
        history: Deque[str],
    ) -> Tuple[bool, bool, float]:
        """执行一步，返回（是否成功，是否结束，下一步前等待的秒数）。"""
        session.step += 1
        step = session.step
        session.wait_video = ""
        self._store.add_step(self._today())
        await self._save_usage()
        interval = self._interval(session, config)

        # 玩到一半锁屏了（有人按了电源键、到了自动锁定的时间）：先解开再看画面；解不开就结束这次
        if await self._unlock(device):
            session.unlocked = True
            self._log(step, "手机锁屏了，已自动解开")
            history.append(f"第{step}步前：手机锁屏了，已经帮你解开；画面可能回到了桌面，按现在看到的接着玩")

        started = time.perf_counter()
        image = await asyncio.to_thread(open_image, await device.screenshot())
        session.stats.seconds["screenshot"] += time.perf_counter() - started
        self._mark_connected()
        await self._publish_frame(image, config)
        width, height = image.size
        if session.handed_back:
            # 刚交还控制：对方可能动过手机，之前的画面比对、重复操作记录都不作数了
            session.handed_back = False
            session.last_signature, session.unchanged_steps = b"", 0
            session.last_action, session.repeat_count = None, 0
            history.append(f"第{step}步前：对方接管过手机，刚交还给你；画面可能变了，按现在看到的重新决定")
        signature = await asyncio.to_thread(screen_signature, image)
        if session.last_signature and signature_distance(signature, session.last_signature) < _UNCHANGED_DISTANCE:
            session.unchanged_steps += 1
        else:
            session.unchanged_steps = 0
        session.last_signature = signature

        # 安全检查：不管模型怎么想，停在白名单以外的应用里就直接回桌面
        package, support = await self._read_foreground(device)
        allowed = {app.package for app in apps} | home_packages | set(config.safety.extra_allowed_packages)
        if package and package not in allowed:
            await device.press_key("home")
            result = f"前台是 {package}，不在允许范围内，已回到桌面"
            self._log(step, "离开了不允许的应用", action="回桌面", ok=False, result=result)
            history.append(f"第{step}步：{result}")
            self._note_failure(session, result)
            return False, False, interval
        if not package and support != "unsupported":
            # 认不出前台就没法确认还在白名单里：这一步不问模型、不操作；连续几步这样会按连续失败结束
            result = _foreground_unknown_text(support)
            diagnostic = await self._record_failure(session, step, image, None, [], result)
            self._log(step, "看不出现在在哪个应用，先不操作", ok=False, result=result, diagnostic=diagnostic)
            self._note_failure(session, result)
            return False, False, interval
        if not package and not session.foreground_noted:
            session.foreground_noted = True
            self._log(step, "这台手机给不出前台应用", result="没法检查她有没有离开白名单里的应用，请留意 WebUI 画面")
        # 她可能从桌面点进了别的应用，不只是用 launch 切换
        found = find_app(apps, package)
        if found is not None:
            self._enter_app(session, found)

        elements: List[UIElement] = []
        elements_note = ""
        if config.play.use_ui_tree:
            started = time.perf_counter()
            try:
                elements = await device.ui_elements(width, height, config.play.max_elements)
            except DeviceError as exc:
                elements_note = f"这个画面读不到界面元素（{exc}），请直接用坐标。"
            session.stats.seconds["ui"] += time.perf_counter() - started
        # 同一个视频从首页、搜索点开是两种页面，操作不一样；认出来就只讲这一种
        page = detect_page(package, elements, width, height)
        session.page = page
        model_image = image
        if elements and config.play.mark_elements:
            model_image = await asyncio.to_thread(draw_element_marks, image, elements)
        model_jpeg = await asyncio.to_thread(
            encode_jpeg, model_image, config.play.model_image_max_edge, _MODEL_JPEG_QUALITY
        )
        requests, session.requests = session.requests, []
        earlier_requests = session.request_log[-_REQUEST_LOG_SHOWN:]
        session.request_log.extend(requests)
        history.extend(f"第{step}步前，对方在聊天里让你：{text}" for text in requests)
        device_settings = session.device_settings or config.device
        context = DecisionContext(
            persona=persona,
            goal=session.goal,
            apps=apps,
            allow_text_input=config.safety.allow_text_input,
            ascii_text_only=device_settings.backend == "adb" and device_settings.text_input == "ascii_only",
            max_text_length=config.safety.max_text_length,
            now=self._now(),
            elapsed_minutes=self._elapsed_seconds(session) / 60,
            max_minutes=session.max_minutes,
            step=step,
            max_steps=config.play.max_steps_per_session,
            foreground=self._foreground_label(package, apps, home_packages),
            history=list(history),
            elements=elements,
            elements_note=elements_note,
            image_url=jpeg_data_url(model_jpeg),
            unchanged_steps=session.unchanged_steps,
            together=session.together,
            ios=device_settings.backend == "ios_wda",
            interaction=self._interaction_quota(config),
            repeated_steps=session.repeat_count if session.repeat_count >= _REPEAT_WARNING else 0,
            repeated_action=session.last_action.describe() if session.last_action else "",
            requests=requests,
            earlier_requests=earlier_requests,
            watched=self._watched_titles(session, config),
            watching=_watching_text(session),
            app_tips=app_tips(package, page),
            skip_watched=config.play.skip_watched_videos,
        )

        epoch = session.control_epoch
        try:
            action = parse_action(
                await self._call_model(session, "vision", self._vision, build_decision_messages(context))
            )
        except (AgentError, ActionError) as exc:
            diagnostic = await self._record_failure(session, step, image, None, elements, f"没能决定下一步：{exc}")
            self._log(step, "没能决定下一步", ok=False, result=str(exc), diagnostic=diagnostic)
            history.append(f"第{step}步：没有得到有效动作（{exc}），请严格按格式只输出一个 JSON")
            self._note_failure(session, f"没能决定下一步：{exc}")
            return False, False, interval
        if session.control_epoch != epoch:
            # 模型想的时候对方接管了（可能已经交还）：这个决定是按旧画面想的，作废
            raise ControlTaken
        if action.video and (action.seen is None or action.seen.media != "video"):
            # 详情页（和安卓的竖屏播放器）的标题就在界面元素里：她没写 seen 也能记下看过哪个视频、拦住重看
            title = page_title(page, elements)
            if title:
                action = replace(action, seen=Seen(media="video", title=title))

        session.step_kind = action.kind
        same = _same_action(session.last_action, action)
        session.repeat_count = session.repeat_count + 1 if same else int(action.kind in _REPEAT_KINDS)
        session.last_action = action

        if action.thought:
            session.last_thought = action.thought
        summary = action.summary or action.describe()
        if action.kind in _IN_APP_KINDS:
            # 模型想的这几秒里前台可能换了（广告跳到了别的应用），做之前再看一次，不在白名单里就不做
            now_package, support = await self._read_foreground(device)
            result = ""
            if now_package and now_package not in allowed:
                await device.press_key("home")
                result = f"做之前前台变成了 {now_package}，不在允许范围内，这一步没做，已回到桌面"
            elif not now_package and support != "unsupported":
                # 认不出就不做，直到重新认出来；只有明确给不出前台的设备才照常做
                result = f"做之前{_foreground_unknown_text(support)}，这一步先不做"
            if result:
                diagnostic = await self._record_failure(session, step, image, action, elements, result)
                self._log(
                    step,
                    summary,
                    thought=action.thought,
                    action=action.describe(),
                    ok=False,
                    result=result,
                    diagnostic=diagnostic,
                )
                history.append(f"第{step}步：{summary} → {result}")
                self._note_failure(session, result)
                return False, False, interval
        watched = self._watched_block(session, action, elements, width, height, config)
        if watched:
            # 不是她做错了，不算连续出错；反复这样也会被当成卡住
            self._log(step, summary, thought=action.thought, action=action.describe(), ok=False, result=watched)
            history.append(f"第{step}步：{summary} → {watched}")
            session.step_failure = watched
            return True, False, 0.0
        started = time.perf_counter()
        try:
            result = await self._execute(device, action, image, elements, apps, config, session, epoch)
        except StaleTarget as exc:
            # 不是她做错了，不算连续出错；画面刚截过，不用再等
            diagnostic = await self._record_failure(session, step, image, action, elements, f"没点：{exc}")
            self._log(
                step,
                summary,
                thought=action.thought,
                action=action.describe(),
                ok=False,
                result=f"没点：{exc}",
                diagnostic=diagnostic,
            )
            history.append(f"第{step}步：{summary} → 没点：点之前画面变了，{exc}。先看看现在的画面再决定")
            # 不算出错，但反复这样也是卡住了
            session.step_failure = f"没点：{exc}"
            return True, False, 0.0
        except (ActionError, DeviceError) as exc:
            diagnostic = await self._record_failure(session, step, image, action, elements, str(exc))
            self._log(
                step,
                summary,
                thought=action.thought,
                action=action.describe(),
                ok=False,
                result=str(exc),
                diagnostic=diagnostic,
            )
            history.append(f"第{step}步：{summary} → 失败：{exc}")
            # 每一步的 summary 措辞不同，比对「同一种失败」只看错误本身
            self._note_failure(session, f"{summary}：{exc}", str(exc))
            return False, False, interval

        finally:
            session.stats.seconds["action"] += time.perf_counter() - started
        self._log(step, summary, thought=action.thought, action=action.describe(), result=result)
        note = _page_note(page, action)
        if action.video and (action.seen is None or action.seen.media != "video"):
            # 没写标题就记不住看过它，下次还会点进来；每次都提醒一下
            note += "（没写 seen 标题，插件记不住看过这个视频；标题看得清就写上）"
        history.append(f"第{step}步：{summary}（{action.describe()}）→ {_history_result(result)}{note}")
        item: Optional[MemoryItem] = None
        if action.seen is not None and self._memory is not None:
            current = session.current or session.app
            name = current.name if current else ""
            if action.seen.media == "video":
                item = self._memory.remember_watched(action.seen, name, action.video, self._now())
                session.video_item, session.video_title = item.id, item.title
            else:
                self._memory.remember_content(action.seen, name, self._now())
            await self._save_memory()
        # 详情页里上下滑、切简介和评论，视频还在上面接着播，不算换了一个
        keeps_video = page == BILI_DETAIL and (action.kind == "swipe" or _is_detail_tab(action, elements))
        self._follow_watching(session, action, item, keeps_video)
        if action.kind == "finish":
            session.finish_summary = action.summary or action.thought
            session.finish_thought = action.thought
            session.end_reason = "自己不想玩了"
            return True, True, 0.0
        session.summaries.append(summary)
        if action.thought:
            session.thoughts.append(action.thought)
            if action.kind == "wait":
                session.watch_thoughts.append(action.thought)
        if action.kind == "wait":
            session.wait_video = action.video
            return True, False, action.seconds
        if action.kind == "wait_turn":
            # 已经等到对方操作完了，画面是新的，不用再等
            return True, False, 0.0
        # 这一步可能切了应用：按切过去的应用的节奏等它加载
        return True, False, self._interval(session, config)

    @staticmethod
    def _follow_watching(session: Session, action: PhoneAction, item: Optional[MemoryItem], keeps_video: bool) -> None:
        """更新「连着看的这一段」：看视频接着上一步就是同一段（认出了不同的标题除外），别的操作结束这一段。

        ``keeps_video`` 是这一步没换视频（比如详情页里翻评论），这一段接着算。
        """
        if keeps_video:
            return
        if action.kind != "wait" or not action.video:
            session.watching, session.watch_item, session.watch_title, session.watch_seconds = False, "", "", 0.0
            return
        if not session.watching or (item is not None and item.id != session.watch_item and session.watch_item):
            session.watch_item, session.watch_title, session.watch_seconds = "", "", 0.0
        if item is not None:
            session.watch_item, session.watch_title = item.id, item.title
        session.watching, session.watch_form = True, action.video

    def _watched_titles(self, session: Session, config: PhonePlayConfig) -> List[str]:
        """这个应用里最近看过的视频，告诉她刷到了别再看；关了 skip_watched_videos 就不提。"""
        app = session.current or session.app
        if self._memory is None or app is None or not config.play.skip_watched_videos:
            return []
        titles = self._memory.watched_titles(app.name, self._now(), _WATCHED_SHOWN + 1)
        # 正在看的这个不算「别再看」
        return [
            title if len(title) <= _WATCHED_TITLE_SHOWN else f"{title[:_WATCHED_TITLE_SHOWN]}…"
            for title in titles
            if title != session.video_title
        ][:_WATCHED_SHOWN]

    def _watched_block(
        self,
        session: Session,
        action: PhoneAction,
        elements: Sequence[UIElement],
        width: int,
        height: int,
        config: PhonePlayConfig,
    ) -> str:
        """要点开或接着看的视频以前看过，就不做这一步，返回给她看的原因；没看过返回空字符串。

        点列表里的卡片时按卡片上的文字（B 站首页、搜索结果的卡片文字就是标题）比对；
        在播放器里刷到时按她写的 seen 标题比对，正在看的这个接着看不拦。对方点名要看的不拦。
        """
        app = session.current or session.app
        if self._memory is None or app is None or not config.play.skip_watched_videos:
            return ""
        now = self._now()
        asked = normalize_title(" ".join([session.hint, session.goal, *session.request_log, *session.requests]))
        if action.kind in ("tap", "long_press"):
            card = _card(action, elements, width, height)
            if card is None:
                return ""
            item = self._memory.find_watched(app.name, card.label, now)
            if item is None:
                # 她没记下、但 B 站自己在搜索结果卡片上标着「最近看过」的，也算看过
                mark = marked_watched(card, elements) if app.package in BILIBILI_PACKAGES else None
                if mark is None or normalize_title(card.label) in asked:
                    return ""
                return f"没点：《{card.label}》B 站标着「{mark}」，换一个没看过的"
            verb = "没点"
        elif action.kind == "wait" and action.seen is not None and action.seen.media == "video":
            item = self._memory.find_watched(app.name, action.seen.title, now)
            if item is None or item.id == session.video_item:
                return ""
            verb = "没看"
        else:
            return ""
        if normalize_title(item.title) in asked:
            return ""
        when = _ago(datetime.fromisoformat(item.updated_at), now)
        return f"{verb}：《{item.title}》{when}看过了，换一个没看过的（短视频直接上滑，列表里点别的）"

    async def _confirm_point(
        self,
        device: PhoneDevice,
        action: PhoneAction,
        image: Image.Image,
        elements: Sequence[UIElement],
        config: PhonePlayConfig,
    ) -> Tuple[int, int]:
        """求出要点的位置，并确认那里还是她看到的东西。

        从截图到点下去隔着一次模型调用（实测 5~10 秒），弹窗可能已经自己收起：实测她去点 B 站弹窗的「不再提醒」，
        弹窗先没了，点到了底下的投稿按钮。所以先截一张新图比对要点的那块，没变就直接点；变了再重新读界面，
        同一个元素还在原处就按它现在的位置点，不在了（或者原来空着的地方冒出了别的东西）就不点。
        读不到界面元素的画面（游戏、视频）没法确认，照原样点。

        Raises:
            ActionError: 元素编号不存在或缺少坐标时抛出。
            StaleTarget: 要点的东西不在原处了。
            DeviceError: 截图或读取界面失败时抛出。
        """
        width, height = image.size
        x, y = resolve_point(action, elements, width, height)
        if not elements:
            return x, y
        if action.element is not None:
            target: Optional[UIElement] = next(element for element in elements if element.index == action.element)
        else:
            target = element_at(elements, x, y)
        if target is not None:
            region = target.bounds
        else:
            half = max(1, round(width * _POINT_BOX_RATIO))
            region = (max(0, x - half), max(0, y - half), min(width, x + half), min(height, y + half))

        current = await asyncio.to_thread(open_image, await device.screenshot())
        if current.size != image.size:
            raise StaleTarget("屏幕转过来了")
        if await asyncio.to_thread(region_distance, image, current, region) < _TARGET_CHANGE_DISTANCE:
            return x, y

        now = await device.ui_elements(width, height, config.play.max_elements)
        if target is None:
            covering = element_at(now, x, y)
            if covering is None:
                return x, y
            raise StaleTarget(f"那里现在是「{covering.label or covering.kind}」")
        same = find_same(target, now)
        if same is None:
            raise StaleTarget(f"「{target.label or target.kind}」已经不在原来的位置了")
        if action.element is None and element_at([same], x, y) is not None:
            return x, y
        return same.center

    async def _execute(
        self,
        device: PhoneDevice,
        action: PhoneAction,
        image: Image.Image,
        elements: Sequence[UIElement],
        apps: Sequence[AllowedApp],
        config: PhonePlayConfig,
        session: Session,
        epoch: int,
    ) -> str:
        """执行动作并返回结果说明；``image`` 是做决定时的截图，``epoch`` 是做决定时的接管序号。

        Raises:
            ActionError: 动作越过安全边界或参数无效时抛出。
            StaleTarget: 点之前画面变了，要点的东西不在原处。
            ControlTaken: 执行前对方接管了手机。
            DeviceError: 设备执行失败时抛出。
        """
        self._check_control(session, epoch)
        kind = action.kind
        width, height = image.size
        if kind in ("like", "follow"):
            self._check_interaction(config, kind)
            x, y = await self._confirm_point(device, action, image, elements, config)
            # 核对画面要截图、读界面，这期间也可能被接管
            self._check_control(session, epoch)
            await device.tap(x, y)
            self._store.add_interaction(self._today(), kind)
            await self._save_usage()
            if kind == "like":
                session.likes += 1
            else:
                session.interactions.append(f"关注了{action.summary.removeprefix('关注') or '一个博主'}")
            return f"已{_INTERACTION_LABELS[kind]} ({x}, {y})"
        if kind == "comment":
            if not config.safety.allow_text_input:
                # 文字输入开关管所有输入：之前关了它、开着评论时 comment 照样能写字
                raise ActionError("配置不允许输入文字，不能写评论")
            self._check_interaction(config, kind)
            text = action.text.strip()
            if _COMMENT_FORBIDDEN.search(text):
                raise ActionError("评论里不能有链接、@ 别人或联系方式")
            if len(text) > config.interaction.comment_max_length:
                raise ActionError(f"评论太长了，最多 {config.interaction.comment_max_length} 个字")
            x, y = await self._confirm_point(device, action, image, elements, config)
            # 核对画面要截图、读界面，这期间也可能被接管
            self._check_control(session, epoch)
            await device.tap(x, y)
            # 等键盘弹出来再写
            await self._sleep(_INPUT_FOCUS_SECONDS)
            self._check_control(session, epoch)
            await device.input_text(text)
            entered = await device.focused_input_text()
            if entered is not None and text not in entered:
                # 实测点到了旁边的表情按钮，打开的是应用自己的表情面板，字写不进去，她却以为写好了，点了二十多次发布
                raise ActionError(
                    "评论没写进输入框：要点写着「说点什么」之类的评论输入框（不是旁边的表情、@ 按钮），键盘弹出来才能写"
                )
            # 写进输入框就算一条：发没发出去看不出来，按多的算更保守
            self._store.add_interaction(self._today(), kind)
            await self._save_usage()
            session.interactions.append(f"评论「{text}」")
            return "已写好评论，还要点发送或发布才会发出去"
        if kind in ("tap", "long_press"):
            x, y = await self._confirm_point(device, action, image, elements, config)
            # 核对画面要截图、读界面，这期间也可能被接管
            self._check_control(session, epoch)
            if kind == "tap":
                await device.tap(x, y)
                return f"已点击 ({x}, {y})"
            await device.long_press(x, y, _LONG_PRESS_MS)
            return f"已长按 ({x}, {y})"
        if kind == "swipe":
            x1, y1, x2, y2 = swipe_points(action, width, height)
            await device.swipe(x1, y1, x2, y2, _SWIPE_DURATION_MS)
            return f"已从 ({x1}, {y1}) 滑到 ({x2}, {y2})"
        if kind == "type":
            if not config.safety.allow_text_input:
                raise ActionError("配置不允许输入文字")
            if len(action.text) > config.safety.max_text_length:
                raise ActionError(f"文字太长了，最多 {config.safety.max_text_length} 个字")
            await device.input_text(action.text)
            return "已输入"
        if kind in _KEY_ACTIONS:
            await device.press_key(_KEY_ACTIONS[kind])
            return "已按下"
        if kind == "launch":
            app = find_app(apps, action.app)
            if app is None:
                raise ActionError(f"「{action.app}」不在可以打开的应用里")
            await device.launch_app(app.package)
            self._enter_app(session, app)
            return f"已打开{app.name}"
        if kind == "wait":
            return f"看 {action.seconds:g} 秒"
        if kind == "wait_turn":
            if not session.together:
                raise ActionError("只有和对方一起玩时才需要等对方")
            return await self._wait_for_partner(device, session, config)
        return "结束这次玩手机"

    async def _wait_for_partner(self, device: PhoneDevice, session: Session, config: PhonePlayConfig) -> str:
        """等对方操作：只截图比对、不调用模型，直到画面变了、超时或时间到。

        Raises:
            DeviceError: 截图失败时抛出。
        """
        started = self._clock()
        image = await asyncio.to_thread(open_image, await device.screenshot())
        baseline = await asyncio.to_thread(screen_signature, image)
        while True:
            if self._clock() - started >= config.play.turn_wait_seconds:
                return f"等了 {config.play.turn_wait_seconds} 秒，对方还没动"
            if self._elapsed_seconds(session) >= session.max_minutes * 60:
                return "等对方的时候，这次的时间到了"
            if session.requests:
                return "对方在聊天里有新的要求"
            await self._sleep(_TURN_POLL_SECONDS)
            await self._resume.wait()
            image = await asyncio.to_thread(open_image, await device.screenshot())
            await self._publish_frame(image, config)
            signature = await asyncio.to_thread(screen_signature, image)
            if signature_distance(signature, baseline) > _PARTNER_CHANGE_DISTANCE:
                return "对方操作完了，画面变了"

    # ===== 自动玩 =====

    async def _auto_loop(self) -> None:
        while True:
            config = self._config()
            if not config.auto_play.enabled:
                # 配置更新时会重新启动调度
                self._next_auto_check = None
                return
            interval = config.auto_play.check_interval_minutes * 60
            self._next_auto_check = self._now() + timedelta(seconds=interval)
            await self._sleep(interval)
            try:
                await self._auto_decide()
            except Exception:
                self._logger.exception("自动玩手机检查失败")

    async def _auto_decide(self) -> None:
        config = self._config()
        if self._state != "idle" or self._controller == "user":
            return
        if not in_active_hours(config.auto_play.active_hours, self._now()):
            return
        if self._rng() >= config.auto_play.play_probability:
            return
        try:
            await self.start(trigger="auto")
        except PlayRefused as exc:
            self._logger.info(f"这次不自动玩手机：{exc}")
