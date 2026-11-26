"""认出现在是哪种页面，好按页面告诉她该怎么看、手机上实际是什么状态。

B 站的两种视频页：同一个视频从不同入口点开，页面不一样，操作也不一样。
- 竖屏播放器（首页推荐点开）：整屏一格，上滑换下一个推荐视频；
- 视频详情页（搜索结果、相关推荐、UP 主主页点开）：上面播放器，下面「简介」「评论」，上滑只翻简介和评论。
网易云音乐的播放页（在放哪首、在放还是暂停了、放到哪）和番茄小说的阅读页（哪一章、第几页、菜单开没开），
用来确认「歌开始放了」「翻到下一页了」这类结果，不用她猜。只在 2026 年 11 月的 iPhone 上验证过。
按界面元素认；读不到界面元素、或者不像这几种时返回空字符串，让她自己看截图。
"""

from typing import Dict, List, Optional, Sequence, Tuple

import re

from .ui_tree import UIElement

# B 站各个版本的包名：国内版安卓、iPhone（App Store 里的「哔哩哔哩」）、国际版、HD 版
BILIBILI_PACKAGES = frozenset(
    {"tv.danmaku.bili", "tv.danmaku.bilianime", "com.bilibili.inter", "tv.danmaku.bilibilihd"}
)
BILI_PLAYER = "bili_player"
BILI_DETAIL = "bili_detail"
# 网易云音乐、番茄小说：iPhone 和安卓的包名一样
NETEASE_MUSIC_PACKAGES = frozenset({"com.netease.cloudmusic"})
FANQIE_PACKAGES = frozenset({"com.dragon.read"})
MUSIC_PLAYER = "music_player"
NOVEL_READER = "novel_reader"
PAGE_LABELS: Dict[str, str] = {
    BILI_PLAYER: "B 站竖屏播放器",
    BILI_DETAIL: "B 站视频详情页",
    MUSIC_PLAYER: "网易云播放页",
    NOVEL_READER: "番茄小说阅读页",
}

# 实测 iPhone 上的 B 站（2026 年 11 月）竖屏播放器：UP 主名字、粉丝数、关注按钮、标题这几个元素的文字是占位名
_PLAYER_PLACEHOLDERS = frozenset({"title", "name", "fans", "follow"})
# 竖屏播放器整屏是一格；占位名哪天改了，就靠「整屏一格 + 多少人正在看」认
_FULL_SCREEN_RATIO = 0.9
# 详情页标题下面那一行：「16.4万播放,132条弹幕,发布时间2025年11月21日」
_DETAIL_STATS = re.compile(r"播放.*弹幕")
# 搜索结果卡片上 B 站自己标的「看过」
_WATCHED_MARKS = ("最近看过", "已看完")
# 安卓竖屏播放器的标题后面跟着播放数，中间可能夹着一个看不见的 \u200e：「离谱爆料之 爱音 将要加入sumimi？ 2.2万播放」
_PLAYER_TITLE = re.compile("^(.+?)[\\s\u200e]*[\\d.]+[万亿]?播放$")


# 网易云播放页（首页「心动」整屏播放和点开的播放页一样）：进度条、上一首、下一首，
# 中间的按钮在放时写「暂停」、停了写「播放」
_MUSIC_CONTROLS = frozenset({"上一首", "下一首"})
# 喜欢、评论按钮那一行左边就是「歌名,歌手」
_MUSIC_ROW_BUTTONS = ("添加到我喜欢的音乐", "查看评论")
_CLOCK = re.compile(r"^\d{1,2}:\d{2}$")
# 实测网易云的会员广告会盖住播放页的按钮（中间一个大框，或者底下弹上来一截），界面元素里播放页的按钮还在；
# 播放页自己的横幅只写「VIP¥0.04/天，无限畅听」，不带这些字
_MUSIC_POPUP_MARKS = ("立即开通", "立即续费", "开VIP无限听", "开会员无限畅听", "开通会员无限畅听")
# 番茄小说阅读页：最上面是章节名，最底下是「当前页/总页数」；书架上也有「第1章 ……」，但没有页数
_CHAPTER = re.compile(r"^第\s*\d+\s*章")
_PAGE_COUNTER = re.compile(r"^(\d+)/(\d+)$")
_READER_EDGE = 0.12
# 点画面中间调出来的菜单
_READER_MENU = frozenset({"目录", "下一章"})


