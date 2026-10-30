"""玩手机的核心循环：决定玩什么 → 看屏幕 → 做决定 → 执行动作。"""

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from logging import Logger
from PIL import Image
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Sequence, Set, Tuple

import asyncio
import math
import random
import re
import time

from .actions import ActionError, PhoneAction, find_app, parse_action, resolve_point, swipe_points
from .devices import AppNotInstalledError, DeviceError, KeyName, PhoneDevice
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
    build_decision_messages,
    build_plan_prompt,
    parse_plan,
)
from .settings import AllowedApp, DeviceSettings, PhonePlayConfig
from .store import SessionRecord, UsageStore
from .ui_tree import UIElement, element_at, find_same

STATE_LABELS: Dict[str, str] = {"idle": "空闲", "starting": "准备中", "playing": "正在玩", "paused": "已暂停"}
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
_PIXEL_POINT = re.compile(r"\s*\(\d+, \d+\)")
# 结束时写的总结短于这么多字，就用她看的时候的想法补上
_SHORT_SUMMARY = 12
# 玩到一半手机断开时，每隔几秒试一次，最多等多久
_RECONNECT_INTERVAL = 5.0
_RECONNECT_SECONDS = 45.0
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

VisionCall = Callable[[List[Dict[str, Any]]], Awaitable[str]]
TextCall = Callable[[str], Awaitable[str]]
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


class AgentError(RuntimeError):
    """本次游玩无法继续。"""


