import pytest

from phone_play.actions import ActionError, find_app, parse_action, resolve_point, swipe_points
from phone_play.settings import AllowedApp
from phone_play.ui_tree import UIElement


def test_parse_tap_by_element_with_code_fence_and_extra_text():
    action = parse_action(
        '好的：\n```json\n{"thought":"点开看看","summary":"点开视频","action":"tap","element":3}\n```'
    )
    assert action.kind == "tap"
    assert action.element == 3
    assert action.thought == "点开看看"
    assert action.describe() == "点击 [3]"


def test_parse_tap_by_point():
    action = parse_action('{"action":"tap","point":[500,250.5]}')
    assert (action.x, action.y) == (500.0, 250.5)
    assert action.describe() == "点击 (500, 250.5)"


@pytest.mark.parametrize(
    "response,message",
    [
        ("我想看猫猫", "没有得到有效的动作"),
        ('{"action":"fly"}', "不认识的动作"),
        ('{"action":"tap"}', "需要 element"),
        ('{"action":"tap","point":[1200,10]}', "0~1000"),
        ('{"action":"tap","point":["500",10]}', r"\[x, y\] 两个数字"),
        ('{"action":"tap","point":[500]}', r"\[x, y\] 两个数字"),
        # Qwen-VL 分开写 x、y 时漏掉 y 键名的真实输出
        ('{"action":"tap","x":933,910}', "JSON 无法解析"),
        ('{"action":"tap","element":true}', "整数编号"),
        ('{"action":"swipe","direction":"forward"}', "direction"),
        ('{"action":"type","text":"  "}', "需要 text"),
        ('{"action":"launch"}', "需要 app"),
        ('{"action":"place","cell":"H8"}', "不认识的动作"),
        ('{"action":"like"}', "需要 element"),
        ('{"action":"follow"}', "需要 element"),
        ('{"action":"comment","element":1,"text":" "}', "comment 需要 text"),
        ('{"action":"comment","text":"好圆"}', "评论输入框"),
    ],
)
def test_parse_rejects_invalid_actions(response, message):
    with pytest.raises(ActionError, match=message):
        parse_action(response)


def test_parse_wait_turn():
    assert parse_action('{"action":"wait_turn"}').describe() == "等对方"


def test_parse_interaction_actions():
    assert parse_action('{"action":"like","element":4}').describe() == "点赞 [4]"
    assert parse_action('{"action":"follow","point":[900,450]}').describe() == "关注 (900, 450)"
    assert parse_action('{"action":"comment","element":4,"text":"好圆"}').describe() == "评论「好圆」"


def test_wait_seconds_are_clamped():
    assert parse_action('{"action":"wait","seconds":300}').seconds == 120
    assert parse_action('{"action":"wait"}').seconds == 1


def test_resolve_point_uses_element_center_or_relative_coordinates():
    elements = [UIElement(index=1, label="搜索", kind="Button", clickable=True, bounds=(100, 200, 300, 400))]
    assert resolve_point(parse_action('{"action":"tap","element":1}'), elements, 1080, 2400) == (200, 300)
    assert resolve_point(parse_action('{"action":"tap","point":[500,500]}'), elements, 1080, 2400) == (540, 1200)
    assert resolve_point(parse_action('{"action":"tap","point":[1000,1000]}'), [], 1080, 2400) == (1079, 2399)
    with pytest.raises(ActionError, match=r"\[9\] 不存在"):
        resolve_point(parse_action('{"action":"tap","element":9}'), elements, 1080, 2400)


def test_swipe_up_moves_finger_up_from_center():
    x1, y1, x2, y2 = swipe_points(parse_action('{"action":"swipe","direction":"up"}'), 1000, 2000)
    assert (x1, y1) == (500, 1000)
    assert x2 == 500 and y2 < y1


def test_swipe_end_point_stays_on_screen():
    _, _, x2, _ = swipe_points(parse_action('{"action":"swipe","direction":"right","point":[900,500]}'), 1000, 2000)
    assert x2 == 998


def test_find_app_by_package_or_name():
    apps = [AllowedApp(package="tv.danmaku.bili", name="哔哩哔哩")]
    assert find_app(apps, "TV.DANMAKU.BILI") is apps[0]
    assert find_app(apps, "哔哩哔哩") is apps[0]
    assert find_app(apps, "com.tencent.mm") is None
