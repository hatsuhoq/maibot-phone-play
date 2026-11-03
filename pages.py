"""认出现在是哪种页面，好按页面告诉她该怎么看。

目前只认 B 站的两种视频页：同一个视频从不同入口点开，页面不一样，操作也不一样。
- 竖屏播放器（首页推荐点开）：整屏一格，上滑换下一个推荐视频；
- 视频详情页（搜索结果、相关推荐、UP 主主页点开）：上面播放器，下面「简介」「评论」，上滑只翻简介和评论。
按界面元素认；读不到界面元素、或者不像这两种（首页、搜索结果）时返回空字符串，让她自己看截图。
"""

from typing import Dict, Optional, Sequence

import re

from .ui_tree import UIElement

# B 站各个版本的包名：国内版安卓、iPhone（App Store 里的「哔哩哔哩」）、国际版、HD 版
BILIBILI_PACKAGES = frozenset(
    {"tv.danmaku.bili", "tv.danmaku.bilianime", "com.bilibili.inter", "tv.danmaku.bilibilihd"}
)
BILI_PLAYER = "bili_player"
BILI_DETAIL = "bili_detail"
PAGE_LABELS: Dict[str, str] = {BILI_PLAYER: "B 站竖屏播放器", BILI_DETAIL: "B 站视频详情页"}

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


def detect_page(package: str, elements: Sequence[UIElement], width: int, height: int) -> str:
    """现在是哪种视频页：``BILI_PLAYER``、``BILI_DETAIL``，认不出是空字符串。"""
    if package not in BILIBILI_PACKAGES or not elements:
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


def marked_watched(card: UIElement, elements: Sequence[UIElement]) -> Optional[str]:
    """卡片上有没有 B 站自己标的「最近看过」「已看完」，有就返回那个标记。"""
    x1, y1, x2, y2 = card.bounds
    for element in elements:
        if element.label in _WATCHED_MARKS:
            cx = (element.bounds[0] + element.bounds[2]) / 2
            cy = (element.bounds[1] + element.bounds[3]) / 2
            if x1 <= cx < x2 and y1 <= cy < y2:
                return element.label
    return None
