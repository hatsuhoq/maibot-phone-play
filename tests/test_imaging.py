from io import BytesIO

import base64
import random

from PIL import Image

from phone_play.imaging import panel_png_base64

from conftest import make_png


def decode(image: str) -> Image.Image:
    raw = base64.b64decode(image, validate=True)
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    return Image.open(BytesIO(raw))


def test_whole_phone_screen_fits_the_panel_picture():
    # 竖长的 iPhone 截图整张放进 480×640 的透明画布正中，两边留空，不用在面板里滚动
    image = decode(panel_png_base64(make_png(1170, 2532, "#336699"))).convert("RGBA")
    assert image.size == (480, 640)
    assert image.getpixel((240, 320)) == (0x33, 0x66, 0x99, 255)
    assert image.getpixel((5, 320))[3] == 0 and image.getpixel((474, 320))[3] == 0
    # 横屏截图（游戏）也一样整张放进去，上下留空
    wide = decode(panel_png_base64(make_png(2532, 1170))).convert("RGBA")
    assert wide.size == (480, 640) and wide.getpixel((240, 5))[3] == 0


def test_panel_png_shrinks_until_the_desktop_pet_accepts_it():
    # 全是噪点的截图压不小：缩到 base64 不超过 100 万字符为止
    rng = random.Random(0)
    noise = Image.frombytes("RGB", (1170, 2532), bytes(rng.getrandbits(8) for _ in range(1170 * 2532 * 3)))
    buffer = BytesIO()
    noise.save(buffer, "JPEG", quality=95)
    encoded = panel_png_base64(buffer.getvalue(), box=(1200, 1600))
    assert len(encoded) <= 1_000_000
    width, height = decode(encoded).size
    assert width < 1200 and width * height <= 4_000_000


def test_mostly_dark_catches_a_black_screenshot_with_a_bit_of_status_bar():
    from PIL import ImageDraw

    from phone_play.imaging import mostly_dark

    assert mostly_dark(Image.new("RGB", (1080, 2400), "black"))
    # 实测 OPPO 熄屏时截出来是黑的，顶上只剩一条红色电量边：按「最亮的像素」认不出来
    edge = Image.new("RGB", (1080, 2400), "black")
    ImageDraw.Draw(edge).rectangle((900, 20, 1060, 50), fill="#ff3b30")
    assert mostly_dark(edge)
    assert not mostly_dark(Image.new("RGB", (1080, 2400), "#336699"))
    # 深色页面上有一块块白字：不算黑屏
    page = Image.new("RGB", (1080, 2400), "black")
    draw = ImageDraw.Draw(page)
    for top in range(200, 2200, 300):
        draw.rectangle((60, top, 1000, top + 40), fill="white")
    assert not mostly_dark(page)
