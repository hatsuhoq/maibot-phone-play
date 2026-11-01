"""给模型的提示词：决定玩什么、每一步怎么操作。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from .actions import extract_json_object, find_app
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
    # 对方刚在聊天里让她在手机上做的事
    requests: Sequence[str] = ()
    # 这次之前已经交给她的要求（最近几条），操作记录一长就会被挤掉
    earlier_requests: Sequence[str] = ()


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
) -> str:
    """让文本模型决定这次打开哪个应用、做什么、玩多久；``interaction`` 表示开启了用她自己的账号互动。"""
    recent_text = "\n".join(
        f"- {record.started_at[5:16].replace('T', ' ')} {record.app}：{record.goal}（{record.summary}）"
        for record in recent
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
    return (
        f"{_identity(persona)}\n\n"
        f"现在是 {now:%Y-%m-%d %H:%M}（{_WEEKDAYS[now.weekday()]}），你有一点空闲，打算拿起手机玩一会儿。\n"
        f"手机上可以打开的应用：\n{format_apps(apps)}\n\n"
        f"最近玩过：\n{recent_text or '（还没玩过）'}\n\n"
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
        "点关闭、拒绝、跳过、下次再说或返回。弹窗挡在前面时（屏幕中间一个框、后面变暗），后面的东西都点不到，"
        "要先关掉弹窗；界面元素列表里可能还列着弹窗后面的按钮，别去点它们。"
        "应用刚打开时的开屏广告（角上有「跳过」和倒计时）几秒后会自己消失，用 wait 等几秒就好，"
        "别去点，点到广告会跳到别的应用。",
        "一直记着这次想做的事。当前画面和它无关时（比如还停在别的视频、别的搜索结果里），"
        "先返回或回到应用首页，去搜索或找相关内容，不要被眼前的画面带跑。",
        "按自己的兴趣挑内容：首页、推荐里没有感兴趣的，就用搜索找你想看的。"
        "找到喜欢的视频、歌曲、文章，用 wait 停下来看一会儿；"
        "点开后发现不感兴趣（带货、广告、营销号、和你的兴趣不沾边的），别硬看：短视频直接划走，长视频、笔记、文章返回列表换一个。"
        "玩够了或想结束时用 finish。",
        "分清短视频和长视频：短视频是竖着占满整个屏幕、一条接一条的（抖音、B 站竖屏视频），上滑就是下一条；"
        "长视频是点开后上面播放器、下面简介和评论的页面（B 站点开的普通视频大多是这种），"
        "上滑只会翻简介和评论，不会换视频。长视频想看就用 wait 多看一会儿；"
        "不想看了就返回到视频列表（首页、搜索结果）点别的，或者点下面推荐的视频。",
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
    '{"action":"wait","seconds":20}：停下来看一会儿（1~120 秒。看视频一次 20~30 秒，不用每隔几秒就看一眼；'
    "在游戏房间里等人进来、等开局、等匹配时可以一次等 60~120 秒，有人进来、开局了或聊天里有新要求会提前叫醒你；"
    "轮到你操作、有倒计时的时候别等这么久）\n"
)
_WAIT_TURN_GUIDE = '{"action":"wait_turn"}：等对方操作（对方操作完、画面变了才继续，等待时不用你做什么）\n'
_ACTION_OUTRO = (
    '{"action":"finish"}：结束这次玩手机。这一步的 summary 不要写「结束」，改用一两句话写这次看到了什么、有什么感受，'
    "例如「看了好几只小奶猫，那只恐高的最呆」\n"
    "每个 JSON 还必须包含：\n"
    '"thought"：你此刻真实的内心想法，一句话，用你自己的口吻；\n'
    '"summary"：这一步在做什么，十个字左右，例如「点开猫猫视频」（finish 除外，见上）。\n'
    '示例：{"thought":"这只橘猫好圆，点开看看","summary":"点开猫猫视频","action":"tap","element":5}'
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
        f"{_WAIT_TURN_GUIDE if context.together else ''}{_ACTION_OUTRO}"
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
    request_text = ""
    if context.earlier_requests:
        request_text = f"对方这次在聊天里提过：{'；'.join(context.earlier_requests)}。还没做完的接着做。\n"
    if context.requests:
        request_text += (
            f"对方刚在聊天里让你：{'；'.join(context.requests)}。"
            "这比原来的打算优先，先照着做；做不到就在 thought 里说为什么。\n"
        )
    state_text = (
        # 目标在系统提示里离截图太远，模型容易只顾眼前画面，这里紧挨着截图再说一次
        f"这次想做的事：{context.goal}\n"
        f"{request_text}"
        f"当前时间：{context.now:%H:%M}\n"
        f"已经玩了 {context.elapsed_minutes:.0f} 分钟（打算玩 {context.max_minutes} 分钟），"
        f"这是第 {context.step}/{context.max_steps} 步。\n"
        f"当前前台：{context.foreground or '无法判断'}\n"
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
