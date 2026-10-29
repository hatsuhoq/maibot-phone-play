import pytest

from phone_play.ui_tree import parse_uiautomator_xml, parse_wda_xml

SAMPLE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" clickable="false" text="" content-desc="">
    <node class="android.widget.LinearLayout" bounds="[0,1000][1080,1200]" clickable="true" text="" content-desc="">
      <node class="android.widget.TextView" bounds="[40,1040][600,1100]" clickable="false" text="超可爱的橘猫" />
    </node>
    <node class="android.widget.ImageView" bounds="[960,120][1040,200]" clickable="true" text="" content-desc="搜索" />
    <node class="android.widget.TextView" bounds="[40,300][500,360]" clickable="false" text="推荐   热门" />
    <node class="android.view.View" bounds="[0,500][4,504]" clickable="true" text="" content-desc="太小" />
    <node class="android.widget.Button" bounds="[0,2500][200,2600]" clickable="true" text="屏幕外" content-desc="" />
  </node>
</hierarchy>"""


def test_parse_keeps_clickable_and_labeled_elements_in_reading_order():
    elements = parse_uiautomator_xml(SAMPLE, 1080, 2400, limit=10)
    assert [(element.index, element.label, element.clickable) for element in elements] == [
        (1, "搜索", True),
        (2, "推荐 热门", False),
        (3, "超可爱的橘猫", True),
    ]
    assert elements[0].center == (1000, 160)
    assert elements[2].describe() == "[3] LinearLayout 超可爱的橘猫（可点）"


def test_parse_respects_limit():
    assert len(parse_uiautomator_xml(SAMPLE, 1080, 2400, limit=1)) == 1


def test_parse_invalid_xml_raises():
    with pytest.raises(ValueError, match="无法解析"):
        parse_uiautomator_xml("ERROR: could not get idle state.", 1080, 2400, limit=10)


WDA_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="哔哩哔哩" visible="true" accessible="false"
    x="0" y="0" width="390" height="844">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" visible="true" accessible="false"
      x="0" y="0" width="390" height="844">
    <XCUIElementTypeButton type="XCUIElementTypeButton" name="search_btn" label="搜索" visible="true" accessible="true"
        x="340" y="50" width="40" height="40"/>
    <XCUIElementTypeTextField type="XCUIElementTypeTextField" placeholderValue="搜索你感兴趣的" visible="true"
        accessible="true" x="60" y="50" width="270" height="36"/>
    <XCUIElementTypeCell type="XCUIElementTypeCell" visible="true" accessible="false"
        x="0" y="300" width="390" height="120">
      <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="超可爱的橘猫" visible="true" accessible="true"
          x="16" y="320" width="300" height="20"/>
    </XCUIElementTypeCell>
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="推荐   热门" visible="true" accessible="true"
        x="16" y="100" width="200" height="30"/>
    <XCUIElementTypeOther type="XCUIElementTypeOther" label="视频卡片 点赞 1万" visible="true" accessible="false"
        x="0" y="450" width="390" height="200"/>
    <XCUIElementTypeOther type="XCUIElementTypeOther" label="直播中" visible="true" accessible="true"
        x="16" y="520" width="80" height="24"/>
    <XCUIElementTypeButton type="XCUIElementTypeButton" label="藏起来的" visible="false" accessible="true"
        x="16" y="760" width="80" height="40"/>
    <XCUIElementTypeKeyboard type="XCUIElementTypeKeyboard" visible="true" x="0" y="560" width="390" height="284">
      <XCUIElementTypeKey type="XCUIElementTypeKey" label="q" visible="true" accessible="true"
          x="3" y="570" width="35" height="45"/>
    </XCUIElementTypeKeyboard>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>"""


def test_parse_wda_converts_points_and_skips_noise():
    elements = parse_wda_xml(WDA_SAMPLE, 1170, 2532, scale=3, limit=10)
    assert [element.describe() for element in elements] == [
        "[1] TextField 搜索你感兴趣的（可点）",
        "[2] Button 搜索（可点）",
        "[3] StaticText 推荐 热门（文字）",
        "[4] Cell 超可爱的橘猫（可点）",
        "[5] Other 直播中（可点）",
    ]
    assert elements[1].bounds == (1020, 150, 1140, 270)
    assert elements[3].bounds == (0, 900, 1170, 1260)


