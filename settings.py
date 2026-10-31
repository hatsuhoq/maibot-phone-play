"""刷会儿手机插件配置模型。"""

from typing import List, Literal

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator

import re

_HOURS_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


class PluginSettings(PluginConfigBase):
    """插件基础设置与权限。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.1.0", description="配置版本")
    admin_user_ids: List[str] = Field(
        default_factory=list,
        description="可以使用 /手机 开始、停止 等控制命令的账号；主程序本地控制台始终允许",
    )
    screenshot_for_everyone: bool = Field(
        default=False,
        description="是否允许所有人使用 /手机截图；关闭时只有管理员可用（截图里可能带有通知等隐私）",
    )


class DeviceSettings(PluginConfigBase):
    """被控制的手机。"""

    __ui_label__ = "设备"
    __ui_icon__ = "smartphone"
    __ui_order__ = 1

    backend: Literal["adb", "ios_wda"] = Field(
        default="adb",
        description="设备类型：adb=安卓真机或模拟器；ios_wda=iPhone（通过 WebDriverAgent 控制）",
    )
    adb_path: str = Field(default="adb", description="adb 可执行文件路径；已加入 PATH 时填 adb 即可")
    serial: str = Field(
        default="",
        description="设备序列号或 IP:端口（如 MuMu 的 127.0.0.1:16384）；留空时使用唯一已连接的设备。"
        "只填自己的设备，别指向不可信的主机",
    )
    auto_connect: bool = Field(default=True, description="序列号是 IP:端口 时，自动执行 adb connect")
    wda_url: str = Field(
        default="http://127.0.0.1:8100",
        description="iPhone 上 WebDriverAgent 的地址（ios_wda 用）；用 ios forward 8100 8100 转发时保持默认即可。"
        "只填自己的设备，别指向不可信的主机",
    )
    command_timeout_seconds: int = Field(
        default=20, ge=3, le=120, description="单条 adb 命令或 WebDriverAgent 请求的超时秒数"
    )
    text_input: Literal["adb_keyboard", "ascii_only"] = Field(
        default="adb_keyboard",
        description="安卓的文字输入方式：adb_keyboard=通过 ADBKeyboard 输入法（支持中文，需先安装并设为当前输入法）；"
        "ascii_only=系统 input text，只支持英文和数字。iPhone 直接支持中文，不看这一项",
    )

    @field_validator("wda_url")
    @classmethod
    def validate_wda_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("wda_url 必须以 http:// 或 https:// 开头，例如 http://127.0.0.1:8100")
        return value


class ModelSettings(PluginConfigBase):
    """决策用的模型。"""

    __ui_label__ = "模型"
    __ui_icon__ = "cpu"
    __ui_order__ = 2

    vision_task: str = Field(default="vlm", description="看屏幕并决定下一步操作的模型任务名，必须能识图")
    vision_model: str = Field(
        default="",
        description="直接指定视觉模型（model_config 中模型的 name），留空则使用任务配置的模型",
    )
    planner_task: str = Field(default="utils", description="决定这次玩什么的文本模型任务名")
    planner_model: str = Field(default="", description="直接指定决定玩什么的模型，留空则使用任务配置的模型")
    llm_timeout_seconds: int = Field(default=120, ge=10, le=600, description="单次模型调用的超时秒数")


class AllowedApp(PluginConfigBase):
    """允许打开的应用。"""

    package: str = Field(
        default="",
        description="安卓包名或 iPhone 的 Bundle ID，"
        "例如 tv.danmaku.bili（安卓 B 站）、tv.danmaku.bilianime（iPhone B 站）",
    )
    name: str = Field(default="", description="显示名称，例如 哔哩哔哩")
    note: str = Field(default="", description="给 Bot 看的用途说明，例如 刷视频、看番")
    keep_open: bool = Field(
        default=False,
        description="玩完不回桌面，下次打开也不先关掉、接着上次的画面。适合联机游戏：回桌面、关掉会退出房间，重新打开要加载很久",
    )


def _default_allowed_apps() -> List[AllowedApp]:
    return [
        AllowedApp(package="tv.danmaku.bili", name="哔哩哔哩", note="刷视频、看番、看猫猫"),
        AllowedApp(package="com.ss.android.ugc.aweme", name="抖音", note="刷短视频"),
        AllowedApp(package="com.xingin.xhs", name="小红书", note="逛帖子、看穿搭和美食"),
        AllowedApp(package="com.netease.cloudmusic", name="网易云音乐", note="听歌"),
        AllowedApp(package="com.dragon.read", name="番茄免费小说", note="看小说"),
    ]


class SafetySettings(PluginConfigBase):
    """操作边界。"""

    __ui_label__ = "安全"
    __ui_icon__ = "shield"
    __ui_order__ = 3

    allowed_apps: List[AllowedApp] = Field(
        default_factory=_default_allowed_apps,
        description="只能打开和停留在这些应用里；未安装的应用会自动忽略",
    )
    extra_allowed_packages: List[str] = Field(
        default_factory=list,
        description="额外允许停留的系统界面包名（如权限弹窗 com.android.permissioncontroller）；桌面会自动识别",
    )
    allow_text_input: bool = Field(default=True, description="是否允许输入文字（用于搜索）")
    max_text_length: int = Field(
        default=30, ge=1, le=200, description="单次输入文字的最大长度（英文搜索词比中文长，不宜太小）"
    )


class InteractionSettings(PluginConfigBase):
    """用手机上登录的她自己的账号点赞、评论、关注。"""

    __ui_label__ = "互动"
    __ui_icon__ = "heart"
    __ui_order__ = 4

    enabled: bool = Field(
        default=False,
        description="允许她用手机上登录的账号点赞、评论、关注。只在专门给她注册的账号上开启：评论会以这个账号公开发出",
    )
    allow_like: bool = Field(default=True, description="允许点赞")
    allow_comment: bool = Field(default=True, description="允许根据视频内容发评论（不会发弹幕、私信，也不回复别人）")
    allow_follow: bool = Field(default=True, description="允许关注她喜欢的博主、UP 主")
    daily_like_limit: int = Field(default=30, ge=0, le=500, description="每天最多点赞多少次")
    daily_comment_limit: int = Field(
        default=5, ge=0, le=100, description="每天最多发多少条评论（发得太频繁容易被平台当成营销号）"
    )
    daily_follow_limit: int = Field(default=3, ge=0, le=50, description="每天最多关注多少个账号")
    comment_max_length: int = Field(default=50, ge=5, le=200, description="一条评论最多多少个字")


class PlaySettings(PluginConfigBase):
    """玩手机的节奏和额度。"""

    __ui_label__ = "游玩"
    __ui_icon__ = "gamepad-2"
    __ui_order__ = 5

    step_interval_seconds: float = Field(default=3.0, ge=0.5, le=60, description="每步操作后等待画面变化的秒数")
    max_steps_per_session: int = Field(default=80, ge=1, le=1000, description="一次最多操作多少步")
    max_session_minutes: int = Field(default=15, ge=1, le=240, description="一次最多玩多少分钟")
    together_minutes: int = Field(
        default=30, ge=1, le=240, description="和对方一起玩（在这台手机上轮流操作）时最多玩多少分钟"
    )
    turn_wait_seconds: int = Field(
        default=180,
        ge=10,
        le=1800,
        description="一起玩时等对方操作的最长秒数；等待期间只截图比对、不调用模型，超时后她再决定要不要继续等",
    )
    daily_session_limit: int = Field(default=8, ge=1, le=200, description="每天最多玩几次")
    daily_step_limit: int = Field(default=600, ge=1, le=20000, description="每天最多操作多少步（每步一次识图调用）")
    max_consecutive_errors: int = Field(default=3, ge=1, le=20, description="连续出错多少次后结束本次游玩")
    allow_chat_trigger: bool = Field(
        default=True, description="是否允许 Bot 在聊天中自己决定去玩手机（play_phone 工具）"
    )
    fresh_start: bool = Field(
        default=True, description="每次开始玩时先关掉应用再打开，从应用首页开始，不接着上次停下的页面"
    )
    go_home_after: bool = Field(default=True, description="玩完后回到桌面，视频不会在后台一直播放")
    finish_notice: Literal["natural", "template", "off"] = Field(
        default="natural",
        description="玩完后在发起的聊天里怎么说：natural=让她用自己的语气随口聊两句（也可能不说）；"
        "template=固定格式的通知；off=不说",
    )
    use_ui_tree: bool = Field(
        default=True,
        description="读取界面元素并编号，普通应用点击更准；游戏、视频画面读不到元素时只用坐标",
    )
    mark_elements: bool = Field(default=True, description="在发给模型的截图上画出元素编号框")
    max_elements: int = Field(default=40, ge=5, le=120, description="每步最多列出多少个界面元素")
    history_steps: int = Field(default=6, ge=0, le=30, description="决策时附带最近几步操作记录")
    model_image_max_edge: int = Field(default=1280, ge=480, le=2560, description="发给视觉模型的截图最长边像素")
    use_bot_personality: bool = Field(default=True, description="决策时带上 Bot 的人设，让玩法更像她自己")
    extra_persona: str = Field(default="", description="额外的兴趣或性格补充，例如 喜欢猫猫视频和轻音乐")


class AutoPlaySettings(PluginConfigBase):
    """空闲时自己决定要不要玩。"""

    __ui_label__ = "自动玩"
    __ui_icon__ = "clock"
    __ui_order__ = 6

    enabled: bool = Field(default=False, description="开启后 Bot 会在空闲时自己决定要不要玩手机、玩什么")
    check_interval_minutes: int = Field(default=30, ge=1, le=1440, description="每隔多少分钟考虑一次要不要玩")
    play_probability: float = Field(default=0.3, ge=0, le=1, description="每次考虑时去玩的概率")
    active_hours: str = Field(default="10:00-23:00", description="允许自动玩的时间段，格式 HH:MM-HH:MM，可跨午夜")

    @field_validator("active_hours")
    @classmethod
    def validate_active_hours(cls, value: str) -> str:
        if not _HOURS_PATTERN.match(value.strip()):
            raise ValueError("active_hours 格式应为 HH:MM-HH:MM，例如 10:00-23:00")
        return value.strip()


class WebUISettings(PluginConfigBase):
    """实时画面与截图。"""

    __ui_label__ = "实时画面"
    __ui_icon__ = "monitor"
    __ui_order__ = 7

    frame_max_edge: int = Field(default=720, ge=240, le=1600, description="WebUI 画面最长边像素")
    frame_jpeg_quality: int = Field(default=70, ge=30, le=95, description="WebUI 画面 JPEG 质量")
    min_frame_interval_seconds: float = Field(
        default=1.0, ge=0.5, le=30, description="WebUI 打开期间刷新画面的最短间隔秒数"
    )
    chat_image_max_edge: int = Field(default=1280, ge=320, le=2560, description="/手机截图 发到聊天里的图片最长边像素")


class PhonePlayConfig(PluginConfigBase):
    """刷会儿手机插件配置。"""

    plugin: PluginSettings = Field(default_factory=PluginSettings)
    device: DeviceSettings = Field(default_factory=DeviceSettings)
    model: ModelSettings = Field(default_factory=ModelSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    interaction: InteractionSettings = Field(default_factory=InteractionSettings)
    play: PlaySettings = Field(default_factory=PlaySettings)
    auto_play: AutoPlaySettings = Field(default_factory=AutoPlaySettings)
    webui: WebUISettings = Field(default_factory=WebUISettings)
