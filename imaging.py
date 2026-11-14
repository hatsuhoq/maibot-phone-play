"""截图解码、缩放、编号框绘制与编码。

这里的函数都是同步 CPU 计算，调用方应放进 ``asyncio.to_thread`` 执行。
"""

from io import BytesIO
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

import base64

from .ui_tree import Bounds, UIElement

_MARK_COLORS = ("#ff3b30", "#007aff", "#34c759", "#ff9500", "#af52de")
_MIN_JPEG_QUALITY = 30
_SIGNATURE_SIZE = (24, 24)
# 屏幕关着时截图是纯黑的（实测 iPhone 每个像素都是 0），留一点余量
_DARK_MAX = 8
# 几乎全黑：缩小后比这个亮的像素不到这么多。实测 OPPO 熄屏时截出来是黑的，只有状态栏一条红色电量边，
# 最亮的像素并不暗，按「最亮的像素」认不出来
_MOSTLY_DARK_SIZE = (60, 130)
_MOSTLY_DARK_LEVEL = 40
_MOSTLY_DARK_SHARE = 0.03


def open_image(data: bytes) -> Image.Image:
    """把截图字节解码为 RGB 图像。"""
    with Image.open(BytesIO(data)) as image:
        return image.convert("RGB")


def is_dark_screen(data: bytes) -> bool:
    """截图是不是全黑（屏幕关着）：亮着的屏幕至少有状态栏的时间、电量，最亮的像素不会这么暗。"""
    with Image.open(BytesIO(data)) as image:
        return image.convert("L").getextrema()[1] <= _DARK_MAX


def mostly_dark(image: Image.Image) -> bool:
    """截图几乎全黑：亮的地方不到 3%（熄屏、锁屏时的黑图，顶多剩状态栏一点点）。

    深色模式的页面、视频黑场也可能算进来，所以只当作「可能是屏幕关着」，要不要当真还得问手机屏幕的状态。
    """
    small = image.convert("L").resize(_MOSTLY_DARK_SIZE)
    bright = sum(small.point(lambda value: 255 if value > _MOSTLY_DARK_LEVEL else 0).histogram()[255:])
    return bright <= _MOSTLY_DARK_SHARE * _MOSTLY_DARK_SIZE[0] * _MOSTLY_DARK_SIZE[1]


def screen_signature(image: Image.Image) -> bytes:
    """缩成很小的灰度图，用来粗略判断两步之间画面有没有变化。"""
    return image.convert("L").resize(_SIGNATURE_SIZE, Image.Resampling.BILINEAR).tobytes()


def signature_distance(first: bytes, second: bytes) -> float:
    """两张缩略图逐像素差值的平均（0~255）；转圈加载、时钟跳动这类小变化通常不到 1。"""
    return sum(abs(a - b) for a, b in zip(first, second, strict=True)) / len(first)


def region_distance(first: Image.Image, second: Image.Image, bounds: Bounds) -> float:
    """两张同样大小的截图里同一块区域的差别，算法同 ``signature_distance``。"""
    return signature_distance(screen_signature(first.crop(bounds)), screen_signature(second.crop(bounds)))


def resize_to_edge(image: Image.Image, max_edge: int) -> Image.Image:
    """等比缩小到最长边不超过 ``max_edge``，不放大。"""
    width, height = image.size
    scale = max_edge / max(width, height)
    if scale >= 1:
        return image
    return image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS)


def encode_jpeg(image: Image.Image, max_edge: int, quality: int, max_bytes: Optional[int] = None) -> bytes:
    """编码为 JPEG；给定 ``max_bytes`` 时逐步降低质量和尺寸直到不超限。

    Raises:
        ValueError: 缩到很小仍然超限时抛出。
    """
    resized = resize_to_edge(image, max_edge)
    current_quality = quality
    for _ in range(20):
        buffer = BytesIO()
        resized.save(buffer, "JPEG", quality=current_quality, optimize=True)
        data = buffer.getvalue()
        if max_bytes is None or len(data) <= max_bytes:
            return data
        if current_quality > _MIN_JPEG_QUALITY:
            current_quality = max(_MIN_JPEG_QUALITY, current_quality - 10)
        else:
            # 质量已经降到底，改为缩小尺寸
            resized = resize_to_edge(resized, int(max(resized.size) * 0.8))
    raise ValueError(f"截图压缩后仍超过 {max_bytes} 字节")


def draw_element_marks(image: Image.Image, elements: Sequence[UIElement]) -> Image.Image:
    """在截图副本上画出元素边框和编号，帮助视觉模型对应元素列表。"""
    marked = image.copy()
    draw = ImageDraw.Draw(marked)
    font_size = max(16, min(marked.size) // 28)
    font = ImageFont.load_default(size=font_size)
    line_width = max(2, font_size // 8)
    padding = 3
    for element in elements:
        color = _MARK_COLORS[element.index % len(_MARK_COLORS)]
        draw.rectangle(element.bounds, outline=color, width=line_width)
        origin = (element.bounds[0] + padding, element.bounds[1] + padding)
        left, top, right, bottom = draw.textbbox(origin, str(element.index), font=font)
        draw.rectangle((left - padding, top - padding, right + padding, bottom + padding), fill=color)
        draw.text(origin, str(element.index), fill="white", font=font)
    return marked


def panel_png_base64(data: bytes, box: tuple[int, int] = (480, 640), max_chars: int = 1_000_000) -> str:
    """桌宠面板里的截图：整张屏幕居中放进 ``box`` 大小的透明画布，桌宠只收 base64 PNG（不超过 100 万字符）。

    桌宠面板里图片按面板宽度铺满、图片框最高 300 像素，竖长的手机截图只能露出一截要滚动；
    放进接近图片框比例的画布后一眼就能看到整个屏幕，点开放大再看细节。超限时整体缩小。

    Raises:
        ValueError: 缩到很小仍然超限时抛出。
    """
    image = open_image(data).convert("RGB")
    width, height = box
    for _ in range(10):
        fitted = image.copy()
        fitted.thumbnail((width, height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        canvas.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
        buffer = BytesIO()
        canvas.save(buffer, "PNG", optimize=True)
        encoded = to_base64(buffer.getvalue())
        if len(encoded) <= max_chars:
            return encoded
        width, height = int(width * 0.8), int(height * 0.8)
    raise ValueError(f"截图转成 PNG 后仍超过 {max_chars} 个字符")


def to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def jpeg_data_url(data: bytes) -> str:
    return f"data:image/jpeg;base64,{to_base64(data)}"