# 插件实际拿到的界面树：不带 visible、accessible（太慢），有 traits
WDA_TRAITS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="设置" x="0" y="0" width="390" height="844"
    traits="">
  <XCUIElementTypeOther type="XCUIElementTypeOther" label="亮度" x="0" y="100" width="390" height="40" traits="Header"/>
  <XCUIElementTypeOther type="XCUIElementTypeOther" label="关注" x="300" y="150" width="70" height="30"
      traits="Button"/>
  <XCUIElementTypeOther type="XCUIElementTypeOther" label="视频卡片" x="0" y="150" width="280" height="30" traits=""/>
  <XCUIElementTypeCell type="XCUIElementTypeCell" label="每周、屏幕使用时间" x="0" y="200" width="390" height="60"
      traits="">
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="每周" x="56" y="210" width="40" height="20"
        traits="StaticText"/>
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="屏幕使用时间" x="56" y="232" width="90"
        height="18" traits="StaticText"/>
    <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" label="每周" value="1" x="310" y="214" width="51" height="31"
        traits="ToggleButton, Button"/>
  </XCUIElementTypeCell>
  <XCUIElementTypeCell type="XCUIElementTypeCell" x="0" y="300" width="390" height="46" traits="">
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="夜览" x="16" y="312" width="40" height="22"
        traits="StaticText"/>
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="关闭" x="300" y="312" width="40" height="22"
        traits="StaticText"/>
  </XCUIElementTypeCell>
  <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="0" x="310" y="400" width="51" height="31"
      traits="ToggleButton, Button"/>
  <XCUIElementTypeOther type="XCUIElementTypeOther" label="垂直滚动条, 3页" x="380" y="100" width="10" height="700"
      traits="Adjustable"/>
  <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="滚到屏幕外了" x="16" y="900" width="200"
      height="22" traits="StaticText"/>
</XCUIElementTypeApplication>"""


def test_parse_wda_uses_traits_and_skips_text_already_in_cells():
    elements = parse_wda_xml(WDA_TRAITS_SAMPLE, 1170, 2532, scale=3, limit=20)
    assert [element.describe() for element in elements] == [
        "[1] Other 亮度（文字）",
        "[2] Other 关注（可点）",
        "[3] Cell 每周、屏幕使用时间（可点）",
        "[4] Switch 每周 开（可点）",
        "[5] Cell 夜览（可点）",
        "[6] StaticText 关闭（文字）",
        "[7] Switch 关（可点）",
    ]


# 真机上点开设置的搜索框后的样子（精简）：搜索结果列表盖住设置首页，键盘盖住屏幕下半部分。
# 导航栏在界面树里排在两个列表前面，但画在它们上面
WDA_OVERLAY_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="设置" x="0" y="0" width="390" height="844">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="390" height="844">
    <XCUIElementTypeNavigationBar type="XCUIElementTypeNavigationBar" x="0" y="47" width="390" height="52">
      <XCUIElementTypeSearchField type="XCUIElementTypeSearchField" label="搜索" value="蓝牙" x="16" y="55"
          width="313" height="36" traits="SearchField"/>
      <XCUIElementTypeButton type="XCUIElementTypeButton" label="取消" x="340" y="57" width="34" height="30"
          traits="Button"/>
    </XCUIElementTypeNavigationBar>
    <XCUIElementTypeCollectionView type="XCUIElementTypeCollectionView" x="0" y="0" width="390" height="844">
      <XCUIElementTypeCell type="XCUIElementTypeCell" label="飞行模式" x="16" y="339" width="358" height="45"/>
      <XCUIElementTypeCell type="XCUIElementTypeCell" label="通用" x="16" y="595" width="358" height="44"/>
    </XCUIElementTypeCollectionView>
    <XCUIElementTypeCollectionView type="XCUIElementTypeCollectionView" x="0" y="0" width="390" height="844">
      <XCUIElementTypeCell type="XCUIElementTypeCell" label="蓝牙、蓝牙" x="0" y="99" width="390" height="61"/>
      <XCUIElementTypeCell type="XCUIElementTypeCell" label="蓝牙电源" x="0" y="160" width="390" height="61"/>
      <XCUIElementTypeCell type="XCUIElementTypeCell" label="被键盘挡住的结果" x="0" y="560" width="390" height="61"/>
      <XCUIElementTypeCollectionView type="XCUIElementTypeCollectionView" x="0" y="221" width="390" height="100">
        <XCUIElementTypeCell type="XCUIElementTypeCell" label="横向列表" x="0" y="221" width="120" height="100"/>
      </XCUIElementTypeCollectionView>
    </XCUIElementTypeCollectionView>
  </XCUIElementTypeWindow>
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="390" height="844">
    <XCUIElementTypeKeyboard type="XCUIElementTypeKeyboard" x="0" y="553" width="390" height="233">
      <XCUIElementTypeKey type="XCUIElementTypeKey" label="q" x="3" y="560" width="35" height="45"/>
    </XCUIElementTypeKeyboard>
    <XCUIElementTypeButton type="XCUIElementTypeButton" label="听写" x="313" y="786" width="69" height="58"
        traits="Button"/>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>"""


