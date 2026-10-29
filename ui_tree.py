"""解析界面树（安卓 uiautomator、iPhone WebDriverAgent），挑出可以点击或带文字的元素。"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
from xml.etree import ElementTree

import re

Bounds = Tuple[int, int, int, int]
_Entry = Tuple[str, str, Bounds]

_BOUNDS_PATTERN = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_MIN_EDGE = 8
_LABEL_LIMIT = 30
_WDA_TYPE_PREFIX = "XCUIElementType"
# 这些类型本身就是拿来点的；其它类型只在带文字时作为「文字」列出
_WDA_CLICKABLE_KINDS = frozenset(
    {
        "Button",
        "Cell",
        "CheckBox",
        "Icon",
        "Link",
        "MenuItem",
        "PickerWheel",
        "SearchField",
        "SecureTextField",
        "SegmentedControl",
        "Slider",
        "Switch",
        "Tab",
        "TextField",
        "TextView",
        "Toggle",
    }
)
# 很多应用自己画的按钮类型是 Other，只能从辅助功能特征（traits）看出是拿来点的
_WDA_CLICKABLE_TRAITS = frozenset({"Button", "Link"})
# 开发者没写说明时，label 常是图片资源名（icon inline volume on 这类全小写的几个英文词），看不懂还会误导
_WDA_ASSET_NAME = re.compile(r"^[a-z0-9]+([ _-][a-z0-9]+)+$")
_WDA_TOGGLE_KINDS = frozenset({"CheckBox", "Switch", "Toggle"})
_WDA_TOGGLE_STATES: Dict[str, str] = {"0": "关", "1": "开"}
# 一个列表左上角叠着这么多不同的元素，就当作它们的坐标无效（见 _drop_collapsed）
_COLLAPSED_MIN = 3
# 用来判断遮挡的「层」：键盘和各种列表（见 _drop_hidden）
_WDA_LAYER_KINDS = frozenset({"CollectionView", "Keyboard", "ScrollView", "Table", "WebView"})
# 应用和窗口的说明是应用名，框住整个屏幕，列出来没有意义
_WDA_CONTAINER_KINDS = frozenset({"Application", "Window"})
_WDA_TEXT_KINDS = frozenset({"StaticText", "Image"})


@dataclass(frozen=True)
class UIElement:
    """一个可交互或带文字的界面元素，坐标为截图像素。"""

    index: int
    label: str
    kind: str
    clickable: bool
    bounds: Bounds

    @property
    def center(self) -> Tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2

    def describe(self) -> str:
        label = f" {self.label}" if self.label else ""
        action = "可点" if self.clickable else "文字"
        return f"[{self.index}] {self.kind}{label}（{action}）"


def _clean_label(text: str) -> str:
    return " ".join(text.split())[:_LABEL_LIMIT]


def _node_label(node: ElementTree.Element) -> str:
    return _clean_label((node.get("text") or "").strip() or (node.get("content-desc") or "").strip())


def _descendant_label(node: ElementTree.Element) -> str:
    for child in node.iter("node"):
        label = _node_label(child)
        if label:
            return label
    return ""


def _clip(left: int, top: int, right: int, bottom: int, width: int, height: int) -> Optional[Bounds]:
    # 裁到屏幕范围内，完全在屏幕外或过小的元素没有点击意义
    left, top = max(0, left), max(0, top)
    right, bottom = min(width, right), min(height, bottom)
    if right - left < _MIN_EDGE or bottom - top < _MIN_EDGE:
        return None
    return left, top, right, bottom


def _parse_bounds(raw: str, width: int, height: int) -> Optional[Bounds]:
    match = _BOUNDS_PATTERN.fullmatch(raw.strip())
    if match is None:
        return None
    left, top, right, bottom = (int(value) for value in match.groups())
    return _clip(left, top, right, bottom, width, height)


def _contains(outer: Bounds, inner: Bounds) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _parse_xml(xml_text: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"界面树 XML 无法解析：{exc}") from exc


def _number_elements(clickable: List[_Entry], labeled: List[_Entry], limit: int) -> List[UIElement]:
    """去掉重复的元素，按从上到下、从左到右排序并从 1 开始编号。"""
    # 已经被同名可点击容器覆盖的纯文字没必要重复列出
    labeled = [
        item
        for item in labeled
        if not any(item[0] == label and _contains(bounds, item[2]) for label, _, bounds in clickable)
    ]

    entries = [(label, kind, bounds, True) for label, kind, bounds in clickable]
    entries += [(label, kind, bounds, False) for label, kind, bounds in labeled]
    # 同一位置同一说明的元素只保留一个（嵌套的多层可点击容器很常见）
    unique: Dict[Tuple[str, Bounds], Tuple[str, str, Bounds, bool]] = {}
    for entry in entries:
        unique.setdefault((entry[0], entry[2]), entry)
    ordered = sorted(unique.values(), key=lambda entry: (entry[2][1], entry[2][0]))[:limit]
    return [
        UIElement(index=index, label=label, kind=kind, clickable=is_clickable, bounds=bounds)
        for index, (label, kind, bounds, is_clickable) in enumerate(ordered, start=1)
    ]


def parse_uiautomator_xml(xml_text: str, width: int, height: int, limit: int) -> List[UIElement]:
    """从 uiautomator dump 的 XML 中提取元素。

    Args:
        xml_text: ``uiautomator dump`` 输出的 XML。
        width: 截图宽度（像素）。
        height: 截图高度（像素）。
        limit: 最多返回多少个元素。

    Returns:
        List[UIElement]: 按从上到下、从左到右排序并编号（从 1 开始）的元素列表。

    Raises:
        ValueError: XML 无法解析时抛出。
    """
    root = _parse_xml(xml_text)
    clickable: List[_Entry] = []
    labeled: List[_Entry] = []
    for node in root.iter("node"):
        bounds = _parse_bounds(node.get("bounds") or "", width, height)
        if bounds is None:
            continue
        kind = (node.get("class") or "View").rsplit(".", 1)[-1]
        is_clickable = node.get("clickable") == "true" or node.get("long-clickable") == "true"
        if is_clickable:
            # 可点击的容器自身常常没有文字，用它内部第一个文字作为说明
            clickable.append((_node_label(node) or _descendant_label(node), kind, bounds))
            continue
        label = _node_label(node)
        if label:
            labeled.append((label, kind, bounds))
    return _number_elements(clickable, labeled, limit)


def _wda_kind(node: ElementTree.Element) -> str:
    return (node.get("type") or node.tag).removeprefix(_WDA_TYPE_PREFIX)


def _wda_label(node: ElementTree.Element) -> str:
    kind = _wda_kind(node)
    if kind in _WDA_TOGGLE_KINDS:
        # 开关的 value 是 0/1，换成开/关才看得懂
        state = _WDA_TOGGLE_STATES.get(node.get("value") or "", "")
        return _clean_label(f"{node.get('label') or ''} {state}")
    # name 常是开发者起的标识（类名之类），对看屏幕没有帮助，不拿来当说明
    for key in ("label", "value", "placeholderValue"):
        label = _clean_label(node.get(key) or "")
        if not label or _WDA_ASSET_NAME.match(label):
            continue
        if key == "value" and label in _WDA_TOGGLE_STATES and kind not in _WDA_TEXT_KINDS:
            # 按钮的 value 是 0/1 时表示选没选中，不是说明
            continue
        return label
    return ""


def _wda_bounds(node: ElementTree.Element, scale: float, width: int, height: int) -> Optional[Bounds]:
    try:
        x, y, w, h = (float(node.get(key) or "") for key in ("x", "y", "width", "height"))
    except ValueError:
        return None
    return _clip(round(x * scale), round(y * scale), round((x + w) * scale), round((y + h) * scale), width, height)


def _wda_traits(node: ElementTree.Element) -> Set[str]:
    return {trait.strip() for trait in (node.get("traits") or "").split(",") if trait.strip()}


@dataclass
class _WdaItem:
    """界面树里的一个元素，``start``/``end`` 是它和它整棵子树在文档里的先后位置。"""

    label: str
    kind: str
    bounds: Bounds
    clickable: bool
    start: int
    end: int = 0


def _area(bounds: Bounds) -> int:
    return (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])


def _overlap(first: Bounds, second: Bounds) -> int:
    width = min(first[2], second[2]) - max(first[0], second[0])
    height = min(first[3], second[3]) - max(first[1], second[1])
    return width * height if width > 0 and height > 0 else 0


def _collect_wda(
    node: ElementTree.Element,
    scale: float,
    size: Tuple[int, int],
    items: List[_WdaItem],
    layers: List[_WdaItem],
    order: int,
    covered: bool = False,
) -> int:
    """按文档顺序收集元素和能挡住别人的层，返回下一个节点的顺序号。"""
    start = order
    kind = _wda_kind(node)
    traits = _wda_traits(node)
    # 一般不让 WDA 算可见性（太慢），只按坐标裁掉屏幕外的；带了 visible 属性时也认
    shown = node.get("visible") != "false" and kind not in _WDA_CONTAINER_KINDS
    # 滚动条、轮播图页码这类可调节的 Other，说明是「垂直滚动条, 3页」之类，列出来没用
    shown = shown and not (kind == "Other" and "Adjustable" in traits)
    bounds = _wda_bounds(node, scale, *size) if shown else None
    if kind == "Keyboard":
        # 键盘按键有几十个，列出来只会挤掉真正有用的元素（输入文字用 type 操作）；键盘只当作挡住下面内容的层
        if bounds is not None:
            layers.append(_WdaItem("", kind, bounds, False, start, start))
        # 跳过整棵键盘子树，顺序号照样往后数
        return order + sum(1 for _ in node.iter())

    # 没有任何特征、却对辅助功能可见的 Other 多半是应用自己画的标签页、卡片（文字和标题都带特征）
    custom_control = kind == "Other" and not traits and node.get("accessible") == "true"
    is_clickable = kind in _WDA_CLICKABLE_KINDS or not traits.isdisjoint(_WDA_CLICKABLE_TRAITS) or custom_control
    own_label = _wda_label(node)
    item: Optional[_WdaItem] = None
    if bounds is not None:
        if is_clickable:
            # 没有文字的卡片用它里面最长的一段文字作说明，一般是标题（第一段常是播放量这类数字）
            label = own_label or max((text for text in map(_wda_label, node.iter()) if text), key=len, default="")
            item = _WdaItem(label, kind, bounds, True, start)
            items.append(item)
        elif kind in _WDA_LAYER_KINDS:
            item = _WdaItem("", kind, bounds, False, start)
            layers.append(item)
        elif own_label and not covered and (kind in _WDA_TEXT_KINDS or traits or node.get("accessible") == "true"):
            item = _WdaItem(own_label, kind, bounds, False, start)
            items.append(item)
    # 带文字的可点击元素（比如设置里的一行）已经把里面的文字说全了，里面的纯文字不再重复列出
    covered = covered or (is_clickable and bool(own_label) and bounds is not None)
    order += 1
    for child in node:
        order = _collect_wda(child, scale, size, items, layers, order, covered)
    if item is not None:
        item.end = order - 1
    return order


def _covers(upper: _WdaItem, lower: _WdaItem) -> bool:
    """``upper`` 排在 ``lower`` 整棵子树之后，并且盖住了它一大半。"""
    return upper.start > lower.end and _overlap(lower.bounds, upper.bounds) * 2 > _area(lower.bounds)


def _drop_hidden(items: List[_WdaItem], layers: List[_WdaItem]) -> List[_WdaItem]:
    """去掉被挡住的元素（不让 WDA 算可见性的代价，用两条可靠的规则近似）。

    界面树按阅读顺序排（导航栏总在最前），不能简单地认为排在后面的就画在上面，只认这两种：
    键盘在单独的窗口里，总在最上面，被它盖住的元素看不见；
    后面的列表盖住了前面的列表，比如搜索结果盖住设置首页、弹出的面板盖住视频列表，前面列表里的东西都看不见。
    """
    keyboards = [layer for layer in layers if layer.kind == "Keyboard"]
    lists = [layer for layer in layers if layer.kind != "Keyboard"]
    hidden_lists = [lower for lower in lists if any(_covers(upper, lower) for upper in lists)]
    return [
        item
        for item in items
        if not any(lower.start <= item.start <= lower.end for lower in hidden_lists)
        and not any(_covers(keyboard, item) for keyboard in keyboards)
    ]


def _drop_collapsed(items: List[_WdaItem], layers: List[_WdaItem]) -> List[_WdaItem]:
    """去掉坐标塌到列表左上角的元素。

    实测 B 站评论区里一串评论报回来的坐标全是列表的左上角（WDA 拿不到它们真实的位置），叠在一起占满了元素名额，
    底部真正要用的评论输入框反而列不出来。同一个列表里有好几个不同的元素都贴在它左上角时，当作坐标无效。
    """
    collapsed: Set[int] = set()
    for layer in layers:
        if layer.kind == "Keyboard":
            continue
        stuck = [
            item for item in items if layer.start < item.start <= layer.end and item.bounds[:2] == layer.bounds[:2]
        ]
        if len({item.label for item in stuck}) >= _COLLAPSED_MIN:
            collapsed.update(id(item) for item in stuck)
    return [item for item in items if id(item) not in collapsed]


def parse_wda_xml(xml_text: str, width: int, height: int, scale: float, limit: int) -> List[UIElement]:
    """从 WebDriverAgent ``/source`` 返回的 XML 中提取元素。

    Args:
        xml_text: ``/source`` 返回的 XML。
        width: 截图宽度（像素）。
        height: 截图高度（像素）。
        scale: 屏幕倍率；界面树的坐标单位是「点」，乘以倍率才是截图像素。
        limit: 最多返回多少个元素。

    Returns:
        List[UIElement]: 按从上到下、从左到右排序并编号（从 1 开始）的元素列表。

    Raises:
        ValueError: XML 无法解析时抛出。
    """
    root = _parse_xml(xml_text)
    items: List[_WdaItem] = []
    layers: List[_WdaItem] = []
    _collect_wda(root, scale, (width, height), items, layers, 0)
    shown = _drop_collapsed(_drop_hidden(items, layers), layers)
    clickable = [(item.label, item.kind, item.bounds) for item in shown if item.clickable]
    labeled = [(item.label, item.kind, item.bounds) for item in shown if not item.clickable]
    return _number_elements(clickable, labeled, limit)
