"""刷会儿手机插件配置模型。"""

from typing import List, Literal, Tuple

from maibot_sdk import Field, PluginConfigBase
from pydantic import ValidationInfo, field_validator, model_validator

import re

from .life import KIND_LABELS

_HOURS_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")
# 「设备」那一台没写名字时叫这个
DEFAULT_DEVICE_NAME = "默认"


class PluginSettings(PluginConfigBase):
    """插件基础设置与权限。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.1.0", description="配置版本")
    admin_user_ids: List[str] = Field(
        default_factory=list,
        description=(
            "可以用 /手机截图（没开 screenshot_for_everyone 时）、在聊天里让她截图给你看、让她换手机的账号；"
            "/手机 开始、停止这些控制命令的权限改在 MaiBot 后台「命令管理」里设（phone_control）"
        ),
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

    name: str = Field(
        default="",
        description="设备名字，切换设备时用，例如 iPhone、OPPO；只有一台设备时可以不填（叫「默认」）",
    )
    backend: Literal["adb", "ios_wda"] = Field(
        default="adb",
        description="设备类型：adb=安卓真机或模拟器；ios_wda=iPhone（通过 WebDriverAgent 控制）",
    )
    adb_path: str = Field(
        default="adb",
        description="adb 可执行文件路径，只填 adb 程序本身（如 D:\\platform-tools\\adb.exe）；"
        "已加入 PATH 时填 adb 即可",
    )
    serial: str = Field(
        default="",
        description="设备序列号或 IP:端口（如 MuMu 的 127.0.0.1:16384）；留空时使用唯一已连接的设备。"
        "无线调试的手机填 IP:端口，或者只填 IP，端口变了插件会用 mDNS 找新的。只填自己的设备，别指向不可信的主机",
    )
    auto_connect: bool = Field(
        default=True, description="序列号是 IP:端口 或 IP 时，自动执行 adb connect，无线调试换了端口时用 mDNS 找新端口"
    )
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
    # 下面这些都可以留默认值，按全局设置走；配置文件写不了空值，所以用 0、default 表示「跟随全局」
    step_interval_seconds: float = Field(
        default=0,
        ge=0,
        le=60,
        description="在这个应用里每步操作后等几秒；0 表示用全局的 play.step_interval_seconds（最少 0.5 秒）",
    )
    max_minutes: int = Field(
        default=0,
        ge=0,
        le=240,
        description="这个应用一次最多玩几分钟；0 表示用全局上限。填了也不会超过全局的 max_session_minutes"
        "（一起玩时是 together_minutes）",
    )
    reopen_on_start: Literal["default", "yes", "no"] = Field(
        default="default",
        description="打开时先关掉、从首页开始吗：default 按全局 fresh_start（15 分钟内再开同一个应用会接着上次）；"
        "yes 总是重新打开；no 总是接着上次的画面（联机游戏关掉会退出房间、重新加载）",
    )
    go_home_after: Literal["default", "yes", "no"] = Field(
        default="default",
        description="玩完回桌面吗：default 按全局 go_home_after；yes 回；no 留在这个应用（联机游戏回桌面会退出房间）。"
        "按结束时实际在用的应用算",
    )
    keep_open: bool = Field(
        default=False,
        description="旧选项，相当于 reopen_on_start=no 加 go_home_after=no；单独设置了那两项时以那两项为准",
    )
    devices: List[str] = Field(
        default_factory=list,
        description='只在这些设备上用，写设备名，例如 ["OPPO"]；不写就是哪台都能用。'
        "iPhone 和安卓上包名不一样的应用各写一条、各标上设备",
    )

    @field_validator("step_interval_seconds")
    @classmethod
    def validate_step_interval(cls, value: float) -> float:
        if 0 < value < 0.5:
            raise ValueError("step_interval_seconds 填 0（跟随全局）或者 0.5 到 60 之间的秒数")
        return value

    def reopens(self, default: bool) -> bool:
        """打开时要不要先关掉；``default`` 是这个应用没单独设置时的结果。"""
        if self.reopen_on_start != "default":
            return self.reopen_on_start == "yes"
        return False if self.keep_open else default

    def goes_home(self, default: bool) -> bool:
        """玩完要不要回桌面；``default`` 是全局的 go_home_after。"""
        if self.go_home_after != "default":
            return self.go_home_after == "yes"
        return False if self.keep_open else default

    def available_on(self, device_name: str) -> bool:
        """这台设备能不能用这个应用（devices 没写就是都能用）。"""
        return not self.devices or device_name.casefold() in {name.casefold() for name in self.devices}

    def stays_open(self) -> bool:
        """这个应用自己设了玩完留在里面（go_home_after=no，或者旧的 keep_open）：联机游戏锁屏也会掉线，玩完不锁屏。"""
        return self.go_home_after == "no" or (self.go_home_after == "default" and self.keep_open)


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
    max_model_calls_per_session: int = Field(
        default=120,
        ge=1,
        le=2000,
        description="一次最多调用几次模型：决定玩什么和每步看屏幕都算，失败的也算（宿主内部的自动重试看不到，不算）；用完就结束这次",
    )
    daily_model_call_limit: int = Field(
        default=1000, ge=1, le=50000, description="每天最多调用几次模型，口径同上；用完今天就不再玩"
    )
    max_consecutive_errors: int = Field(default=3, ge=1, le=20, description="连续出错多少次后结束本次游玩")
    allow_chat_trigger: bool = Field(
        default=True, description="是否允许 Bot 在聊天中自己决定去玩手机（play_phone 工具）"
    )
    fresh_start: bool = Field(
        default=True, description="每次开始玩时先关掉应用再打开，从应用首页开始，不接着上次停下的页面"
    )
    go_home_after: bool = Field(default=True, description="玩完后回到桌面，视频不会在后台一直播放")
    lock_after: bool = Field(
        default=True,
        description="玩完锁屏（iPhone 和安卓真机，模拟器不锁），下次玩时自动解开；"
        "你接管着手机、应用设了玩完留在里面（联机游戏）时不锁。手机设了锁屏密码就关掉，不然下次她解不开、玩不了",
    )
    skip_watched_videos: bool = Field(
        default=True,
        description="记住 30 天内看过的视频，不再点开、刷到了直接划走；对方在聊天里点名要看的不拦",
    )
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
    active_hours: str = Field(
        default="10:00-23:00",
        description="允许自动玩的时间段，格式 HH:MM-HH:MM，可跨午夜；"
        "开着「生活联动」并查得到麦生活的日程时按日程，这里不用",
    )

    @field_validator("active_hours")
    @classmethod
    def validate_active_hours(cls, value: str) -> str:
        if not _HOURS_PATTERN.match(value.strip()):
            raise ValueError("active_hours 格式应为 HH:MM-HH:MM，例如 10:00-23:00")
        return value.strip()


_KIND_BY_LABEL = {label: kind for kind, label in KIND_LABELS.items()}


class LifeLinkSettings(PluginConfigBase):
    """和麦生活（Mai_life）的日程联动、玩完交给它分享。"""

    __ui_label__ = "生活联动"
    __ui_icon__ = "calendar"
    __ui_order__ = 9

    enabled: bool = Field(
        default=True,
        description="装了麦生活（Mai_life）时，自动玩按它的日程决定玩不玩、最多玩多久；"
        "没装、查不到当天日程时按「自动玩」的 active_hours。聊天、命令和 WebUI 叫她玩不受影响",
    )
    play_kinds: List[str] = Field(
        default_factory=lambda: ["leisure", "rest", "meal", "travel"],
        description="日程是这几类时按 play_probability 自动玩：leisure 休闲、rest 休息、meal 吃饭、travel 在路上、"
        "study 学习、work 工作、nap 午睡、sleep 睡觉（写中文也行）",
    )
    slack_kinds: List[str] = Field(
        default_factory=lambda: ["work", "study"],
        description="日程是这几类时偶尔摸鱼玩一会儿（写法同 play_kinds；也写在 play_kinds 里的按 play_kinds）；"
        "两边都没写的（默认是睡觉、午睡）不玩",
    )
    slack_probability: float = Field(
        default=0.1, ge=0, le=1, description="上班、上课这类日程时，每次考虑摸鱼的概率；0 表示这时候不玩"
    )
    slack_minutes: int = Field(default=5, ge=1, le=60, description="摸鱼一次最多玩几分钟")
    min_minutes: int = Field(default=5, ge=1, le=60, description="这段日程剩下不到这么多分钟就先不玩")
    share_after_play: bool = Field(
        default=True,
        description="按麦生活日程自动玩、自然结束的那几局，玩完把看到的交给麦生活的主动分享；"
        "由它按私聊用户的额度、免打扰决定什么时候跟谁提起，也可能不提",
    )
    share_weight: float = Field(
        default=0.4,
        ge=0,
        le=1,
        description="交给麦生活时的分享权重：它打分时从这里起步，再加精力、心情、关系温度，"
        "过了它的阈值（默认 0.45）才可能提起；越高越容易被挑中",
    )

    @field_validator("play_kinds", "slack_kinds")
    @classmethod
    def validate_kinds(cls, value: List[str], info: ValidationInfo) -> List[str]:
        kinds: List[str] = []
        for item in value:
            text = item.strip()
            kind = _KIND_BY_LABEL.get(text, text.lower())
            if kind not in KIND_LABELS:
                choices = "、".join(f"{key}（{label}）" for key, label in KIND_LABELS.items())
                raise ValueError(f"{info.field_name} 里的「{item}」不认识，可以写：{choices}")
            if kind not in kinds:
                kinds.append(kind)
        return kinds


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


class MultiDeviceSettings(PluginConfigBase):
    """更多手机：可以用 /手机 切换、或者在聊天里叫她换一台。"""

    __ui_label__ = "多设备"
    __ui_icon__ = "smartphone"
    __ui_order__ = 8

    devices: List[DeviceSettings] = Field(
        default_factory=list,
        description="除了「设备」那一台以外的手机，写法和「设备」一样，每台都要写 name；"
        "用 /手机 切换 名字 换着用，当前用的是哪台插件会记住",
    )

    @field_validator("devices")
    @classmethod
    def validate_devices(cls, value: List[DeviceSettings]) -> List[DeviceSettings]:
        for device in value:
            if not device.name.strip():
                raise ValueError("多设备里的每台手机都要写 name，切换时用它")
        return value


class DesktopPetSettings(PluginConfigBase):
    """MaiBot 内置桌宠上的按钮和气泡。"""

    __ui_label__ = "桌宠"
    __ui_icon__ = "cat"
    __ui_order__ = 10

    button: bool = Field(
        default=True,
        description="在桌宠的「插件扩展」菜单里放一个「手机」按钮：上面写着她在不在玩，"
        "点开能看手机截图和她此刻的想法，还能开始、暂停、停止、接管",
    )
    bubbles: bool = Field(
        default=True,
        description="她拿起、放下手机时在桌宠上冒一句气泡；只在桌面上显示，不进聊天记录、不调用模型，桌宠没开时跳过",
    )
    thought_bubbles: bool = Field(
        default=True,
        description="玩的时候把她此刻的想法也冒成气泡（「💭 这只小猫好软」），不打开面板也知道她在看什么；同一句不重复",
    )
    thought_bubble_seconds: int = Field(
        default=20, ge=5, le=600, description="两个气泡之间至少隔多少秒，免得太吵；拿起、放下手机的那句不受限制"
    )
    animations: bool = Field(
        default=True,
        description="拿起手机时打个招呼，玩够了放下时高兴一下，卡住、出错结束时摇摇头；具体动作由桌宠皮肤决定，皮肤没有就不做",
    )


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
    multi_device: MultiDeviceSettings = Field(default_factory=MultiDeviceSettings)
    life_link: LifeLinkSettings = Field(default_factory=LifeLinkSettings)
    desktop_pet: DesktopPetSettings = Field(default_factory=DesktopPetSettings)

    @model_validator(mode="after")
    def validate_device_names(self) -> "PhonePlayConfig":
        names = [name for name, _ in self.device_choices()]
        folded = [name.casefold() for name in names]
        duplicated = sorted({name for name in names if folded.count(name.casefold()) > 1})
        if duplicated:
            raise ValueError(
                f"设备名字重复了：{'、'.join(duplicated)}（「设备」那一台没写 name 时叫「{DEFAULT_DEVICE_NAME}」）"
            )
        for app in self.safety.allowed_apps:
            unknown = [name for name in app.devices if name.casefold() not in folded]
            if unknown:
                raise ValueError(
                    f"应用 {app.name or app.package} 的 devices 里有不存在的设备：{'、'.join(unknown)}"
                    f"（现有：{'、'.join(names)}）"
                )
        return self

    def device_choices(self) -> List[Tuple[str, DeviceSettings]]:
        """所有能切换的设备（名字，设置）：「设备」那一台在最前面。"""
        main = (self.device.name.strip() or DEFAULT_DEVICE_NAME, self.device)
        return [main, *((device.name.strip(), device) for device in self.multi_device.devices)]
