import pytest

from phone_play.mdns import build_query, parse_service_port

# 2026 年 11 月 OPPO Reno4 5G（安卓 12）开着「无线调试」时，单播问 _adb-tls-connect._tcp.local 得到的回答，
# 只把实例名里的序列号和 A、AAAA 地址换成了等长的假值，报文结构、压缩指针都是手机原样给的：
# PTR 指向 adb-0123abcd-Xy12Zw，附加记录里有 SRV（Android.local:40135）、TXT 和 A（192.168.1.23）、AAAA（fe80::1）
OPPO_REPLY = bytes.fromhex(
    "000084000001000100000004105f6164622d746c732d636f6e6e656374045f746370056c6f63616c00000c8001c00c000c000100"
    "00000a0016136164622d30313233616263642d587931325a77c00cc039002100010000000a0010000000009cc707416e64726f69"
    "64c022c039001000010000000a001d1c763d4144425f5345435552455f534552564943455f56455253494f4ec061000100010000"
    "000a0004c0a80117c061001c00010000000a0010fe800000000000000000000000000001"
)


def test_query_asks_for_adb_wireless_service_with_unicast_reply():
    query = build_query()
    assert query[:12] == bytes([0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0])
    assert b"\x10_adb-tls-connect\x04_tcp\x05local\x00" in query
    # 类型 PTR、类别 IN 并要求单播回答
    assert query.endswith(b"\x00\x0c\x80\x01")


def test_real_phone_reply_gives_its_port():
    assert parse_service_port(OPPO_REPLY) == "40135"


def test_reply_without_the_service_has_no_port():
    # 只有问题、没有回答
    assert parse_service_port(build_query()) == ""


def test_broken_reply_is_reported():
    with pytest.raises(ValueError, match="mDNS 回答格式不对"):
        parse_service_port(OPPO_REPLY[:60])