def detect_page(package: str, elements: Sequence[UIElement], width: int, height: int) -> str:
    """现在是哪种页面：``BILI_PLAYER``、``BILI_DETAIL``、``MUSIC_PLAYER``、``NOVEL_READER``，认不出是空字符串。"""
    if not elements:
        return ""
    if package in NETEASE_MUSIC_PACKAGES:
        return MUSIC_PLAYER if _is_music_player(elements) else ""
    if package in FANQIE_PACKAGES:
        return NOVEL_READER if _reader_parts(elements, height) is not None else ""
    if package not in BILIBILI_PACKAGES:
        return ""
    labels = {element.label for element in elements if element.label}
    # 实测详情页：播放器下面一排「简介」「评论」切换（安卓上评论后面可能带着条数）
    if "简介" in labels and any(label.startswith("评论") for label in labels):
        return BILI_DETAIL
    if len(labels & _PLAYER_PLACEHOLDERS) >= 2:
        return BILI_PLAYER
    # 实测安卓上的竖屏播放器：没有整屏那一格、也没有占位名，右下角有「详情页」按钮，左上角写着多少人正在看
    if "详情页" in labels and any("人正在看" in label for label in labels):
        return BILI_PLAYER
    full_screen = any(
        (element.bounds[2] - element.bounds[0]) * (element.bounds[3] - element.bounds[1])
        >= width * height * _FULL_SCREEN_RATIO
        for element in elements
    )
    if full_screen and any("人正在看" in label for label in labels):
        return BILI_PLAYER
    return ""


def detail_title(elements: Sequence[UIElement]) -> str:
    """详情页上的视频标题：播放、弹幕数那一行正上方的那一格（文字可能被截短）；找不到是空字符串。

    详情页的标题是界面元素里的文字，插件自己就能记下看过哪个视频，不用等她在 seen 里写。
    竖屏播放器里标题那个元素的文字是占位名「title」，读不到。
    """
    stats = next((element for element in elements if _DETAIL_STATS.search(element.label)), None)
    if stats is None:
        return ""
    above = [
        element
        for element in elements
        if element is not stats
        and element.kind == "Cell"
        and element.label
        and not element.label.startswith("up主")
        and element.bounds[3] <= stats.bounds[1] + 10
    ]
    title = max(above, key=lambda element: element.bounds[3], default=None)
    return title.label if title is not None else ""


def player_title(elements: Sequence[UIElement]) -> str:
    """竖屏播放器上的视频标题（安卓上读得到，iPhone 上那个元素的文字是占位名「title」）；读不到是空字符串。"""
    for element in elements:
        match = _PLAYER_TITLE.match(element.label)
        if match and len(match.group(1).strip()) >= 2:
            return match.group(1).strip()
    return ""


def page_title(page: str, elements: Sequence[UIElement]) -> str:
    """当前视频页上读得到的标题：详情页、安卓竖屏播放器有，读不到是空字符串。"""
    if page == BILI_DETAIL:
        return detail_title(elements)
    if page == BILI_PLAYER:
        return player_title(elements)
    return ""


def _inside(card: UIElement, element: UIElement) -> bool:
    """元素的中心在卡片里面。"""
    x1, y1, x2, y2 = card.bounds
    cx = (element.bounds[0] + element.bounds[2]) / 2
    cy = (element.bounds[1] + element.bounds[3]) / 2
    return x1 <= cx < x2 and y1 <= cy < y2


def marked_watched(card: UIElement, elements: Sequence[UIElement]) -> Optional[str]:
    """卡片上有没有 B 站自己标的「最近看过」「已看完」，有就返回那个标记。"""
    return next(
        (element.label for element in elements if element.label in _WATCHED_MARKS and _inside(card, element)), None
    )


def card_texts(card: UIElement, elements: Sequence[UIElement]) -> List[str]:
    """卡片上读得到的文字：卡片自己的在前，后面是卡片里面从上到下的几行（不含「最近看过」这类标记）。

    实测 iPhone 上 B 站搜索结果的卡片（2026 年 11 月）：有的卡片能点的整张卡片的文字是一条热评
    （「某某：好棒的手书……」，卡片下半截还有一行一样的），标题是卡片里单独一行不能点的文字。
    只看卡片自己的文字就认不出是哪个视频。
    """
    texts = [card.label] if card.label else []
    for element in _inner(card, elements):
        if element.label not in texts:
            texts.append(element.label)
    return texts


def card_title(card: UIElement, elements: Sequence[UIElement]) -> str:
    """给她看的卡片名字：一般就是卡片自己的文字；卡片自己的文字是热评（卡片下半截有一行一样的）时，取卡片里最上面那行。"""
    inner = _inner(card, elements)
    middle = (card.bounds[1] + card.bounds[3]) / 2
    if not any(element.label == card.label and element.bounds[1] >= middle for element in inner):
        return card.label
    others = [element for element in inner if element.label != card.label]
    return min(others, key=lambda element: (element.bounds[1], element.bounds[0])).label if others else card.label


