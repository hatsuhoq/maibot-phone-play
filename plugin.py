"""刷会儿手机：让 Bot 用安卓手机、模拟器或 iPhone 自己刷视频、逛帖子、听歌、看小说。"""

from datetime import datetime
from typing import Any, Dict, List, Optional, cast
from uuid import uuid4

from maibot_sdk import API, Command, HookHandler, MaiBotPlugin, ON_BOT_CONFIG_RELOAD, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .agent import AgentError, PhoneAgent, PlayRefused
from .devices import DeviceError, create_device
from .imaging import to_base64
from .prompts import Persona
from .settings import PhonePlayConfig
from .store import SessionRecord, UsageStore

_HELP_TEXT = (
    "📱 手机命令：\n"
    "/手机 — 查看状态\n"
    "/手机截图 — 发送当前手机画面\n"
    "/手机 开始 [想做的事] — 去玩手机，例如 /手机 开始 刷B站看猫猫；不写则自己决定\n"
    "/手机 暂停 · /手机 继续 · /手机 停止\n"
    "实时观看：WebUI 侧边栏「插件扩展」里的「手机实时画面」"
)
# /手机、/手机截图、/手机 开始 刷B站 都能匹配；/手机号码 这类普通消息不会被拦截
_COMMAND_PATTERN = r"^/手机\s*(?P<sub>截图|状态|开始|停止|暂停|继续|帮助)?(?:\s+(?P<arg>.+?))?\s*$"
# 聊天中叫她去玩手机时，等她决定好玩什么再回话，这样她能说出具体打算
_PLAN_WAIT_SECONDS = 30.0
# Maisaka 默认把插件工具放进 tool_search 后面，实测她要先搜一轮才会去玩，对方得催一句。
# 去玩、问近况、叫停这三个要立刻能用，所以一直放在她的工具列表里；看屏幕不常用，仍按需搜索
_ALWAYS_VISIBLE = "visible"


def _finish_reason_text(end_reason: str) -> str:
    """把结束原因换成适合给她看的说法。"""
    if end_reason == "自己不想玩了":
        return "你玩够了自己放下的"
    if end_reason.startswith(("出错", "意外错误", "连续")):
        # 技术细节在 WebUI 的操作记录里，给她看只会被编成别的情况
        return "手机出了点状况，没法接着玩了"
    if end_reason.startswith(("玩够", "达到", "今天的操作步数")):
        return "玩到差不多该放下的时候了"
    return "被叫停了"


class PhonePlayPlugin(MaiBotPlugin):
    """刷会儿手机插件。"""

    config_model = PhonePlayConfig
    config_reload_subscriptions = (ON_BOT_CONFIG_RELOAD,)

    def __init__(self) -> None:
        super().__init__()
        self._agent: Optional[PhoneAgent] = None
        self._bot_nickname = ""

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
        self._agent = PhoneAgent(
            config=lambda: self.settings,
            device_factory=create_device,
            vision=self._call_vision,
            planner=self._call_planner,
            persona=self._load_persona,
            store=store,
            notifier=self._notify_finished,
            logger=self.ctx.logger,
        )
        self._agent.restart_auto_play()

    async def on_unload(self) -> None:
        """停止正在进行的会话和自动玩调度。"""
        if self._agent is not None:
            await self._agent.shutdown()
            self._agent = None

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

    async def _call_llm(self, prompt: Any, task_name: str, model_name: str, label: str) -> str:
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
        return text

    async def _call_vision(self, messages: List[Dict[str, Any]]) -> str:
        model = self.settings.model
        return await self._call_llm(messages, model.vision_task, model.vision_model, "视觉模型")

    async def _call_planner(self, prompt: str) -> str:
        model = self.settings.model
        return await self._call_llm(prompt, model.planner_task, model.planner_model, "文本模型")

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
                f"📱 {name}放下手机了：{record.app} · {record.goal}\n"
                f"{record.summary}（{record.steps} 步，{record.end_reason}）{extra}",
                stream_id,
            )
            return

        place = f"和对方一起在{record.app}里" if record.together else f"在{record.app}里"
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

    def _is_admin(self, user_id: str, is_local_operator: bool) -> bool:
        return is_local_operator or (bool(user_id) and user_id in self.settings.plugin.admin_user_ids)

    @staticmethod
    def _denied(user_id: str) -> str:
        return f"只有管理员可以使用这个命令。请在插件配置的 admin_user_ids 里添加你的账号（{user_id or '未知'}）"

    # ===== Command 组件 =====

    async def _status_text(self) -> str:
        status = self.agent.status()
        name = await self._bot_name()
        lines = [f"📱 {name}的手机：{status['state_label']}"]
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

    async def _run_command(self, sub: str, arg: str, stream_id: str, user_id: str, is_local_operator: bool) -> str:
        if not sub and arg:
            return f"不认识的手机命令：{arg}\n\n{_HELP_TEXT}"
        if sub == "帮助":
            return _HELP_TEXT
        if sub in ("", "状态"):
            return await self._status_text()

        is_admin = self._is_admin(user_id, is_local_operator)
        if sub == "截图":
            if not (is_admin or self.settings.plugin.screenshot_for_everyone):
                return self._denied(user_id)
            return await self._send_screenshot(stream_id)

        if not is_admin:
            return self._denied(user_id)
        name = await self._bot_name()
        if sub == "开始":
            try:
                await self.agent.start(trigger="command", hint=arg, origin_stream=stream_id)
                plan = await self.agent.wait_for_plan(_PLAN_WAIT_SECONDS)
            except PlayRefused as exc:
                return f"现在不能玩手机：{exc}"
            what = f"打开{plan.app.name}，{plan.goal}（大概 {plan.minutes} 分钟）" if plan else "还在想玩什么"
            return f"好，{name}去玩手机了：{what}。用 /手机截图 或 WebUI 的「手机实时画面」可以看她在做什么。"
        if sub == "停止":
            stopped = await self.agent.stop("被命令叫停")
            return f"已经让{name}放下手机了" if stopped else f"{name}现在没在玩手机"
        if sub == "暂停":
            return "已暂停，当前这一步做完后停下" if self.agent.pause() else "现在没有在玩，无法暂停"
        # 命令正则只允许上面这些子命令，剩下的就是「继续」
        if self.agent.resume():
            return "继续玩"
        return "现在没有暂停"

    @Command("phone", description="手机：截图、状态、开始、停止、暂停、继续", pattern=_COMMAND_PATTERN)
    async def handle_phone_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        is_local_operator: bool = False,
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """处理 /手机 系列命令。"""
        del kwargs
        groups = matched_groups or {}
        sub = str(groups.get("sub") or "")
        arg = str(groups.get("arg") or "").strip()
        reply = await self._run_command(sub, arg, stream_id, user_id, is_local_operator)
        if reply:
            await self.ctx.send.text(reply, stream_id)
        return True, reply or "已发送手机截图", True

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
            "（比如点准备、退出房间、接受邀请、换个视频）。"
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

    # ===== WebUI 页面 API =====

    @API("webui_status", description="WebUI：手机状态与操作记录", version="1")
    async def webui_status(self) -> Dict[str, Any]:
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