class StaleTarget(Exception):
    """点之前画面变了，要点的东西已经不在原处：这一步不点，让她看了新画面再决定。"""


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
    ) -> None:
        self._config = config
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
        # 上一次玩手机是从哪个聊天发起的、什么时候结束的
        self._last_stream = ""
        self._last_ended_clock = 0.0

        self._device: Optional[PhoneDevice] = None
        self._device_key = ""
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

    def _log(
        self, step: int, summary: str, *, thought: str = "", action: str = "", ok: bool = True, result: str = ""
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
            )
        )

    def _get_device(self) -> PhoneDevice:
        """返回当前配置对应的设备；设备配置变化后重新创建。"""
        settings = self._config().device
        key = settings.model_dump_json()
        if self._device is None or key != self._device_key:
            if self._device is not None:
                task = asyncio.create_task(self._device.close())
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)
            self._device = self._device_factory(settings)
            self._device_key = key
        return self._device

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
            "app": session.app.name if session and session.app else "",
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
            "device": self._device.description if self._device else "",
            "auto_play": config.auto_play.enabled,
            "next_auto_check": next_check,
        }

    def activity_text(self) -> str:
        """给聊天中的 Bot 看的近况，方便她聊起手机上看到的东西。"""
        session = self._session
        if session is not None:
            if session.app is None:
                return "正在拿起手机，还在想玩什么。"
            recent = "；".join(_recent_unique(session.summaries, 5)) or "刚打开"
            prefix = "（暂停中）" if self._state == "paused" else ""
            text = (
                f"{prefix}正在用手机：{session.app.name}，想{session.goal}。"
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
        place = f"在{last.app}里" if last.app else ""
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
            limit = config.play.together_minutes if session.together else config.play.max_session_minutes
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
        """确认手机能用后，在后台开始一次玩手机。

        手机连不上、没有能玩的应用时直接拒绝，不算作玩了一次。

        Args:
            trigger: 触发来源，见 ``TRIGGER_LABELS``。
            hint: 想做的事；留空由 Bot 自己决定。
            origin_stream: 结束后要通知的聊天流，留空不通知。
            minutes: 指定玩多久，0 表示由 Bot 决定。
            together: 是否和对方在这台手机上一起玩（对方直接在手机上操作，她轮到对方时等着）。

        Raises:
            PlayRefused: 已经在玩、额度用完、手机连不上或没有能玩的应用时抛出。
        """
        async with self._start_lock:
            if self._state != "idle":
                goal = self._session.goal if self._session else ""
                raise PlayRefused(
                    f"已经在玩手机了（{goal or '正在准备'}）", chat_text=f"你已经在玩手机了（{goal or '还在想玩什么'}）"
                )
            config = self._config()
            self._check_limits(config)
            try:
                device = self._get_device()
                await device.ensure_ready()
                installed = await device.installed_packages([app.package for app in config.safety.allowed_apps])
            except DeviceError as exc:
                self._last_error = str(exc)
                self._logger.warning(f"手机连不上，这次没玩成：{exc}")
                raise PlayRefused(str(exc), chat_text="手机连不上，可能没开机或者没连好") from exc
            apps = [app for app in config.safety.allowed_apps if app.package in installed]
            if not apps:
                raise PlayRefused(
                    "白名单里的应用都没有安装在手机上，请先安装，或修改插件配置的 allowed_apps",
                    chat_text="手机上没有你能玩的应用",
                )

            session = Session(
                trigger=trigger,
                hint=hint.strip(),
                origin_stream=origin_stream,
                minutes_override=max(0, minutes),
                started_at=self._now(),
                started_clock=self._clock(),
                apps=apps,
                together=together,
            )
            self._session = session
            self._state = "starting"
            self._stop_reason = ""
            self._last_error = ""
            self._resume.set()
            self._logs.clear()
            self._log(0, f"拿起手机（{TRIGGER_LABELS[trigger]}）", result=session.hint)
            self._log(0, f"已连接 {device.description}")
            self._task = asyncio.create_task(self._run(session), name="phone_play_session")

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
        if self._session is not None:
            self._session.notify = notify
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
        session.paused_since = self._clock()
        self._resume.clear()
        self._state = "paused"
        self._log(session.step, "暂停")
        return True

    def resume(self) -> bool:
        session = self._session
        if self._state != "paused" or session is None or session.paused_since is None:
            return False
        session.paused_seconds += self._clock() - session.paused_since
        session.paused_since = None
        self._state = "playing"
        self._resume.set()
        self._log(session.step, "继续")
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
        if self._device is not None:
            await self._device.close()

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
                device = self._get_device()
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
        device = self._get_device()
        image = await asyncio.to_thread(open_image, await device.screenshot())
        await self._publish_frame(image, config)
        return await asyncio.to_thread(encode_jpeg, image, config.webui.chat_image_max_edge, _CHAT_JPEG_QUALITY)

    # ===== 会话 =====

    async def _run(self, session: Session) -> None:
        end_reason = ""
        try:
            await self._play(session)
            end_reason = session.end_reason
        except asyncio.CancelledError:
            end_reason = self._stop_reason or "被停止"
            raise
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
        if app is not None and self._config().play.go_home_after:
            try:
                await self._get_device().press_key("home")
            except DeviceError as exc:
                self._log(session.step, "没能回到桌面", ok=False, result=str(exc))
        self._state = "idle"
        self._session = None
        self._last_stream, self._last_ended_clock = session.origin_stream, self._clock()
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
            goal=session.goal,
            summary=summary,
            steps=session.step,
            end_reason=end_reason,
            together=session.together,
            interactions=_interaction_list(session),
        )
        self._store.add_record(record)
        try:
            await self._store.save()
            if session.origin_stream and session.notify:
                await self._notifier(record, session.origin_stream)
        except Exception:
            self._logger.exception("保存或通知玩手机结果失败")

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
        )
        response = await self._planner(prompt)
        try:
            plan = parse_plan(response, apps, max_minutes)
        except PlanDeclined:
            raise
        except ValueError as exc:
            raise AgentError(f"没能决定玩什么：{exc}") from exc
        if session.together:
            # 一起玩时什么时候结束主要看对方，给满一起玩的时长
            plan = Plan(app=plan.app, goal=plan.goal, minutes=min(session.minutes_override or max_minutes, max_minutes))
        elif session.minutes_override:
            plan = Plan(app=plan.app, goal=plan.goal, minutes=min(session.minutes_override, max_minutes))
        return plan

    async def _play(self, session: Session) -> None:
        config = self._config()
        device = self._get_device()
        apps = session.apps
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
            try:
                # 应用在后台时直接打开会回到上次停下的页面（比如上次看的视频），容易被带跑；
                # 一起玩时不关：上次停下的多半是和对方组好的房间，关掉就退出房间了
                if config.play.fresh_start and not session.together:
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
        session.planned.set()
        self._state = "playing"
        # 真正打开应用才算玩了一次
        self._store.add_session(self._today())
        await self._sleep(config.play.step_interval_seconds)

        history: Deque[str] = deque(maxlen=config.play.history_steps)
        consecutive_errors = 0
        while True:
            await self._resume.wait()
            config = self._config()
            reason = self._stop_check(session, config)
            if reason:
                session.end_reason = reason
                return
            try:
                ok, finished, wait_seconds = await self._step(
                    device, session, config, apps, home_packages, persona, history
                )
            except DeviceError as exc:
                # 实测 iPhone 一晚上会断开好几次（数据线松动、WDA 被重启），十几秒就恢复；等它恢复再接着玩，等不到才结束
                self._log(session.step, "手机断开了，等它恢复", ok=False, result=str(exc))
                if not await self._wait_for_device(device):
                    raise
                self._log(session.step, "手机恢复了，接着玩")
                history.append(f"第{session.step}步：手机断开了一会儿，刚恢复，先看看现在是什么画面")
                ok, finished, wait_seconds = False, False, 0.0
            if finished:
                return
            consecutive_errors = 0 if ok else consecutive_errors + 1
            if consecutive_errors >= config.play.max_consecutive_errors:
                session.end_reason = f"连续 {consecutive_errors} 步操作失败"
                self._last_error = session.end_reason
                return
            await self._nap(session, wait_seconds)

    async def _nap(self, session: Session, seconds: float) -> None:
        """等到下一步；对方在聊天里提了新要求就提前醒来。"""
        self._wake.clear()
        if session.requests:
            return
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waker.cancel()

    def _stop_check(self, session: Session, config: PhonePlayConfig) -> str:
        if session.step >= config.play.max_steps_per_session:
            return f"达到单次 {config.play.max_steps_per_session} 步的上限"
        if self._elapsed_seconds(session) >= session.max_minutes * 60:
            return f"玩够 {session.max_minutes} 分钟了"
        _, steps = self._store.usage(self._today())
        if steps >= config.play.daily_step_limit:
            return "今天的操作步数用完了"
        return ""

    async def _wait_for_device(self, device: PhoneDevice) -> bool:
        """手机短暂断开时每隔几秒试着连一次，恢复了返回 True，等太久还没恢复返回 False。"""
        waited = 0.0
        while waited < _RECONNECT_SECONDS:
            await self._sleep(_RECONNECT_INTERVAL)
            waited += _RECONNECT_INTERVAL
            try:
                await device.ensure_ready()
            except DeviceError:
                continue
            return True
        return False

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
            comments=left("comment", settings.allow_comment, settings.daily_comment_limit),
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
        self._store.add_step(self._today())
        interval = config.play.step_interval_seconds

        image = await asyncio.to_thread(open_image, await device.screenshot())
        await self._publish_frame(image, config)
        width, height = image.size
        signature = await asyncio.to_thread(screen_signature, image)
        if session.last_signature and signature_distance(signature, session.last_signature) < _UNCHANGED_DISTANCE:
            session.unchanged_steps += 1
        else:
            session.unchanged_steps = 0
        session.last_signature = signature

        # 安全检查：不管模型怎么想，停在白名单以外的应用里就直接回桌面
        package = await device.foreground_package()
        allowed = {app.package for app in apps} | home_packages | set(config.safety.extra_allowed_packages)
        if package and package not in allowed:
            await device.press_key("home")
            result = f"前台是 {package}，不在允许范围内，已回到桌面"
            self._log(step, "离开了不允许的应用", action="回桌面", ok=False, result=result)
            history.append(f"第{step}步：{result}")
            return False, False, interval

        elements: List[UIElement] = []
        elements_note = ""
        if config.play.use_ui_tree:
            try:
                elements = await device.ui_elements(width, height, config.play.max_elements)
            except DeviceError as exc:
                elements_note = f"这个画面读不到界面元素（{exc}），请直接用坐标。"
        model_image = image
        if elements and config.play.mark_elements:
            model_image = await asyncio.to_thread(draw_element_marks, image, elements)
        model_jpeg = await asyncio.to_thread(
            encode_jpeg, model_image, config.play.model_image_max_edge, _MODEL_JPEG_QUALITY
        )
        requests, session.requests = session.requests, []
        # 记进操作记录，后面几步也看得到对方说过什么
        history.extend(f"第{step}步前，对方在聊天里让你：{text}" for text in requests)
        context = DecisionContext(
            persona=persona,
            goal=session.goal,
            apps=apps,
            allow_text_input=config.safety.allow_text_input,
            ascii_text_only=config.device.backend == "adb" and config.device.text_input == "ascii_only",
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
            ios=config.device.backend == "ios_wda",
            interaction=self._interaction_quota(config),
            repeated_steps=session.repeat_count if session.repeat_count >= _REPEAT_WARNING else 0,
            repeated_action=session.last_action.describe() if session.last_action else "",
            requests=requests,
        )

        try:
            action = parse_action(await self._vision(build_decision_messages(context)))
        except (AgentError, ActionError) as exc:
            self._log(step, "没能决定下一步", ok=False, result=str(exc))
            history.append(f"第{step}步：没有得到有效动作（{exc}），请严格按格式只输出一个 JSON")
            return False, False, interval

        same = _same_action(session.last_action, action)
        session.repeat_count = session.repeat_count + 1 if same else int(action.kind in _REPEAT_KINDS)
        session.last_action = action

        if action.thought:
            session.last_thought = action.thought
        summary = action.summary or action.describe()
        try:
            result = await self._execute(device, action, image, elements, apps, config, session)
        except StaleTarget as exc:
            # 不是她做错了，不算连续出错；画面刚截过，不用再等
            self._log(step, summary, thought=action.thought, action=action.describe(), ok=False, result=f"没点：{exc}")
            history.append(f"第{step}步：{summary} → 没点：点之前画面变了，{exc}。先看看现在的画面再决定")
            return True, False, 0.0
        except (ActionError, DeviceError) as exc:
            self._log(step, summary, thought=action.thought, action=action.describe(), ok=False, result=str(exc))
            history.append(f"第{step}步：{summary} → 失败：{exc}")
            return False, False, interval

        self._log(step, summary, thought=action.thought, action=action.describe(), result=result)
        history.append(f"第{step}步：{summary}（{action.describe()}）→ {_history_result(result)}")
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
            return True, False, action.seconds
        if action.kind == "wait_turn":
            # 已经等到对方操作完了，画面是新的，不用再等
            return True, False, 0.0
        return True, False, interval

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
    ) -> str:
        """执行动作并返回结果说明；``image`` 是做决定时的截图。

        Raises:
            ActionError: 动作越过安全边界或参数无效时抛出。
            StaleTarget: 点之前画面变了，要点的东西不在原处。
            DeviceError: 设备执行失败时抛出。
        """
        kind = action.kind
        width, height = image.size
        if kind in ("like", "follow"):
            self._check_interaction(config, kind)
            x, y = await self._confirm_point(device, action, image, elements, config)
            await device.tap(x, y)
            self._store.add_interaction(self._today(), kind)
            if kind == "like":
                session.likes += 1
            else:
                session.interactions.append(f"关注了{action.summary.removeprefix('关注') or '一个博主'}")
            return f"已{_INTERACTION_LABELS[kind]} ({x}, {y})"
        if kind == "comment":
            self._check_interaction(config, kind)
            text = action.text.strip()
            if _COMMENT_FORBIDDEN.search(text):
                raise ActionError("评论里不能有链接、@ 别人或联系方式")
            if len(text) > config.interaction.comment_max_length:
                raise ActionError(f"评论太长了，最多 {config.interaction.comment_max_length} 个字")
            x, y = await self._confirm_point(device, action, image, elements, config)
            await device.tap(x, y)
            # 等键盘弹出来再写
            await self._sleep(_INPUT_FOCUS_SECONDS)
            await device.input_text(text)
            entered = await device.focused_input_text()
            if entered is not None and text not in entered:
                # 实测点到了旁边的表情按钮，打开的是应用自己的表情面板，字写不进去，她却以为写好了，点了二十多次发布
                raise ActionError(
                    "评论没写进输入框：要点写着「说点什么」之类的评论输入框（不是旁边的表情、@ 按钮），键盘弹出来才能写"
                )
            # 写进输入框就算一条：发没发出去看不出来，按多的算更保守
            self._store.add_interaction(self._today(), kind)
            session.interactions.append(f"评论「{text}」")
            return "已写好评论，还要点发送或发布才会发出去"
        if kind in ("tap", "long_press"):
            x, y = await self._confirm_point(device, action, image, elements, config)
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
        if self._state != "idle" or not in_active_hours(config.auto_play.active_hours, self._now()):
            return
        if self._rng() >= config.auto_play.play_probability:
            return
        try:
            await self.start(trigger="auto")
        except PlayRefused as exc:
            self._logger.info(f"这次不自动玩手机：{exc}")
