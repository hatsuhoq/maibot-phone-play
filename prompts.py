"""给模型的提示词：决定玩什么、每一步怎么操作。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from .actions import extract_json_object, find_app
from .life import LifeActivity
from .pages import BILI_DETAIL, BILI_PLAYER, BILIBILI_PACKAGES
from .settings import AllowedApp
from .store import SessionRecord
from .ui_tree import UIElement

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


class PlanDeclined(ValueError):
    """想做的事用手机上能打开的应用做不到；消息是模型给出的原因。"""


@dataclass(frozen=True)
class Persona:
    """Bot 的身份信息。"""

    nickname: str
    personality: str = ""
    extra: str = ""

    def describe(self) -> str:
        parts = [text.strip() for text in (self.personality, self.extra) if text.strip()]
        return "\n".join(parts)


@dataclass(frozen=True)
class Plan:
    """这次玩手机的打算。"""

    app: AllowedApp
    goal: str
    minutes: int


@dataclass(frozen=True)
class InteractionQuota:
    """开启互动时今天还能点赞、评论、关注多少次（配置里没允许的那类是 0），以及一条评论的字数上限。"""

    likes: int
    comments: int
    follows: int
    comment_max_length: int


@dataclass(frozen=True)
class DecisionContext:
    """单步决策需要的全部信息。"""

    persona: Persona
    goal: str
    apps: Sequence[AllowedApp]
    allow_text_input: bool
    ascii_text_only: bool
    max_text_length: int
    now: datetime
    elapsed_minutes: float
    max_minutes: int
    step: int
    max_steps: int
    foreground: str
    history: Sequence[str]
    elements: Sequence[UIElement]
    elements_note: str
    image_url: str
    # 画面和上一步几乎一样的连续步数
    unchanged_steps: int = 0
    # 和对方在这台手机上一起玩（比如同一台手机上两个人轮流玩的游戏）
    together: bool = False
    # iPhone 没有返回键，back 是从左边缘往右滑
    ios: bool = False
    # 开启了用她自己的账号互动时的额度；没开启是 None
    interaction: Optional[InteractionQuota] = None
    # 连续做同一个操作的次数（够多了才提醒，否则是 0）和那个操作的说明
    repeated_steps: int = 0
    repeated_action: str = ""
    # 对方刚在聊天里让她在手机上做的事，带编号，例如「#2 放《春日影》」
    requests: Sequence[str] = ()
    # 之前交给她、还没有结果的要求（最近几条），操作记录一长就会被挤掉；做到了、做不到、被换掉的不再列
    earlier_requests: Sequence[str] = ()
    # 这个应用里最近看过的视频标题，刷到了别再看
    watched: Sequence[str] = ()
    # 这个应用里看了一部分、可以接着看的长视频，例如「《……》（看了一部分（约 3 分钟））」
    resumable: Sequence[str] = ()
    # 正在看的视频和已经看了多久，例如「《春日影》（长视频），已经看了约 3 分钟」
    watching: str = ""
    # 当前应用专门的说明（比如 B 站的播放器是什么样的）
    app_tips: str = ""
    # 看过的视频不再看（play.skip_watched_videos）
    skip_watched: bool = True
    # 这次已经标过的「这一刻」她在想什么；没标过是空的
    moment: str = ""


# 实测 iPhone 上的 B 站（2026 年 11 月）：首页推荐点开的进竖屏播放器，搜索结果点开的进视频详情页，两种页面操作不一样
_BILIBILI_TIPS = (
    "B 站：首页、搜索结果里视频卡片的右下角写着时长，卡片上标着「最近看过」的是看过的。"
    "首页推荐点开的视频进竖屏播放器（上滑换下一个推荐）；搜索结果、相关推荐、UP 主主页点开的进视频详情页"
    "（上面播放器、下面简介和评论，上滑不换视频）。插件认出是哪种页面时会告诉你。别点全屏按钮，手机会转成横屏。"
)
_BILIBILI_PAGE_TIPS: Dict[str, str] = {
    BILI_PLAYER: (
        "现在是 B 站的竖屏播放器（从首页推荐点开的样子）：竖着占满屏幕的是短视频，横着放在中间、上下黑边的是长视频；"
        "上滑就是下一个推荐视频（长视频也一样），想接着看就别上滑；右下角「详情页」进去能看简介、分集和评论。"
        "看的时候在 seen 里写上画面上的标题（插件读得到时会自己记）。"
    ),
    BILI_DETAIL: (
        "现在是 B 站的视频详情页（从搜索结果、相关推荐、UP 主主页点开的样子）：最上面是播放器，视频在那里播；"
        "下面是简介和评论（「简介」「评论」两个标签切换），简介里有标题、点赞投币那一排、分集（共几集时可以点下一集）"
        "和更下面的相关推荐。这里上滑只会翻简介、评论和相关推荐，不会换视频，视频也不会停；"
        "看视频就用带 video 的 wait 停下来看（横屏的是长视频）。"
        "想换一个：返回搜索结果点下一个，或者点下面相关推荐里的。"
        "这个页面的标题插件会自己记下。"
    ),
}


def app_tips(package: str, page: str = "") -> str:
    """当前应用、当前页面专门的说明；没有就是空字符串。"""
    if package not in BILIBILI_PACKAGES:
        return ""
    return _BILIBILI_PAGE_TIPS.get(page, _BILIBILI_TIPS)


def format_apps(apps: Sequence[AllowedApp]) -> str:
    return "\n".join(f"- {app.name}（{app.package}）{'：' + app.note if app.note else ''}" for app in apps)


def _identity(persona: Persona) -> str:
    description = persona.describe()
    return f"你是{persona.nickname}。\n{description}" if description else f"你是{persona.nickname}。"


def build_plan_prompt(
    *,
    persona: Persona,
    now: datetime,
    apps: Sequence[AllowedApp],
    recent: Sequence[SessionRecord],
    hint: str,
    max_minutes: int,
    together: bool = False,
    interaction: bool = False,
    memories: Sequence[str] = (),
    life: Optional[LifeActivity] = None,
    slack: bool = False,
) -> str:
    """让文本模型决定这次打开哪个应用、做什么、玩多久。

    ``interaction`` 表示开启了用她自己的账号互动；``memories`` 是她记得的内容（看过的、对方说过的、上次没做完的）；
    ``life`` 是麦生活日程里现在这一段（自动玩时才有，``remaining_seconds`` 已经换算到现在），
    ``max_minutes`` 已经按这段剩下的时间（摸鱼时还有摸鱼的上限）算过；``slack`` 表示是上班、上课中途偷偷摸鱼。
    """
    recent_text = "\n".join(
        f"- {record.started_at[5:16].replace('T', ' ')} {record.places_text()}：{record.goal}（{record.summary}）"
        for record in recent
    )
    memory_text = ""
    if memories:
        memory_text = (
            "你记得的（「画面上看到的」是你从手机画面认出来的，「对方说过」是对方亲口说的）：\n"
            + "\n".join(f"- {line}" for line in memories)
            + "\n想接着上次看、接着做的话就照这些挑；不想也可以按兴趣来。\n\n"
        )
    if hint:
        hint_text = (
            f"这次想做的事：{hint}\n"
            "如果这件事用上面这些应用都做不到，不要硬选一个，输出 "
            '{"app": "", "reason": "为什么做不到，一句话"}。'
        )
    else:
        hint_text = (
            "这次玩什么由你自己决定：按你的兴趣、心情、时间和最近玩过的内容来选，别总玩同一个。"
            "想好具体想看、想听的东西（某个话题、某首歌、某类视频），goal 写具体一点，例如「在B站搜吉他弹唱看看」。"
        )
    if together:
        hint_text += (
            "\n这次是和对方一起玩：对方可能在这台手机上和你轮流操作，也可能用自己的手机和你联机、组队；"
            "选能一起玩的应用，goal 里写上是和对方一起玩、怎么一起玩（对方说了什么模式、哪个房间就照着写）。"
        )
        interact_text = "在手机上只是看、听、玩，不下载保存文件、不发布内容；除了和对方一起玩，不和别人互动。\n"
    else:
        interact_text = "在手机上只是看、听、玩，不下载保存文件、不发布内容、不和别人互动。\n"
    if interaction:
        # 实测没改这里时，她想给 UP 主留评论会被这句话挡回来：「不发布内容也不和别人互动，所以做不到」
        interact_text = (
            "手机上登录的是你自己的账号：看到喜欢的可以点赞、评论、关注博主或 UP 主；"
            "但不发视频、动态、弹幕、私信，不下载保存文件。\n"
        )
        if together:
            interact_text += "和对方一起玩时就专心和对方玩。\n"
    opener = "你有一点空闲，打算拿起手机玩一会儿。"
    life_text = ""
    if life is not None:
        # 两边时区可能设得不一样，只说这段还剩多久，不说几点结束
        life_text = f"按你今天的日程，现在是{life.describe()}，这段还有大约 {int(life.remaining_seconds // 60)} 分钟。"
        if life.scene:
            life_text += f"此刻的情形：{life.scene}"
        if slack:
            opener = "你正忙着，想趁没人注意偷偷拿起手机摸一会儿鱼。"
            life_text += (
                f"\n这是{life.kind_label}中途偷偷摸鱼：只玩几分钟，挑能随时放下的内容（刷几条短视频、看几条笔记），"
                f"别一头扎进长视频、游戏里，玩完接着{life.kind_label}。\n"
            )
        else:
            life_text += "\n挑适合这会儿的内容，别玩到这段结束以后。\n"
    return (
        f"{_identity(persona)}\n\n"
        f"现在是 {now:%Y-%m-%d %H:%M}（{_WEEKDAYS[now.weekday()]}），{opener}\n"
        f"{life_text}"
        f"手机上可以打开的应用：\n{format_apps(apps)}\n\n"
        f"最近玩过：\n{recent_text or '（还没玩过）'}\n\n"
        f"{memory_text}"
        f"{hint_text}\n"
        f"{interact_text}"
        f"请决定这次打开哪个应用、想做什么、玩多久（1~{max_minutes} 分钟）。\n"
        "只输出一个 JSON 对象，不要输出其它内容，例如：\n"
        f'{{"app": "{apps[0].package}", "goal": "想做的事，一句话", "minutes": {min(10, max_minutes)}}}'
    )


def parse_plan(response: str, apps: Sequence[AllowedApp], max_minutes: int) -> Plan:
    """解析并校验玩手机的打算。

    Raises:
        PlanDeclined: 模型认为这些应用做不到想做的事时抛出。
        ValueError: 回复格式不对或选了不允许的应用时抛出。
    """
    data = extract_json_object(response)
    app_value = str(data.get("app") or "").strip()
    if not app_value:
        reason = str(data.get("reason") or "").strip()[:80]
        raise PlanDeclined(reason or "手机上的应用做不到这件事")
    app = find_app(apps, app_value)
    if app is None:
        raise ValueError(f"选择的应用「{app_value}」不在可以打开的应用里")
    goal = str(data.get("goal", "")).strip()[:80]
    if not goal:
        raise ValueError("没有说想做什么")
    minutes = data.get("minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        raise ValueError("minutes 必须是数字")
    return Plan(app=app, goal=goal, minutes=min(max(1, round(minutes)), max_minutes))


def _interaction_rule(quota: InteractionQuota) -> str:
    """开启互动时，告诉她能怎么用自己的账号点赞、评论、关注。"""
    parts = ["这台手机上登录的是你自己的账号，可以像平时一样互动；不想互动就不互动，不用为了用完额度去互动。"]
    if quota.likes:
        parts.append("看完真心喜欢的视频，用 like 点它的点赞按钮。")
    if quota.comments:
        parts.append(
            "有话想说就评论：先打开评论区，用 comment 点评论输入框并写好，再点发送或发布。"
            f"评论要看过视频、根据内容用你自己的口吻说一两句，友好、简短（不超过 {quota.comment_max_length} 个字）；"
            "不发广告和链接，不 @ 别人，不留联系方式，不聊政治和敏感话题，不跟人争吵。"
        )
    if quota.follows:
        parts.append("特别喜欢某个博主、UP 主时，用 follow 点他的关注按钮；已经关注了就别再点，再点会取消关注。")
    counts = (("点赞", quota.likes), ("评论", quota.comments), ("关注", quota.follows))
    used_up = [name for name, left in counts if not left]
    if used_up:
        parts.append(f"今天不能再{'、'.join(used_up)}了。")
    return "".join(parts)


def _rules(context: DecisionContext) -> str:
    if context.allow_text_input:
        text_rule = f"输入文字只用于搜索，每次不超过 {context.max_text_length} 个字。"
        if context.interaction is not None and context.interaction.comments:
            text_rule = f"type 只用于搜索，每次不超过 {context.max_text_length} 个字；写评论用 comment。"
        if context.ascii_text_only:
            text_rule += "这台手机只能输入英文字母和数字，不能输入中文，搜索词请直接用简短的英文。"
    else:
        text_rule = "不要输入任何文字。"
    if context.interaction is None:
        boundary = (
            # 没开互动时 like 动作会被拦下，提示词也别再说能点赞（之前写着「除了点赞不做其它互动」）
            "只使用下面列出的应用。不要进入设置、应用商店；不要付款、充值、下单；不要点赞，不要发评论、弹幕、私信；"
            "不要关注别人、加好友；不要登录、注册或修改账号。"
        )
    else:
        boundary = (
            "只使用下面列出的应用。不要进入设置、应用商店；不要付款、充值、下单；不要发弹幕、私信，不要回复别人的评论；"
            "不要加好友；不要登录、注册或修改账号。"
        )
    rules = [
        boundary,
        text_rule,
        "不要下载或保存文件，不要安装应用。遇到登录、付款、权限申请、下载推广、求好评之类的弹窗，"
        "点关闭、拒绝、跳过、下次再说或返回；"
        "关不掉、返回也绕不开、必须有人处理才能接着玩时（要登录或重新登录、验证码或滑块验证、"
        "只能点允许的系统权限、强制更新），用 ask_help 请对方帮忙，自己绝不输入账号、密码、验证码；"
        "能关掉的弹窗、能换个内容看的照常自己处理，别动不动就求助。弹窗挡在前面时（屏幕中间一个框、后面变暗），后面的东西都点不到，"
        "要先关掉弹窗；界面元素列表里可能还列着弹窗后面的按钮，别去点它们。"
        "应用刚打开时的开屏广告（角上有「跳过」和倒计时）几秒后会自己消失，用 wait 等几秒就好，"
        "别去点，点到广告会跳到别的应用。",
        "一直记着这次想做的事。当前画面和它无关时（比如还停在别的视频、别的搜索结果里），"
        "先返回或回到应用首页，去搜索或找相关内容，不要被眼前的画面带跑。",
        "按自己的兴趣挑内容：首页、推荐里没有感兴趣的，就用搜索找你想看的。"
        "找到喜欢的歌曲、文章，用 wait 停下来看一会儿；"
        "点开后发现不感兴趣（带货、广告、营销号、和你的兴趣不沾边的），别硬看：短视频直接划走，长视频、笔记、文章返回列表换一个。"
        "玩够了或想结束时用 finish。",
        "看视频分短视频和长视频，wait 要带上 video。"
        "短视频（竖屏、一两分钟以内，一条接一条）：一眼看出不感兴趣就直接上滑，不用等；"
        '感兴趣就用 {"action":"wait","seconds":20,"video":"short"} 看完它'
        "（一次 10~40 秒，按视频长短定），看完上滑看下一条。"
        "长视频（横屏、几分钟以上）：先用 video 为 long 的 wait 看 20~30 秒，判断喜不喜欢，不喜欢就上滑或返回换一个；"
        "喜欢就一次看 60~120 秒、一段一段接着看，不用每隔几秒就看一眼；"
        "十几分钟以上的长视频看到喜欢的部分、看够几分钟就可以换。"
        "看视频的时候画面一直在变，插件不会因为画面变了叫醒你，看完你定的时间才会再问你。"
        "长视频如果是点开后上面播放器、下面简介和评论的页面，上滑只会翻简介和评论，不会换视频；"
        "不想看了就返回到视频列表（首页、搜索结果）点别的，或者点下面推荐的视频。",
        "看视频时在 seen 里写上画面上的视频标题，插件会记下你看过哪些、看到哪了"
        + (
            "；看过的视频不要再点开，刷到了就直接划走，除非对方在聊天里点名要你再看；"
            "上次只看了一部分的长视频可以点开接着看。"
            if context.skip_watched
            else "。"
        )
        + "画面上看得出视频、歌已经播完（出现重播按钮、进度条到头、已经自动从头开始下一轮）时，"
        + 'seen 里加 "progress":"finished"；'
        '不感兴趣、要划走或返回的视频，seen 里加 "skip":true。只看了一会儿、没看到结尾的不要写看完。',
        "留意最近的操作：同一件事已经做了两三次、画面却没有变化，就换个做法，或者去看别的内容。"
        "一直黑屏或转圈加载时不要干等：往上划到下一个、返回再进或换个入口，实在不行就结束。",
        # 实测和群友打斗地主好友房，她觉得「没等到人」就结束，群友一喊又重开，一上午十一次斗地主有八次这样
        "在联机游戏的房间里（好友房、组队房间）等别人进来、等开局、等匹配时，用长一点的 wait 等着，"
        "不要 finish，也不要退出房间：别人可能正在进来。等了好几分钟还是没人来，再结束。",
    ]
    if context.interaction is not None:
        rules.append(_interaction_rule(context.interaction))
        if context.interaction.likes:
            rules.append(
                "点赞要克制，真喜欢才点。同一条视频只点一次：点过的爱心会变红，再点就取消了；"
                "最近的操作里刚给这条点过赞，就接着看或者换下一条，不要再点。"
            )
    if context.ios:
        rules.append(
            "iPhone 从别的应用跳过来时，屏幕左上角时间下面会出现「◀ 应用名」，那是回到上一个应用的快捷按钮，"
            "不代表你在那个应用里，不要点它；现在在哪个应用以「当前前台」为准。"
        )
    if context.together:
        # 实测对方在自己手机上和她组欢乐斗地主 2v2，原来的规则只有同机轮流、「不要联网匹配陌生人」，
        # 她进了组队房间不敢开局，连着三次刚进房间就结束，玩完回桌面，房间也退了
        rules.append(
            "这次是和对方一起玩，有两种：对方直接在这台手机上操作（人人对战、同机双人这类模式），"
            "轮到对方时不要替对方操作，用 wait_turn 等对方；"
            "或者对方用自己的手机和你联机（好友房、组队、对方发来邀请），这时进对方说的模式或房间、接受对方的邀请，和对方一起开局，需要匹配别的玩家凑满一桌也没关系，"
            "对局里轮到你就照常出手。在房间里等对方进来、等开局、等匹配时，用 wait 或 wait_turn 等着，不要 finish；"
            "不要丢下对方自己去和陌生人开局。打完一局、分出胜负后再决定接着玩还是 finish，summary 写清楚谁赢了；"
            "等了好几分钟对方还没来，再结束。"
        )
    rules.append("只输出一个 JSON 对象，不要输出其它内容。")
    return "规则：\n" + "\n".join(f"{index}. {rule}" for index, rule in enumerate(rules, start=1))


def _interaction_guide(context: DecisionContext) -> str:
    quota = context.interaction
    if quota is None:
        return ""
    guide = ""
    if quota.likes:
        guide += '{"action":"like","element":3}：点赞（点视频的点赞按钮，也可以用 point）\n'
    if quota.comments:
        guide += (
            '{"action":"comment","element":12,"text":"这只橘猫也太圆了"}：写评论。element（或 point）填评论输入框'
            "（写着「说点什么」「发一条友善的评论」之类的框，不是旁边的表情、@ 按钮），插件会先点开它再写；"
            "写好后还要点发送或发布\n"
        )
    if quota.follows:
        guide += '{"action":"follow","element":3}：关注（点博主、UP 主的关注按钮，也可以用 point）\n'
    return guide


_ACTION_INTRO = (
    '坐标：用 "point":[x,y] 表示，x、y 都是 0~1000 的相对坐标，[0,0] 是屏幕左上角，[1000,1000] 是右下角。'
    "截图上画了编号框时，点击对应元素优先填写 element 编号。\n"
    "可用操作：\n"
)
_ACTION_GUIDE = (
    '{"action":"tap","element":3} 或 {"action":"tap","point":[500,820]}：点击\n'
    '{"action":"long_press","element":3}：长按\n'
    '{"action":"swipe","direction":"up"}：滑动。up=手指上滑（往下翻；在短视频里是看下一条），'
    "down=手指下滑（往上翻；在短视频里是回上一条），left/right=左右滑；可以加 point 指定起点\n"
    '{"action":"type","text":"猫猫"}：在已经点开的输入框里输入文字\n'
    '{"action":"enter"}：按回车或搜索键\n'
)
_BACK_GUIDE = '{"action":"back"}：返回\n'
_IOS_BACK_GUIDE = (
    '{"action":"back"}：返回（iPhone 没有返回键，这个操作是从屏幕左边缘往右滑；'
    "有的页面不支持这个手势，那就点左上角的返回或关闭按钮）\n"
)
_APP_GUIDE = (
    '{"action":"home"}：回到桌面\n'
    '{"action":"launch","app":"包名"}：打开应用\n'
    '{"action":"wait","seconds":20}：停下来看一会儿（1~120 秒）。'
    "在游戏房间里等人进来、等开局、等匹配时可以一次等 60~120 秒，"
    "有人进来、开局了或聊天里有新要求会提前叫醒你；轮到你操作、有倒计时的时候别等这么久\n"
    '{"action":"wait","seconds":20,"video":"short"}：看视频。'
    "video 写 short（短视频，一次最多 60 秒）或 long（长视频，最多 120 秒），"
    "看的时候画面变了不会叫醒你，聊天里有新要求或跳到了别的应用才会\n"
)
_WAIT_TURN_GUIDE = '{"action":"wait_turn"}：等对方操作（对方操作完、画面变了才继续，等待时不用你做什么）\n'
_HELP_GUIDE = (
    '{"action":"ask_help","text":"小红书要重新登录才能看笔记"}：请对方帮忙，'
    "text 用一句话写清楚卡在哪、要对方做什么；求助后你会停下来，等对方在手机上处理好再接着玩\n"
)
_ACTION_OUTRO = (
    '{"action":"finish"}：结束这次玩手机。这一步的 summary 不要写「结束」，改用一两句话写这次看到了什么、有什么感受，'
    "例如「看了好几只小奶猫，那只恐高的最呆」\n"
    "每个 JSON 还必须包含：\n"
    '"thought"：你此刻真实的内心想法，一句话，用你自己的口吻；\n'
    '"summary"：这一步在做什么，十个字左右，例如「点开猫猫视频」（finish 除外，见上）。\n'
    '可以加 "seen"：正在看、读、听的内容在画面上清楚写着名字时（书名、视频标题、歌名）才写，下次能接着看；'
    "看视频的 wait 能看清标题就都带上，插件靠它记住看过哪些视频，"
    '例如 {"type":"novel","title":"画面上原样的书名","detail":"第12章","certain":true}；'
    "type 是 novel、video、music 之一，detail 只写画面上明确写着的章节或集数，没有就不写；看不清、靠猜的不要写；"
    '视频还可以加 "progress":"finished"（画面上看得出播完了）或 "skip":true（不感兴趣）。\n'
    # 实测写成「特别喜欢才写、普通的好看不用写」时，一局安静的弹琴视频看下来一次都没标
    '可以加 "moment":true：这次玩手机里你最喜欢、最想给对方看的那一下（很可爱、很好笑、很惊艳、很戳你的画面）'
    "标一次，真没有喜欢的就不标；之后遇到更喜欢的可以再标，插件只留最后一次的画面。\n"
    '示例：{"thought":"这只橘猫好圆，点开看看","summary":"点开猫猫视频","action":"tap","element":5}'
)
# 对方交给她的事：做没做成要靠这一步的截图说话，点了一下、页面还在加载都不算
_REQUEST_GUIDE = (
    '对方交给你的事有了结果时，在那一步的 JSON 里加 "request"，'
    '例如 {"id":2,"result":"done","note":"画面上在播放《春日影》"}：id 是要求前面的编号；'
    "result 写 done（这张截图上已经能看到做到了：歌在播放、已经退出房间、已经点了准备）"
    "或 failed（确定做不到，note 写原因，比如找不到这首歌、按钮点不了）；note 用一句话写你在画面上看到的依据。"
    "只是点了一下、页面还在加载、还没看到结果的不要写 done，接着做；还在做的不用加 request。\n"
)


def build_decision_messages(context: DecisionContext) -> List[Dict[str, Any]]:
    """组装单步决策的多模态消息（系统提示 + 文字状态 + 截图）。"""
    system_text = (
        f"{_identity(context.persona)}\n\n"
        "你现在正拿着自己的手机打发时间。你会看到手机当前的屏幕截图，需要像真人一样决定下一步操作。\n\n"
        f"这次想做的事：{context.goal}\n"
        f"可以打开的应用：\n{format_apps(context.apps)}\n\n"
        f"{_rules(context)}\n\n"
        f"{_ACTION_INTRO}{_ACTION_GUIDE}{_IOS_BACK_GUIDE if context.ios else _BACK_GUIDE}{_APP_GUIDE}"
        f"{_interaction_guide(context)}"
        f"{_WAIT_TURN_GUIDE if context.together else ''}{_HELP_GUIDE}{_ACTION_OUTRO}"
        f"{chr(10) + _REQUEST_GUIDE if context.requests or context.earlier_requests else ''}"
    )

    history_text = "\n".join(context.history) if context.history else "（刚开始）"
    if context.elements:
        elements_text = "界面元素（编号与截图上的编号框对应）：\n" + "\n".join(
            element.describe() for element in context.elements
        )
    else:
        elements_text = context.elements_note or "这个画面没有列出界面元素，请直接用坐标。"
    # 模型每步只看到一张截图，自己比不出画面有没有变，由代码比对后告诉它
    unchanged_text = ""
    if context.unchanged_steps:
        unchanged_text = f"注意：画面已经连续 {context.unchanged_steps} 步和上一步几乎一样，没有变化。\n"
    if context.repeated_steps:
        unchanged_text += (
            f"注意：你已经连续 {context.repeated_steps} 步在做同一个操作（{context.repeated_action}），好像没有效果；"
            "换个做法，或者看看是不是被弹窗、表情面板挡住了，或者那个按钮还点不了。\n"
        )
    quota = context.interaction
    quota_text = (
        f"今天还能点赞 {quota.likes} 次、评论 {quota.comments} 条、关注 {quota.follows} 个。\n" if quota else ""
    )
    video_text = ""
    if context.app_tips:
        video_text += f"{context.app_tips}\n"
    if context.watching:
        video_text += f"正在看：{context.watching}\n"
    if context.watched:
        titles = "、".join(f"《{title}》" for title in context.watched)
        video_text += f"这个应用里你最近看过这些视频，再刷到就直接划走，列表里看到也别点开：{titles}\n"
    if context.resumable:
        video_text += f"这个应用里上次只看了一部分的长视频，想看可以接着看：{'、'.join(context.resumable)}\n"
    request_text = ""
    if context.earlier_requests:
        request_text = (
            f"对方交给你、还没有结果的事：{'；'.join(context.earlier_requests)}。接着做，有结果了就用 request 回报。\n"
        )
    if context.requests:
        request_text += (
            f"对方刚在聊天里让你：{'；'.join(context.requests)}。"
            "这比原来的打算优先，先照着做；做到了、确定做不到时用 request 回报。\n"
        )
    if context.moment:
        video_text += f"这次最想给对方看的那一下你已经标过了（{context.moment}），遇到更喜欢的再标。\n"
    state_text = (
        # 目标在系统提示里离截图太远，模型容易只顾眼前画面，这里紧挨着截图再说一次
        f"这次想做的事：{context.goal}\n"
        f"{request_text}"
        f"当前时间：{context.now:%H:%M}\n"
        f"已经玩了 {context.elapsed_minutes:.0f} 分钟（打算玩 {context.max_minutes} 分钟），"
        f"这是第 {context.step}/{context.max_steps} 步。\n"
        f"当前前台：{context.foreground or '无法判断'}\n"
        f"{video_text}"
        f"{unchanged_text}"
        f"{quota_text}"
        f"最近的操作：\n{history_text}\n\n"
        f"{elements_text}\n\n"
        "请看截图，输出下一步操作的 JSON。"
    )
    return [
        {"role": "system", "content": system_text},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": state_text},
                {"type": "image_url", "image_url": {"url": context.image_url}},
            ],
        },
    ]
