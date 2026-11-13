"""刷会儿手机：让 Bot 用安卓手机、模拟器或 iPhone 自己刷视频、逛帖子、听歌、看小说。"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Set, cast
from uuid import uuid4

import asyncio

from maibot_sdk import (
    API,
    Command,
    DesktopPetButton,
    DesktopPetButtonState,
    HookHandler,
    MaiBotPlugin,
    ON_BOT_CONFIG_RELOAD,
    Tool,
)
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .agent import AgentError, ModelReply, PhoneAgent, PlayRefused, SwitchRefused
from .devices import DeviceError, create_device
from .diagnostics import DiagnosticStore
from .memory import MemoryStore
from .imaging import jpeg_data_url, panel_png_base64, to_base64
from .life import (
    SHARE_API,
    SHARE_API_VERSION,
    SHARE_EXPIRES_MINUTES,
    SNAPSHOT_API,
    SNAPSHOT_API_VERSION,
    LifeActivity,
    LifeLinkError,
    parse_share_result,
    parse_snapshot,
    share_text,
)
from .prompts import Persona
from .report import CARD_WIDTH, report_html
from .settings import PhonePlayConfig
from .store import SessionRecord, UsageStore

_HELP_TEXT = (
    "📱 手机命令：\n"
    "/手机 — 查看状态\n"
    "/手机截图 — 发送当前手机画面\n"
    "/手机 开始 [想做的事] — 去玩手机，例如 /手机 开始 刷B站看猫猫；不写则自己决定\n"
    "/手机 暂停 · /手机 继续 · /手机 停止\n"
    "/手机 接管 — 你来操作手机，她马上停手、自动玩也不会抢；/手机 交还 — 交还给她，她看看现在的画面再接着玩\n"
    "/手机 设备 — 看有哪些设备、现在用哪台；/手机 切换 名字 — 换一台手机（比如 /手机 切换 OPPO），正在玩时不能换\n"
    "/手机 周报 — 最近 7 天玩了多久、玩了什么、看了什么，发一张图\n"
    "开始到切换这些控制命令要有权限：插件管理员能用，其他人在 MaiBot 后台「命令管理」里给 phone_control 放行\n"
    "实时观看：WebUI 侧边栏「插件扩展」里的「手机实时画面」"
)
# 看状态时最多这么久查一次麦生活的日程（它的接口只读、很便宜，但 WebUI 每隔几秒就要一次状态）
_LIFE_REFRESH_SECONDS = 60.0
# 桌宠「插件扩展」菜单里的按钮
_PET_BUTTON = "phone"
# 桌宠的按钮和面板回调限时 8 秒，截图最多等这么久，超时就用上一张
_PET_SCREENSHOT_SECONDS = 3.0
# 面板上的操作：ID → 按钮文字
_PET_ACTION_LABELS: Dict[str, str] = {
    "start": "开始玩",
    "pause": "暂停",
    "resume": "继续",
    "stop": "停止",
    "takeover": "我来操作",
    "release": "交还控制",
    "refresh": "刷新",
}
# 控制手机的子命令：交给宿主当受保护命令，谁能用在 MaiBot 后台「命令管理」里设（插件管理员、本地控制台默认能用）
_CONTROL_SUBS = "开始|停止|暂停|继续|接管|交还|切换"
# 谁都能用的 /手机、/手机截图、/手机 设备……；控制子命令归下面那个命令，这里用前瞻让开，两个命令不会同时匹配。
# /手机号码 这类普通消息不会被拦截
_COMMAND_PATTERN = (
    rf"^/手机(?!\s*(?:{_CONTROL_SUBS})(?:\s|$))\s*(?P<sub>截图|状态|设备|周报|帮助)?(?:\s+(?P<arg>.+?))?\s*$"
)
_CONTROL_PATTERN = rf"^/手机\s*(?P<sub>{_CONTROL_SUBS})(?:\s+(?P<arg>.+?))?\s*$"
# 聊天中叫她去玩手机时，等她决定好玩什么再回话，这样她能说出具体打算
_PLAN_WAIT_SECONDS = 30.0
# Maisaka 默认把插件工具放进 tool_search 后面，实测她要先搜一轮才会去玩，对方得催一句。
# 去玩、问近况、叫停这三个要立刻能用，所以一直放在她的工具列表里；看屏幕不常用，仍按需搜索
_ALWAYS_VISIBLE = "visible"


def _first_clause(text: str, limit: int) -> str:
    """取第一句（她的感想常用「；」连着好几句），去掉句尾标点，超长截断。桌宠面板一行只放得下十几个字。"""
    clause = text.split("；")[0].strip().rstrip("。．.！!？?，,、；; ")
    return clause if len(clause) <= limit else clause[: limit - 1] + "…"


def _pet_panel_lines(status: Dict[str, Any]) -> List[str]:
    """桌宠面板正文：大约三行，空闲时说上一次玩了什么，玩的时候说在玩什么、想做什么、在想什么。"""
    device = f" · {status['device_name']}" if len(status["devices"]) > 1 else ""
    playing = status["state"] in ("playing", "paused")
    if status["controller"] == "user":
        lines = [f"你在操作手机{device}，用完点「交还控制」"]
    elif playing:
        progress = f"{status['elapsed_minutes']:g}/{status['max_minutes']} 分钟"
        lines = [f"{status['state_label']}{device}：{status['app']} {progress}"]
    elif status["state"] == "starting":
        lines = [f"正在拿起手机{device}…"]
    else:
        lines = [f"空闲{device} · 今天玩了 {status['today']['sessions']} 次"]
    if playing:
        if status["goal"]:
            lines.append(f"想做：{_first_clause(status['goal'], 26)}")
        if status["thought"]:
            lines.append(f"💭 {_first_clause(status['thought'], 34)}")
    elif status["state"] == "idle" and status["controller"] == "bot" and status["history"]:
        last = status["history"][0]
        started = datetime.fromisoformat(last["started_at"])
        minutes = max(1, round((datetime.fromisoformat(last["ended_at"]) - started).total_seconds() / 60))
        when = f"{started:%H:%M}" if started.date() == datetime.now().date() else f"{started:%m-%d %H:%M}"
        lines.append(f"上次 {when} · {last['app']} · {minutes} 分钟")
        if last["summary"]:
            lines.append(f"💭 {_first_clause(last['summary'], 34)}")
    return lines


def _pet_actions(status: Dict[str, Any]) -> List[str]:
    """桌宠面板上按现在的状态能做的操作。"""
    if status["controller"] == "user":
        return ["release", "refresh"]
    if status["state"] == "idle":
        return ["start", "takeover", "refresh"]
    if status["state"] == "playing":
        return ["pause", "stop", "takeover", "refresh"]
    if status["state"] == "paused":
        return ["resume", "stop", "takeover", "refresh"]
    # 正在拿起手机（连手机、想玩什么）
    return ["stop", "refresh"]


def _finish_reason_text(end_reason: str) -> str:
    """把结束原因换成适合给她看的说法。"""
    if end_reason == "自己不想玩了":
        return "你玩够了自己放下的"
    if end_reason.startswith(("出错", "意外错误", "连续")):
        # 技术细节在 WebUI 的操作记录里，给她看只会被编成别的情况
        return "手机出了点状况，没法接着玩了"
    if end_reason.startswith(("玩够", "达到", "今天的操作步数", "这次的模型调用", "今天的模型调用")):
        return "玩到差不多该放下的时候了"
    if end_reason.startswith("卡住了"):
        return "在手机上卡住了，怎么点都没反应，就先放下了"
    if end_reason.startswith("暂停或接管"):
        return "中途停下来太久，这次就先不玩了"
    return "被叫停了"


class PhonePlayPlugin(MaiBotPlugin):
    """刷会儿手机插件。"""

    config_model = PhonePlayConfig
    config_reload_subscriptions = (ON_BOT_CONFIG_RELOAD,)
    desktop_pet_buttons = [DesktopPetButton(id=_PET_BUTTON, label="手机", icon="star")]

    def __init__(self) -> None:
        super().__init__()
        self._agent: Optional[PhoneAgent] = None
        self._diagnostics: Optional[DiagnosticStore] = None
        self._memory: Optional[MemoryStore] = None
        self._bot_nickname = ""
        # 桌宠面板上点了「停止」：收尾（回桌面、锁屏）可能超过回调的 8 秒，放到后台
        self._pet_tasks: Set[asyncio.Task[bool]] = set()

    @property
    def settings(self) -> PhonePlayConfig:
        return cast(PhonePlayConfig, self.config)

    @property
    def agent(self) -> PhoneAgent:
        if self._agent is None:
            raise RuntimeError("刷会儿手机插件尚未加载完成")
        return self._agent

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        """创建会话管理器并启动自动玩调度。"""
        store = UsageStore(self.ctx.paths.data_dir / "usage.json")
        await store.load()
        if store.recovered:
            self.ctx.logger.warning(store.recovered)
        # 失败步骤的诊断画面：只留最近 30 条、3 天内的，加载时顺手清掉过期的
        self._diagnostics = DiagnosticStore(self.ctx.paths.data_dir / "diagnostics")
        await self._diagnostics.prune()
        self._memory = MemoryStore(self.ctx.paths.data_dir / "memory.json")
        await self._memory.load()
        if self._memory.recovered:
            self.ctx.logger.warning(self._memory.recovered)
        self._agent = PhoneAgent(
            config=lambda: self.settings,
            device_factory=create_device,
            vision=self._call_vision,
            planner=self._call_planner,
            persona=self._load_persona,
            store=store,
            notifier=self._notify_finished,
            logger=self.ctx.logger,
            diagnostics=self._diagnostics,
            memory=self._memory,
            life=self._life_snapshot,
            life_share=self._share_with_life,
            announcer=self._pet_bubble,
            refresher=self._pet_refresh_buttons,
        )
        self._agent.restart_auto_play()

    async def on_unload(self) -> None:
        """停止正在进行的会话和自动玩调度。"""
        if self._agent is not None:
            await self._agent.shutdown()
            self._agent = None
        if self._pet_tasks:
            # 桌宠上点的「停止」和 shutdown 等的是同一个会话，这里只等它们收完
            await asyncio.wait(self._pet_tasks)

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """处理配置热重载：插件配置变化后重启自动玩调度，Bot 配置变化后刷新昵称。

        Args:
            scope: 配置变更范围。
            config_data: 最新配置数据。
            version: 配置版本号。
        """
        del config_data, version
        if scope == "bot":
            self._bot_nickname = ""
        elif scope == "self" and self._agent is not None:
            # 设备配置变化会在下次操作时自动重建设备连接
            self._agent.restart_auto_play()

    # ===== 宿主能力封装 =====

    async def _bot_name(self) -> str:
        if not self._bot_nickname:
            self._bot_nickname = str(await self.ctx.config.get("bot.nickname", "")).strip()
        return self._bot_nickname

    async def _load_persona(self) -> Persona:
        play = self.settings.play
        personality = ""
        if play.use_bot_personality:
            personality = str(await self.ctx.config.get("personality.personality", ""))
        return Persona(nickname=await self._bot_name(), personality=personality, extra=play.extra_persona)

    async def _call_llm(self, prompt: Any, task_name: str, model_name: str, label: str) -> ModelReply:
        # timeout_ms 由 SDK 的 call_capability 当作等宿主回应的 RPC 超时取走，不会转发给宿主的 llm.generate
        timeout_ms = self.settings.model.llm_timeout_seconds * 1000
        try:
            result = await self.ctx.llm.generate(
                prompt=prompt, task_name=task_name, model_name=model_name, timeout_ms=timeout_ms
            )
        except Exception as exc:
            # 宿主 RPC 超时或断开时抛出的异常不在 SDK 中导出，这里统一转成带原因的会话错误
            raise AgentError(f"{label}调用失败：{exc}") from exc
        if not result.get("success"):
            raise AgentError(f"{label}调用失败：{result.get('error') or '宿主没有返回原因'}")
        text = str(result.get("response") or "").strip()
        if not text:
            raise AgentError(f"{label}没有返回内容")
        # 宿主的 token 字段默认是 0：拿不到真实用量时为 0，按「未提供」处理，不估算
        total = int(result.get("total_tokens") or 0)
        if total <= 0:
            return ModelReply(text)
        return ModelReply(
            text,
            prompt_tokens=int(result.get("prompt_tokens") or 0),
            completion_tokens=int(result.get("completion_tokens") or 0),
            total_tokens=total,
        )

    async def _call_vision(self, messages: List[Dict[str, Any]]) -> ModelReply:
        model = self.settings.model
        return await self._call_llm(messages, model.vision_task, model.vision_model, "视觉模型")

    async def _call_planner(self, prompt: str) -> ModelReply:
        model = self.settings.model
        return await self._call_llm(prompt, model.planner_task, model.planner_model, "文本模型")

    async def _life_snapshot(self) -> Optional[LifeActivity]:
        """问麦生活（Mai_life）日程里现在这一段，见 life.py。"""
        try:
            response = await self.ctx.api.call(SNAPSHOT_API, version=SNAPSHOT_API_VERSION, future_limit=0)
        except Exception as exc:
            # 和 _call_llm 一样：宿主 RPC 超时或断开时抛出的异常不在 SDK 中导出，转成带原因的联动错误
            raise LifeLinkError(f"查麦生活的日程失败：{exc}") from exc
        return parse_snapshot(response)

    async def _share_with_life(self, record: SessionRecord) -> str:
        """把这次看到的交给麦生活的主动分享（create_proactive_opportunity），返回契机编号。"""
        topic, motive = share_text(record)
        try:
            response = await self.ctx.api.call(
                SHARE_API,
                version=SHARE_API_VERSION,
                topic=topic,
                motive=motive,
                weight=self.settings.life_link.share_weight,
                expires_minutes=SHARE_EXPIRES_MINUTES,
            )
        except Exception as exc:
            raise LifeLinkError(f"交给麦生活失败：{exc}") from exc
        return parse_share_result(response)

    async def _refresh_life(self) -> None:
        """看状态时顺便更新日程（一分钟内查过就不再查）；只有开着自动玩时日程才有用。"""
        if self.settings.auto_play.enabled:
            await self.agent.refresh_life(max_age=_LIFE_REFRESH_SECONDS)

    async def _notify_finished(self, record: SessionRecord, stream_id: str) -> None:
        """在发起的聊天里告知玩完了（只有真正打开过应用的那次才会通知）：默认请 Maisaka 让她自己决定说不说、怎么说。"""
        style = self.settings.play.finish_notice
        if style == "off":
            return
        interactions = "、".join(record.interactions)
        if style == "template":
            name = await self._bot_name()
            extra = f"\n互动：{interactions}" if interactions else ""
            await self.ctx.send.text(
                f"📱 {name}放下手机了：{record.places_text()} · {record.goal}\n"
                f"{record.summary}（{record.steps} 步，{record.end_reason}）{extra}",
                stream_id,
            )
            return

        place = f"和对方一起在{record.places_text()}里" if record.together else f"在{record.places_text()}里"
        did = f"用自己的账号{interactions}。" if interactions else ""
        intent = (
            f"你刚放下手机。这次{place}{record.goal}，{_finish_reason_text(record.end_reason)}。"
            f"玩的时候的感受：{record.summary}。{did}"
            "如果合适，就用你平时说话的语气在这里随口提一两句刚才在手机上看到的东西；不想说也可以不说。"
            "不要用通知、汇报的口吻，也不要提插件、步数这些。"
        )
        result = await self.ctx.maisaka.proactive.trigger(stream_id, intent, reason="玩手机结束")
        if not result.get("success"):
            raise RuntimeError(f"请她主动聊天失败：{result.get('error')}")

    # ===== 权限 =====
    # 开始、停止这些控制命令由宿主的命令权限管（phone_control 是受保护命令）；
    # admin_user_ids 只管截图和聊天里让她换手机：这两个不是控制命令，宿主管不到

    def _is_admin(self, user_id: str, is_local_operator: bool) -> bool:
        return is_local_operator or (bool(user_id) and user_id in self.settings.plugin.admin_user_ids)

    @staticmethod
    def _denied(user_id: str) -> str:
        return f"只有管理员可以截图。请在插件配置的 admin_user_ids 里添加你的账号（{user_id or '未知'}）"

    # ===== Command 组件 =====

    async def _status_text(self) -> str:
        await self._refresh_life()
        status = self.agent.status()
        name = await self._bot_name()
        lines = [f"📱 {name}的手机：{status['state_label']}"]
        if len(status["devices"]) > 1:
            lines.append(f"设备：{status['device_name']}（共 {len(status['devices'])} 台，/手机 设备 查看）")
        if status["state"] == "idle":
            lines.append(self.agent.activity_text())
        else:
            if status["app"]:
                lines.append(f"正在：{status['app']} · {status['goal']}")
            lines.append(
                f"第 {status['step']}/{status['max_steps']} 步 · "
                f"已玩 {status['elapsed_minutes']:g}/{status['max_minutes']} 分钟"
            )
            if status["thought"]:
                lines.append(f"💭 {status['thought']}")
        today = status["today"]
        lines.append(
            f"今天：{today['sessions']}/{today['session_limit']} 次 · {today['steps']}/{today['step_limit']} 步"
        )
        if status["auto_play"]:
            next_check = f"，下次考虑在 {status['next_auto_check']}" if status["next_auto_check"] else ""
            lines.append(f"自动玩：已开启{next_check}")
            if status["life"]:
                lines.append(f"生活联动：{status['life']['text']}")
        if status["last_error"]:
            lines.append(f"⚠ 最近的错误：{status['last_error']}")
        lines.append("发送 /手机 帮助 查看全部命令")
        return "\n".join(lines)

    async def _send_screenshot(self, stream_id: str) -> str:
        """发送截图，返回需要额外回复的文字（成功时为空或当前在做的事）。"""
        try:
            jpeg = await self.agent.chat_screenshot()
        except DeviceError as exc:
            return f"截图失败：{exc}"
        if not await self.ctx.send.image(to_base64(jpeg), stream_id):
            return "截图发送失败，请查看日志"
        status = self.agent.status()
        if status["state"] != "idle" and status["app"]:
            return f"正在{status['app']}：{status['goal']}（第 {status['step']} 步）"
        return ""

    async def _send_weekly_report(self, stream_id: str) -> str:
        """把最近 7 天的周报卡片交给宿主的浏览器渲染成图发出去，返回需要额外回复的文字（发出图时为空）。"""
        name = await self._bot_name()
        report = self.agent.weekly_report(name)
        if not report.sessions:
            return f"最近 7 天{name}没玩手机，没有周报可看"
        try:
            rendered = await self.ctx.render.html2png(
                report_html(report),
                selector=".card",
                viewport={"width": CARD_WIDTH + 80, "height": 900},
                device_scale_factor=2.0,
                omit_background=True,
            )
        except Exception as exc:
            # 和 _call_llm 一样：宿主 RPC 出错时抛出的异常不在 SDK 中导出；原因原样告诉对方，常见的是宿主没开浏览器渲染
            self.ctx.logger.warning(f"周报渲染失败：{exc}")
            return f"周报图没生成出来：{exc}（MaiBot 配置里「浏览器渲染」的「启用浏览器渲染」要开着）"
        if not await self.ctx.send.image(rendered["image_base64"], stream_id):
            return "周报图发送失败，请查看日志"
        return ""

    def _devices_text(self) -> str:
        lines = ["📱 设备："]
        for device in self.agent.device_list():
            mark = "（现在用的）" if device["current"] else ""
            lines.append(f"· {device['name']}{mark}：{device['kind']} · {device['address']}")
        if len(lines) == 2:
            lines.append("只配置了一台。要加设备，在插件配置的「多设备」里添加，每台写上 name")
        else:
            lines.append("用 /手机 切换 名字 换一台")
        return "\n".join(lines)

    async def _switch_text(self, arg: str) -> str:
        if not arg:
            return f"要换哪台？\n{self._devices_text()}"
        try:
            device, changed = await self.agent.switch_device(arg)
        except SwitchRefused as exc:
            return f"没换：{exc}"
        name = await self._bot_name()
        return f"好，{name}之后用{device}玩手机" if changed else f"现在用的就是{device}"

    async def _run_command(self, sub: str, arg: str, stream_id: str, user_id: str, is_local_operator: bool) -> str:
        """谁都能用的子命令：状态、帮助、设备；截图另看 screenshot_for_everyone 和 admin_user_ids。"""
        if not sub and arg:
            return f"不认识的手机命令：{arg}\n\n{_HELP_TEXT}"
        if sub == "帮助":
            return _HELP_TEXT
        if sub in ("", "状态"):
            return await self._status_text()
        if sub == "设备":
            return self._devices_text()
        if sub == "周报":
            return await self._send_weekly_report(stream_id)
        # 命令正则只允许上面这些子命令，剩下的就是「截图」
        if not (self._is_admin(user_id, is_local_operator) or self.settings.plugin.screenshot_for_everyone):
            return self._denied(user_id)
        return await self._send_screenshot(stream_id)

    async def _run_control(self, sub: str, arg: str, stream_id: str) -> str:
        """控制手机的子命令。谁能用由宿主判断：phone_control 是受保护命令，没权限的到不了这里。"""
        name = await self._bot_name()
        if sub == "开始":
            try:
                await self.agent.start(trigger="command", hint=arg, origin_stream=stream_id)
                plan = await self.agent.wait_for_plan(_PLAN_WAIT_SECONDS)
            except PlayRefused as exc:
                return f"现在不能玩手机：{exc}"
            if plan is None and self.agent.connecting:
                return f"手机暂时连不上，正在重试；连上了{name}会接着玩。WebUI 的「手机实时画面」能看到重连进度。"
            what = f"打开{plan.app.name}，{plan.goal}（大概 {plan.minutes} 分钟）" if plan else "还在想玩什么"
            return f"好，{name}去玩手机了：{what}。用 /手机截图 或 WebUI 的「手机实时画面」可以看她在做什么。"
        if sub == "停止":
            stopped = await self.agent.stop("被命令叫停")
            return f"已经让{name}放下手机了" if stopped else f"{name}现在没在玩手机"
        if sub == "暂停":
            return "已暂停，当前这一步做完后停下" if self.agent.pause() else "现在没有在玩，无法暂停"
        if sub == "接管":
            if not self.agent.takeover():
                return "你已经在操作手机了"
            return f"好，手机交给你：{name}马上停手，自动玩也不会抢回来。用完发 /手机 交还"
        if sub == "交还":
            if not self.agent.release():
                return f"手机本来就在{name}手里"
            return f"已交还给{name}" + ("，她看看现在的画面再接着玩" if self.agent.state != "idle" else "")
        if sub == "切换":
            return await self._switch_text(arg)
        # 命令正则只允许上面这些子命令，剩下的就是「继续」
        if self.agent.resume():
            return "继续玩"
        return "现在没有暂停"

    @Command("phone", description="手机：状态、截图、设备列表、帮助", pattern=_COMMAND_PATTERN)
    async def handle_phone_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        is_local_operator: bool = False,
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """处理谁都能用的 /手机 命令。"""
        del kwargs
        groups = matched_groups or {}
        sub = str(groups.get("sub") or "")
        arg = str(groups.get("arg") or "").strip()
        reply = await self._run_command(sub, arg, stream_id, user_id, is_local_operator)
        if reply:
            await self.ctx.send.text(reply, stream_id)
        return True, reply or "已发送图片", True

    @Command(
        "phone_control",
        description="手机控制：开始、停止、暂停、继续、接管、交还、切换设备",
        pattern=_CONTROL_PATTERN,
        permission="operator",
    )
    async def handle_phone_control(
        self, stream_id: str = "", matched_groups: Optional[Dict[str, Any]] = None, **kwargs: Any
    ):
        """处理 /手机 开始、停止这些控制命令；受保护命令，宿主先查过权限。"""
        del kwargs
        groups = matched_groups or {}
        reply = await self._run_control(str(groups.get("sub") or ""), str(groups.get("arg") or "").strip(), stream_id)
        await self.ctx.send.text(reply, stream_id)
        return True, reply, True

    # ===== Tool 组件 =====
    # 宿主只把 description 发给模型，detailed_description 已经不用了：用法都要写进 description。
    # 实测只写「拿起手机玩一会儿（刷视频、看番……）」时，对方叫她去玩手机上新装的斗地主，她嘴上说「那我试试」，
    # 没调用 play_phone，接着编了一段「一进去全是广告」

    @Tool(
        "play_phone",
        description=(
            "拿起手机玩一会儿：刷视频、看番、逛帖子、听歌、看小说、玩手机上的游戏。"
            "你自己想玩、无聊想打发时间，或者聊天里有人叫你去手机上玩点什么、看看某个内容时使用；"
            "调用了它才是真的拿起手机在玩，没调用就别说自己在玩、看到了什么。"
            "对方想和你一起玩（在这台手机上轮流操作，或者用自己的手机和你联机、组队）时，together 填 true。"
            "会等你想好玩什么（一般几秒）再返回，等结果出来再回对方、说说具体打算；玩不了时照实说原因。"
            "玩完后你会回到这个聊天。正在玩时再调用不会重新开始，而是把 goal 当成对方的新要求交给手机上的你"
            "（比如点准备、退出房间、接受邀请、换个视频）。对方要你换一台手机玩时，先用 switch_phone 换好再调用它。"
        ),
        parameters=[
            ToolParameterInfo(
                name="goal",
                param_type=ToolParamType.STRING,
                description="想在手机上做什么，例如「刷B站看猫猫视频」；留空则到手机上再决定",
                required=False,
            ),
            ToolParameterInfo(
                name="minutes",
                param_type=ToolParamType.INTEGER,
                description="打算玩几分钟；留空则自己决定",
                required=False,
            ),
            ToolParameterInfo(
                name="together",
                param_type=ToolParamType.BOOLEAN,
                description=(
                    "是否和对方一起玩：对方在这台手机上和你轮流操作，或者用自己的手机和你联机、组队（比如游戏里的好友房）"
                ),
                required=False,
            ),
        ],
        visibility=_ALWAYS_VISIBLE,
    )
    async def tool_play_phone(
        self, goal: str = "", minutes: int = 0, together: bool = False, stream_id: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """聊天中去玩手机；等她决定好玩什么再返回，方便她说出具体打算。"""
        del kwargs
        if self.agent.state != "idle":
            # 实测玩的时候对方在聊天里说「退出房间」「点准备」，她嘴上说做了，手机上的她根本不知道
            if self.agent.add_request(goal, minutes):
                return {
                    "success": True,
                    "content": (
                        f"已经告诉手机上的你：{goal.strip()}。手机上的你下一步会先去做；"
                        "做没做成要过一会儿用 phone_activity 看了再说，别直接说已经做好了。"
                    ),
                }
            return {"success": True, "content": f"你已经在玩手机了。{self.agent.activity_text()}"}
        if not self.settings.play.allow_chat_trigger:
            return {"success": False, "error": "插件配置不允许在聊天中自己去玩手机"}
        try:
            await self.agent.start(
                trigger="chat", hint=goal, origin_stream=stream_id, minutes=minutes, together=together
            )
            plan = await self.agent.wait_for_plan(_PLAN_WAIT_SECONDS)
        except PlayRefused as exc:
            return {"success": False, "error": f"这次玩不了手机：{exc.chat_text}"}
        if plan is None:
            if self.agent.connecting:
                return {"success": True, "content": "手机暂时连不上，正在重试，连上了会接着玩；别说已经在玩了。"}
            return {"success": True, "content": "你拿起了手机，还在想玩什么。"}
        if together:
            # 实测聊天这边不知道手机上玩到哪了，会自己编「该你了」这类进展，和手机上的实际情况对不上
            content = (
                f"你拿起了手机，打算打开{plan.app.name}：{plan.goal}。"
                "对方在这台手机上和你轮流操作时，轮到对方你会等着；对方用自己的手机联机时，你会进房间等对方、一起开局。"
                "可以告诉对方你准备好了。"
                "每一步都是你在手机上看着画面自己操作的，聊天里不用报坐标，也不要编造进展；"
                "对方问起局面时用 phone_activity 看看现在的情况再说。玩完你会回到这个聊天。"
            )
        else:
            content = f"你拿起了手机，打算打开{plan.app.name}：{plan.goal}（大概玩 {plan.minutes} 分钟）。"
        return {"success": True, "content": content}

    @Tool(
        "switch_phone",
        description=(
            "换一台手机玩（你有不止一台手机时）。对方让你换手机、用另一台玩（比如「换安卓手机」「用 iPhone 玩」"
            "「切到 OPPO」）时使用；换好后之后玩手机都用这台。只有管理员能让你换，正在玩时换不了；"
            "换完要接着玩就再调用 play_phone。没换成时照实说原因。"
        ),
        parameters=[
            ToolParameterInfo(
                name="device",
                param_type=ToolParamType.STRING,
                description="换成哪台：设备名字，或者「安卓」「iPhone」这样的说法；留空则返回有哪些设备",
                required=False,
            )
        ],
    )
    async def tool_switch_phone(self, device: str = "", user_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        """在聊天中换一台手机；和 /手机 切换 一样只认管理员。"""
        del kwargs
        names = "、".join(
            f"{item['name']}（{item['kind']}{'，现在用的' if item['current'] else ''}）"
            for item in self.agent.device_list()
        )
        if not device.strip():
            return {"success": True, "content": f"你的手机有：{names}"}
        # user_id 是宿主按会话注入的；这个工具的参数里没有 user_id，模型一般不会自己去填
        if not self._is_admin(user_id, False):
            return {"success": False, "error": "只有管理员能让你换手机，婉拒就好，照常用现在这台"}
        try:
            name, changed = await self.agent.switch_device(device)
        except SwitchRefused as exc:
            return {"success": False, "error": f"没换成：{exc}"}
        if not changed:
            return {"success": True, "content": f"现在用的就是{name}，不用换。"}
        return {"success": True, "content": f"换好了，之后玩手机用{name}。要接着玩就调用 play_phone。"}

    @Tool(
        "remember_for_phone",
        description=(
            "记住对方亲口说的、和你玩手机有关的兴趣或要求，下次玩手机时会想起来。"
            "比如对方说「我喜欢看种田小说」「下次接着看那本《XX》」时使用；"
            "只记对方明确说的话，不要记你自己猜的，也不要记手机画面上看到的（那些手机上的你会自己记）。"
        ),
        parameters=[
            ToolParameterInfo(
                name="note",
                param_type=ToolParamType.STRING,
                description="对方说的兴趣或要求，用一句话写清楚，例如「对方喜欢看种田小说」",
                required=True,
            )
        ],
    )
    async def tool_remember_for_phone(self, note: str = "", **kwargs: Any) -> Dict[str, Any]:
        """把对方亲口说的兴趣记进内容记忆，来源标成「对方说过」。"""
        del kwargs
        if self._memory is None:
            return {"success": False, "error": "插件还没加载完"}
        if not note.strip():
            return {"success": False, "error": "没有要记的内容"}
        item = self._memory.remember_interest(note, datetime.now())
        await self._memory.save()
        return {"success": True, "content": f"记住了：{item.title}。下次玩手机时会想起来。"}

    @Tool(
        "show_phone_screen",
        description=(
            "截一张手机屏幕，可以附在回复里给对方看。聊天里有人想看你在手机上看什么、让你截图给他看时使用。"
            "截图先交给你，对方这时还看不到；要给对方看，回复时用 attach_pic 附上它，"
            "再照着画面上真有的内容说一两句（截到的是桌面就是还没打开应用）。"
        ),
    )
    async def tool_show_phone_screen(self, user_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        """截图交给她，由她在回复里附上图片，图和话一起发出去，也不会重复发图。"""
        del kwargs
        # user_id 是宿主按会话注入的；但模型若自己传了同名参数，宿主会以模型的为准。这个工具的参数里没有 user_id，
        # 模型一般不会去填，这里只能算辅助校验；截图里可能有通知等隐私，
        # 真正在意时保持 screenshot_for_everyone 关闭并少加管理员
        if not (self._is_admin(user_id, False) or self.settings.plugin.screenshot_for_everyone):
            return {"success": False, "error": "对方不在可以看你手机的名单里，婉拒就好"}
        try:
            jpeg = await self.agent.chat_screenshot()
        except DeviceError as exc:
            self.ctx.logger.warning(f"手机截图失败：{exc}")
            return {"success": False, "error": "手机连不上，截不了图"}
        return {
            "success": True,
            "content": (
                "这是你手机现在的屏幕截图，对方还看不到；要给对方看，回复时用 attach_pic 附上它。"
                f"{self.agent.activity_text()}"
            ),
            "content_items": [
                {
                    "content_type": "image",
                    "data": to_base64(jpeg),
                    "mime_type": "image/jpeg",
                    "description": "你手机当前的屏幕",
                }
            ],
        }

    @Tool(
        "phone_activity",
        description=(
            "看看自己正在或刚才在手机上做了什么。聊天里有人问你在玩什么、玩上了没有、刚才在手机上看到了什么，"
            "或者你想聊聊手机上看到的内容时使用；聊手机上的事以它返回的为准，别自己编。"
        ),
        visibility=_ALWAYS_VISIBLE,
    )
    async def tool_phone_activity(self, **kwargs: Any) -> Dict[str, Any]:
        """查询玩手机近况。"""
        del kwargs
        return {"success": True, "content": self.agent.activity_text()}

    @Tool(
        "stop_phone",
        description="放下手机，不玩了。有人让你别玩手机了、叫你过来聊天，或者你自己不想玩了时使用。",
        visibility=_ALWAYS_VISIBLE,
    )
    async def tool_stop_phone(self, **kwargs: Any) -> Dict[str, Any]:
        """在聊天中放下手机。"""
        del kwargs
        # 她正在聊天里接这句话，不再额外发结束通知
        stopped = await self.agent.stop("在聊天中放下了手机", notify=False)
        if not stopped:
            return {"success": True, "content": "你现在本来就没在玩手机。"}
        return {"success": True, "content": f"你放下了手机。{self.agent.activity_text()}"}

    # ===== Hook 组件 =====
    # 玩手机时把手机上的实际情况放进发起那个聊天的每一轮，她聊手机上的事才有依据（见 PhoneAgent.chat_context）

    @HookHandler(
        "maisaka.planner.before_request",
        name="phone_play_planner_context",
        description="玩手机时把手机上的实际情况告诉聊天里的她",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_planner_context(self, **kwargs: Any) -> Dict[str, Any]:
        """追加在请求最后，不改前面的消息，聊天记录的提示词缓存照样命中。"""
        items = kwargs.get("items")
        text = self._agent.chat_context(str(kwargs.get("session_id") or "")) if self._agent else ""
        if not text or not isinstance(items, list):
            return {"action": "continue"}
        note = {
            "item_type": "UserMessageItem",
            "meta": {
                "item_id": f"phone-play-{uuid4().hex}",
                "logical_turn_id": None,
                "timestamp": datetime.now().isoformat(),
            },
            "parts": [{"type": "text", "text": text}],
        }
        # 宿主用 modified_kwargs 整体替换参数，要带上全部原有参数
        return {"action": "continue", "modified_kwargs": {**kwargs, "items": [*items, note]}}

    @HookHandler(
        "maisaka.replyer.before_request",
        name="phone_play_replyer_context",
        description="玩手机时让写回复的她也知道手机上的实际情况",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_replyer_context(self, **kwargs: Any) -> Dict[str, Any]:
        """规划时只定下回复的方向，具体说什么由 replyer 写，实测「点到了，已经退出来了」这类话就是这一步编的。"""
        extra = kwargs.get("extra_prompt")
        text = self._agent.chat_context(str(kwargs.get("session_id") or "")) if self._agent else ""
        if not text or not isinstance(extra, str):
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": {**kwargs, "extra_prompt": f"{extra}\n{text}".strip()}}

    # ===== 桌宠 =====
    # 宿主内置桌宠（MaiBot 1.4）：「插件扩展」菜单里的「手机」按钮、点开后的面板，以及玩手机时冒的气泡

    async def _pet_bubble(self, text: str, event: str = "") -> None:
        """让桌宠做个动作、冒一句气泡（两者都可以为空）；桌宠没就绪时跳过。

        开关和间隔由 agent 判断，出错由 agent 记日志。先等动作播完再冒气泡：气泡一出来，宿主就把正在播的动作打断了。
        """
        state = await self.ctx.desktop_pet.status()
        if not state.get("ready"):
            return
        if event and state.get("visible"):
            await self._pet_play(event)
        if text:
            await self.ctx.desktop_pet.say(text)

    async def _pet_play(self, event: str) -> None:
        """播一个标准动作并等它播完。

        先查当前皮肤有没有这个动作，没有就不播：宿主会把「皮肤没有这个动作」当成能力异常，记一条带堆栈的错误。
        """
        animations = await self.ctx.desktop_pet.list_animations()
        if not animations.get("events", {}).get(event):
            return
        playback = await self.ctx.desktop_pet.play_event(event)
        await asyncio.sleep(playback["duration_ms"] / 1000)

    async def _pet_refresh_buttons(self) -> None:
        """她的状态变了，让宿主重新读一次按钮上的字；按钮关掉了就不打扰宿主。

        宿主只是排一次刷新，桌宠没开时也不会出错；面板本身不会跟着变，要重新点按钮。
        """
        if self.settings.desktop_pet.button:
            await self.ctx.desktop_pet.refresh_buttons()

    def _pet_button_text(self) -> str:
        """按钮上的一行状态，打开桌宠的扩展菜单时刷新。"""
        status = self.agent.status()
        if status["controller"] == "user":
            return "你在操作手机"
        if status["state"] in ("playing", "paused"):
            prefix = "暂停中" if status["state"] == "paused" else "正在玩"
            return f"{prefix}：{status['app']} {status['elapsed_minutes']:g}/{status['max_minutes']} 分钟"
        if status["state"] == "starting":
            return "正在拿起手机"
        return f"空闲 · 今天玩了 {status['today']['sessions']} 次"

    async def get_desktop_pet_button_states(self) -> Dict[str, DesktopPetButtonState]:
        visible = self.settings.desktop_pet.button and self._agent is not None
        return {_PET_BUTTON: DesktopPetButtonState(visible=visible, text=self._pet_button_text() if visible else "")}

    async def on_desktop_pet_button_click(self, button_id: str) -> Dict[str, Any]:
        return await self.get_desktop_pet_panel(button_id)

    async def get_desktop_pet_panel(self, button_id: str, notice: str = "") -> Dict[str, Any]:
        """桌宠里的面板：她在玩什么、此刻的想法、手机截图，以及按现在的状态能做的操作。

        宿主每次执行面板操作前也会调一次这里来核对操作还在不在，所以截图复用最短刷新间隔内的那张。
        """
        if button_id != _PET_BUTTON:
            raise ValueError(f"没有这个桌宠按钮：{button_id}")
        agent = self.agent
        try:
            await asyncio.wait_for(agent.refresh_frame(), _PET_SCREENSHOT_SECONDS)
            stale = False
        except TimeoutError:
            stale = True
        jpeg, frame_error = agent.latest_frame()
        image = await asyncio.to_thread(panel_png_base64, jpeg) if jpeg is not None else None
        status = agent.status()
        lines = [notice] if notice else []
        lines.extend(_pet_panel_lines(status))
        # 有问题时才多一行；生活联动、步数这些细节在 WebUI 里看
        connection = status["connection"]
        if connection["state"] not in ("ok", "unknown"):
            lines.append(f"⚠ {connection['label']}")
        if stale:
            lines.append("⚠ 截图超时，这是上一张")
        elif frame_error:
            lines.append(f"⚠ 截图失败：{_first_clause(frame_error, 24)}")
        name = await self._bot_name()
        return {
            "title": f"{name or '她'}的手机"[:64],
            "text": "\n".join(lines)[:4000],
            "image": image,
            "actions": [{"id": action, "label": _PET_ACTION_LABELS[action]} for action in _pet_actions(status)],
        }

    async def on_desktop_pet_panel_action(self, button_id: str, action_id: str) -> Dict[str, Any]:
        """面板上的操作，做完返回更新后的面板，最上面一行说明做了什么。"""
        if button_id != _PET_BUTTON:
            raise ValueError(f"没有这个桌宠按钮：{button_id}")
        agent = self.agent
        notice = ""
        if action_id == "start":
            try:
                await agent.start(trigger="pet")
                notice = "她拿起了手机，正在想玩什么"
            except PlayRefused as exc:
                notice = f"没开始：{exc}"
        elif action_id == "pause":
            notice = "暂停了：这一步做完就停下" if agent.pause() else "现在没在玩，不用暂停"
        elif action_id == "resume":
            notice = "接着玩了" if agent.resume() else "现在没有暂停"
        elif action_id == "stop":
            # 收尾要回桌面、锁屏，可能超过回调的 8 秒：放到后台，面板先说一声
            task = asyncio.create_task(agent.stop("在桌宠上停止"), name="phone_play_pet_stop")
            self._pet_tasks.add(task)
            task.add_done_callback(self._pet_task_done)
            notice = "她正在放下手机"
        elif action_id == "takeover":
            agent.takeover()
            notice = "手机交给你了，她不会再操作；用完点「交还控制」"
        elif action_id == "release":
            agent.release()
            notice = "交还给她了，她会先看看现在的画面再接着玩"
        elif action_id != "refresh":
            raise ValueError(f"没有这个操作：{action_id}")
        return await self.get_desktop_pet_panel(button_id, notice)

    def _pet_task_done(self, task: "asyncio.Task[bool]") -> None:
        self._pet_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.ctx.logger.error(f"桌宠上停止玩手机失败：{task.exception()}")

    # ===== WebUI 页面 API =====

    @API("webui_status", description="WebUI：手机状态与操作记录", version="1")
    async def webui_status(self) -> Dict[str, Any]:
        await self._refresh_life()
        status = self.agent.status()
        status["nickname"] = await self._bot_name()
        return status

    @API("webui_frame", description="WebUI：手机实时画面", version="1")
    async def webui_frame(self, since: int = 0) -> Dict[str, Any]:
        await self.agent.refresh_frame()
        return self.agent.frame_payload(since)

    @API("webui_start", description="WebUI：开始玩手机", version="1")
    async def webui_start(self, goal: str = "") -> Dict[str, Any]:
        try:
            await self.agent.start(trigger="webui", hint=goal)
        except PlayRefused as exc:
            raise ValueError(f"现在不能玩手机：{exc}") from exc
        return {"started": True}

    @API("webui_stop", description="WebUI：停止玩手机", version="1")
    async def webui_stop(self) -> Dict[str, Any]:
        return {"stopped": await self.agent.stop("在 WebUI 中停止")}

    @API("webui_pause", description="WebUI：暂停玩手机", version="1")
    async def webui_pause(self) -> Dict[str, Any]:
        if not self.agent.pause():
            raise ValueError("现在没有在玩，无法暂停")
        return {"paused": True}

    @API("webui_diagnostic", description="WebUI：查看一条失败诊断", version="1")
    async def webui_diagnostic(self, id: str = "") -> Dict[str, Any]:
        store = self._diagnostics
        if store is None:
            raise ValueError("插件还没加载完")
        entry = next((item for item in await store.entries() if item["id"] == id), None)
        image = await store.image(id) if entry else None
        if entry is None or image is None:
            raise ValueError("这条诊断已经过期或被清理了")
        return {"entry": entry, "image": jpeg_data_url(image)}

    @API("webui_clear_diagnostics", description="WebUI：清理失败诊断", version="1")
    async def webui_clear_diagnostics(self) -> Dict[str, Any]:
        if self._diagnostics is None:
            raise ValueError("插件还没加载完")
        return {"cleared": await self._diagnostics.clear()}

    @API("webui_memory", description="WebUI：查看内容记忆", version="1")
    async def webui_memory(self) -> Dict[str, Any]:
        if self._memory is None:
            raise ValueError("插件还没加载完")
        return {"items": [item.to_dict() for item in self._memory.items()]}

    @API("webui_memory_delete", description="WebUI：删除一条内容记忆", version="1")
    async def webui_memory_delete(self, id: str = "") -> Dict[str, Any]:
        if self._memory is None:
            raise ValueError("插件还没加载完")
        if not self._memory.delete(id):
            raise ValueError("这条记忆已经不在了")
        await self._memory.save()
        return {"deleted": id}

    @API("webui_memory_clear", description="WebUI：清空内容记忆", version="1")
    async def webui_memory_clear(self, kind: str = "") -> Dict[str, Any]:
        """全部删掉；``kind`` 是 watched 时只清空看过的视频。"""
        if self._memory is None:
            raise ValueError("插件还没加载完")
        if kind not in ("", "watched"):
            raise ValueError(f"不认识的记忆类型：{kind}")
        cleared = self._memory.clear(kind)
        await self._memory.save()
        return {"cleared": cleared}

    @API("webui_switch_device", description="WebUI：换一台设备", version="1")
    async def webui_switch_device(self, name: str = "") -> Dict[str, Any]:
        try:
            device, changed = await self.agent.switch_device(name)
        except SwitchRefused as exc:
            raise ValueError(f"没换：{exc}") from exc
        return {"device": device, "changed": changed}

    @API("webui_takeover", description="WebUI：人工接管手机", version="1")
    async def webui_takeover(self) -> Dict[str, Any]:
        if not self.agent.takeover():
            raise ValueError("你已经在操作手机了")
        return {"controller": "user"}

    @API("webui_release", description="WebUI：交还手机控制", version="1")
    async def webui_release(self) -> Dict[str, Any]:
        if not self.agent.release():
            raise ValueError("手机本来就在她手里")
        return {"controller": "bot"}

    @API("webui_resume", description="WebUI：继续玩手机", version="1")
    async def webui_resume(self) -> Dict[str, Any]:
        if not self.agent.resume():
            raise ValueError("现在没有暂停")
        return {"resumed": True}


def create_plugin() -> PhonePlayPlugin:
    """创建刷会儿手机插件实例。

    Returns:
        PhonePlayPlugin: 新的插件实例。
    """

    return PhonePlayPlugin()
