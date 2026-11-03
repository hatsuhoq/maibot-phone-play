from dataclasses import replace

from phone_play.pages import BILI_DETAIL, BILI_PLAYER, detail_title, detect_page, marked_watched, page_title

from conftest import BILI_DETAIL as DETAIL
from conftest import BILI_PLAYER as PLAYER
from conftest import BILI_ANDROID_SCREEN, BILI_PLAYER_ANDROID, BILI_SCREEN, BILI_SEARCH


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