# 真机 B 站首页（精简）：标签页是没有特征的 Other，卡片没有说明，按钮的说明是图片资源名
WDA_BILIBILI_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="bilibili" accessible="false" x="0" y="0"
    width="390" height="844">
  <XCUIElementTypeOther type="XCUIElementTypeOther" label="推荐" accessible="true" traits="" x="146" y="91" width="41"
      height="36"/>
  <XCUIElementTypeCell type="XCUIElementTypeCell" accessible="false" traits="" x="6" y="391" width="184" height="224">
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="1297" accessible="true" traits="StaticText"
        x="30" y="511" width="30" height="14"/>
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="鹈鹕的悲伤是水做的" accessible="true"
        traits="StaticText" x="14" y="540" width="164" height="21"/>
    <XCUIElementTypeButton type="XCUIElementTypeButton" label="Queybee" accessible="true" traits="Button" x="34"
        y="589" width="49" height="17"/>
  </XCUIElementTypeCell>
  <XCUIElementTypeButton type="XCUIElementTypeButton" label="icon inline volume on" value="1" accessible="true"
      traits="Button" x="342" y="311" width="40" height="37"/>
  <XCUIElementTypeOther type="XCUIElementTypeOther" label="电池电量：100%" accessible="true" traits="Header"
      x="336" y="19" width="28" height="14"/>
</XCUIElementTypeApplication>"""


def test_parse_wda_reads_custom_tabs_and_card_titles():
    elements = parse_wda_xml(WDA_BILIBILI_SAMPLE, 1170, 2532, scale=3, limit=20)
    assert [element.describe() for element in elements] == [
        "[1] Other 电池电量：100%（文字）",
        "[2] Other 推荐（可点）",
        # 资源名和 0/1 状态都不当说明
        "[3] Button（可点）",
        # 卡片用最长的一段文字（标题）当说明，不用排在前面的播放量
        "[4] Cell 鹈鹕的悲伤是水做的（可点）",
        "[5] StaticText 1297（文字）",
        "[6] Button Queybee（可点）",
    ]


def test_parse_wda_drops_content_under_overlay_list_and_keyboard():
    elements = parse_wda_xml(WDA_OVERLAY_SAMPLE, 1170, 2532, scale=3, limit=20)
    assert [element.describe() for element in elements] == [
        "[1] SearchField 搜索（可点）",
        "[2] Button 取消（可点）",
        "[3] Cell 蓝牙、蓝牙（可点）",
        "[4] Cell 蓝牙电源（可点）",
        "[5] Cell 横向列表（可点）",
        "[6] Button 听写（可点）",
    ]


# 真机 B 站评论区（精简）：评论报回来的坐标全是列表的左上角，叠在一起占满名额
WDA_COLLAPSED_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="bilibili" x="0" y="0" width="390" height="844">
  <XCUIElementTypeTable type="XCUIElementTypeTable" x="0" y="306" width="390" height="539">
    <XCUIElementTypeOther type="XCUIElementTypeOther" label="昨天刚刚过世" accessible="true" traits="" x="0" y="306"
        width="322" height="24"/>
    <XCUIElementTypeButton type="XCUIElementTypeButton" label="灭爪" accessible="true" traits="Button" x="0" y="306"
        width="26" height="19"/>
    <XCUIElementTypeOther type="XCUIElementTypeOther" label="非常可爱。" accessible="true" traits="" x="0" y="306"
        width="322" height="24"/>
    <XCUIElementTypeOther type="XCUIElementTypeOther" label="置顶 大家都想要的同款" accessible="true" traits=""
        x="56" y="380" width="322" height="42"/>
  </XCUIElementTypeTable>
  <XCUIElementTypeOther type="XCUIElementTypeOther" label="只是一直在等你而已" accessible="true" traits=""
      x="21" y="777" width="314" height="19"/>
</XCUIElementTypeApplication>"""


def test_parse_wda_drops_elements_collapsed_onto_the_list_corner():
    elements = parse_wda_xml(WDA_COLLAPSED_SAMPLE, 1170, 2532, scale=3, limit=20)
    assert [element.label for element in elements] == ["置顶 大家都想要的同款", "只是一直在等你而已"]


def test_parse_wda_invalid_xml_raises():
    with pytest.raises(ValueError, match="无法解析"):
        parse_wda_xml("<XCUIElementTypeApplication", 1170, 2532, scale=3, limit=10)