def _inner(card: UIElement, elements: Sequence[UIElement]) -> List[UIElement]:
    """卡片里面带文字的元素（不含卡片自己和「最近看过」这类标记），按从上到下的顺序。"""
    return [
        element
        for element in elements
        if element is not card and element.label and element.label not in _WATCHED_MARKS and _inside(card, element)
    ]


def _is_music_player(elements: Sequence[UIElement]) -> bool:
    """网易云播放页：有进度条、「上一首」「下一首」，中间的按钮是「暂停」或「播放」；只有底下一条迷你播放条的页面不算。"""
    labels = {element.label for element in elements if element.label}
    return (
        any(element.kind == "Slider" for element in elements)
        and _MUSIC_CONTROLS <= labels
        and bool(labels & {"暂停", "播放"})
    )


def _music_song(elements: Sequence[UIElement]) -> str:
    """喜欢、评论按钮那一行、整个在按钮左边的「歌名,歌手」；找不到是空字符串。

    同一行可能还有横跨整屏的广告字（「欢迎回来~开通会员无限畅听」），不整个在按钮左边，不算。
    """
    row = next((element for name in _MUSIC_ROW_BUTTONS for element in elements if element.label == name), None)
    if row is None:
        return ""
    candidates = [
        element
        for element in elements
        if element.label
        and element.kind != "Button"
        and element.bounds[2] <= row.bounds[0]
        and row.bounds[1] <= (element.bounds[1] + element.bounds[3]) / 2 <= row.bounds[3]
    ]
    song = max(candidates, key=lambda element: element.bounds[2] - element.bounds[0], default=None)
    return song.label if song is not None else ""


def _music_times(elements: Sequence[UIElement]) -> Optional[Tuple[str, str]]:
    """进度条正下方的（放到哪，一共多长）；读不到是 None。"""
    slider = next((element for element in elements if element.kind == "Slider"), None)
    if slider is None:
        return None
    gap = max(slider.bounds[3] - slider.bounds[1], 1) * 10
    times = sorted(
        (
            element
            for element in elements
            if _CLOCK.match(element.label) and slider.bounds[3] <= element.bounds[1] <= slider.bounds[3] + gap
        ),
        key=lambda element: element.bounds[0],
    )
    return (times[0].label, times[-1].label) if len(times) >= 2 else None


def _reader_parts(elements: Sequence[UIElement], height: int) -> Optional[Tuple[str, int, int]]:
    """阅读页最上面的章节名和最底下的页码（当前页，总页数）；不是阅读页时是 None。"""
    chapter = next(
        (
            element.label
            for element in elements
            if _CHAPTER.match(element.label) and element.bounds[3] <= height * _READER_EDGE
        ),
        None,
    )
    counter = next(
        (
            _PAGE_COUNTER.match(element.label)
            for element in elements
            if _PAGE_COUNTER.match(element.label) and element.bounds[1] >= height * (1 - _READER_EDGE)
        ),
        None,
    )
    if chapter is None or counter is None:
        return None
    return chapter, int(counter.group(1)), int(counter.group(2))


def page_state(page: str, elements: Sequence[UIElement], height: int) -> str:
    """认出来的页面上手机实际是什么样，给她确认「歌开始放了」「翻到下一页了」这类结果；不是这几种页面是空字符串。"""
    labels = {element.label for element in elements if element.label}
    if page == MUSIC_PLAYER:
        song = _music_song(elements)
        text = "网易云播放页：" + (f"在放的是「{song}」（歌名,歌手），" if song else "")
        text += "正在播放" if "暂停" in labels else "暂停着"
        times = _music_times(elements)
        if times is not None:
            text += f"（{times[0]}/{times[1]}）"
        if any(mark in label for label in labels for mark in _MUSIC_POPUP_MARKS):
            text += "。上面盖着会员广告，播放页的按钮点不到：先点广告右上角的 × 关掉，别点开通、续费"
        return text
    if page == NOVEL_READER:
        parts = _reader_parts(elements, height)
        if parts is None:
            return ""
        chapter, current, total = parts
        text = f"番茄小说阅读页：{chapter}，页码 {current}/{total}（翻过去就会变）"
        if _READER_MENU <= labels:
            text += "。菜单开着（上一章、下一章、目录），点画面中间收起"
        return text
    return ""
