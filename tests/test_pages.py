from dataclasses import replace

from phone_play.pages import BILI_DETAIL, BILI_PLAYER, detail_title, detect_page, marked_watched, page_title

from conftest import BILI_DETAIL as DETAIL
from conftest import BILI_PLAYER as PLAYER
from conftest import BILI_ANDROID_SCREEN, BILI_PLAYER_ANDROID, BILI_SCREEN, BILI_SEARCH, BILI_SEARCH_COMMENT
from conftest import FANQIE_HISTORY, FANQIE_READER, FANQIE_READER_MENU
from conftest import NETEASE_HOME_PLAYER, NETEASE_MINE, NETEASE_PLAYER, NETEASE_POPUP_CENTER, NETEASE_POPUP_SHEET


def test_bilibili_video_pages_are_told_apart_by_their_elements():
    assert detect_page("tv.danmaku.bilianime", DETAIL, *BILI_SCREEN) == BILI_DETAIL
    assert detect_page("tv.danmaku.bilianime", PLAYER, *BILI_SCREEN) == BILI_PLAYER
    # 搜索结果、读不到元素、不是 B 站：认不出就不说
    assert detect_page("tv.danmaku.bilianime", BILI_SEARCH, *BILI_SCREEN) == ""
    assert detect_page("tv.danmaku.bilianime", [], *BILI_SCREEN) == ""
    assert detect_page("com.xingin.discover", DETAIL, *BILI_SCREEN) == ""


def test_player_is_still_recognised_if_the_placeholder_names_change():
    # 占位名哪天改了：整屏一格加上「多少人正在看」也能认出来
    renamed = [
        replace(element, label="") if element.label in ("name", "fans", "follow", "title") else element
        for element in PLAYER
    ]
    assert detect_page("tv.danmaku.bili", renamed, *BILI_SCREEN) == BILI_PLAYER


def test_detail_page_title_is_read_from_the_elements():
    assert detail_title(DETAIL) == "【中字4K·HiRes】「春日影」- MyGO!!!!!｜M"
    assert detail_title(PLAYER) == ""


def test_bilibili_marks_recently_watched_cards():
    first, second = (element for element in BILI_SEARCH if element.kind == "Cell")
    assert marked_watched(first, BILI_SEARCH) == "最近看过"
    assert marked_watched(second, BILI_SEARCH) is None


def test_android_player_is_recognised_and_its_title_is_read():
    # 安卓上竖屏播放器的右边也有「评论」，但没有「简介」，不会被当成详情页
    assert detect_page("tv.danmaku.bili", BILI_PLAYER_ANDROID, *BILI_ANDROID_SCREEN) == BILI_PLAYER
    assert page_title(BILI_PLAYER, BILI_PLAYER_ANDROID) == "离谱爆料之 爱音 将要加入sumimi？"
    # iPhone 上竖屏播放器的标题读不到，详情页读得到
    assert page_title(BILI_PLAYER, PLAYER) == ""
    assert page_title(BILI_DETAIL, DETAIL) == "【中字4K·HiRes】「春日影」- MyGO!!!!!｜M"


def test_card_texts_and_title_read_the_title_inside_a_card_whose_text_is_a_hot_comment():
    from phone_play.pages import card_texts, card_title

    card = BILI_SEARCH_COMMENT[2]
    texts = card_texts(card, BILI_SEARCH_COMMENT)
    assert texts[:2] == ["某网友：好棒的手书😭选曲也是非常完美", "【丰川祥子个人向手书】Esther Reed"]
    # 不含「最近看过」，也不会混进下一张卡片
    assert "最近看过" not in texts and "【手书】和祥子的15个约定" not in texts
    assert card_title(card, BILI_SEARCH_COMMENT) == "【丰川祥子个人向手书】Esther Reed"
    # 卡片文字就是标题的那种照旧用卡片文字，不会取成里面的时长
    assert card_title(BILI_SEARCH[2], BILI_SEARCH) == "【中字4K·HiRes】「春日影」- MyGO!!!!!｜M"


def test_netease_player_page_says_what_is_playing_and_whether_it_is_paused():
    from phone_play.pages import MUSIC_PLAYER, page_state

    height = BILI_SCREEN[1]
    assert detect_page("com.netease.cloudmusic", NETEASE_PLAYER, *BILI_SCREEN) == MUSIC_PLAYER
    assert page_state(MUSIC_PLAYER, NETEASE_PLAYER, height) == (
        "网易云播放页：在放的是「蝶恋花,曹思义」（歌名,歌手），正在播放（02:58/03:43）"
    )
    paused = [replace(element, label="播放") if element.label == "暂停" else element for element in NETEASE_PLAYER]
    assert page_state(MUSIC_PLAYER, paused, height).endswith("暂停着（02:58/03:43）")
    # 首页「心动」整屏播放也一样认
    assert detect_page("com.netease.cloudmusic", NETEASE_HOME_PLAYER, *BILI_SCREEN) == MUSIC_PLAYER
    assert "「Auspicious sign,Jusqu'à Grand-」" in page_state(MUSIC_PLAYER, NETEASE_HOME_PLAYER, height)
    # 只有底下一条迷你播放条的页面不算播放页
    assert detect_page("com.netease.cloudmusic", NETEASE_MINE, *BILI_SCREEN) == ""


def test_netease_vip_popups_over_the_player_are_called_out():
    from phone_play.pages import MUSIC_PLAYER, page_state

    height = BILI_SCREEN[1]
    for covered in (NETEASE_POPUP_SHEET, NETEASE_POPUP_CENTER):
        assert detect_page("com.netease.cloudmusic", covered, *BILI_SCREEN) == MUSIC_PLAYER
        assert "上面盖着会员广告，播放页的按钮点不到：先点广告右上角的 × 关掉" in page_state(
            MUSIC_PLAYER, covered, height
        )
    # 广告标题和歌名在同一行，歌名照样认得出；播放页自己的会员横幅不算弹窗
    assert "「蝶恋花,曹思义」" in page_state(MUSIC_PLAYER, NETEASE_POPUP_SHEET, height)
    assert "会员广告" not in page_state(MUSIC_PLAYER, NETEASE_PLAYER, height)


def test_fanqie_reader_page_tells_the_chapter_page_and_menu():
    from phone_play.pages import NOVEL_READER, page_state

    height = BILI_SCREEN[1]
    assert detect_page("com.dragon.read", FANQIE_READER, *BILI_SCREEN) == NOVEL_READER
    assert page_state(NOVEL_READER, FANQIE_READER, height) == (
        "番茄小说阅读页：第1章 完美的人生于此开始，页码 13/3736（翻过去就会变）"
    )
    assert detect_page("com.dragon.read", FANQIE_READER_MENU, *BILI_SCREEN) == NOVEL_READER
    assert page_state(NOVEL_READER, FANQIE_READER_MENU, height).endswith(
        "页码 14/3736（翻过去就会变）。菜单开着（上一章、下一章、目录），点画面中间收起"
    )
    # 阅读历史上也有「第1章 ……」，但没有页码：不算阅读页
    assert detect_page("com.dragon.read", FANQIE_HISTORY, *BILI_SCREEN) == ""
