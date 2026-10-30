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


def open_image(data: bytes) -> Image.Image:
    """把截图字节解码为 RGB 图像。"""
    with Image.open(BytesIO(data)) as image:
        return image.convert("RGB")


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


def to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def jpeg_data_url(data: bytes) -> str:
    return f"data:image/jpeg;base64,{to_base64(data)}"
