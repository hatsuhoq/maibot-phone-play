"""问无线调试的安卓手机它现在开在哪个端口。

安卓 11 起的「无线调试」端口是随机的，开关它、插拔数据线、重启手机都会变。手机会用 mDNS 公布服务
``_adb-tls-connect._tcp.local``，但实测 ``adb mdns services`` 只在手机刚打开无线调试、主动广播的那一两分钟里列得出来，
之后就过期了；直接往手机 IP 的 5353 端口发一个单播查询，亮屏、息屏锁着时它都马上回答（OPPO Reno4 5G，安卓 12）。
只用标准库，不用加依赖；阻塞的收发由调用方放进线程。
"""

from typing import Tuple

import socket
import struct

ADB_SERVICE = "_adb-tls-connect._tcp.local"
_MDNS_PORT = 5353
_TYPE_PTR = 12
_TYPE_SRV = 33
# 类别 IN，最高位要求单播回答
_CLASS_IN_UNICAST = 0x8001
_MAX_PACKET = 9000


def build_query(service: str = ADB_SERVICE) -> bytes:
    """一个 PTR 查询：问这台机器上有哪些 ``service`` 服务。"""
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    return header + _encode_name(service) + struct.pack("!HH", _TYPE_PTR, _CLASS_IN_UNICAST)


def _encode_name(name: str) -> bytes:
    return b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split(".")) + b"\x00"


def _read_name(data: bytes, offset: int) -> Tuple[str, int]:
    """读一个可能带压缩指针的域名，返回（名字，名字之后的位置）。"""
    labels = []
    end = -1
    for _ in range(64):
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if end < 0:
                end = offset + 2
            offset = struct.unpack("!H", data[offset : offset + 2])[0] & 0x3FFF
            continue
        if length == 0:
            return ".".join(labels), end if end >= 0 else offset + 1
        labels.append(data[offset + 1 : offset + 1 + length].decode("utf-8"))
        offset += 1 + length
    raise ValueError("域名的压缩指针绕成了圈")


def parse_service_port(data: bytes, service: str = ADB_SERVICE) -> str:
    """从回答里取出 ``service`` 实例的 SRV 端口，没有这个服务时返回空字符串。

    Raises:
        ValueError: 回答格式不对。
    """
    try:
        _, _, questions, answers, authorities, additional = struct.unpack("!HHHHHH", data[:12])
        offset = 12
        for _ in range(questions):
            _, offset = _read_name(data, offset)
            offset += 4
        for _ in range(answers + authorities + additional):
            name, offset = _read_name(data, offset)
            record_type, _, _, length = struct.unpack("!HHIH", data[offset : offset + 10])
            offset += 10
            if record_type == _TYPE_SRV and name.endswith("." + service):
                return str(struct.unpack("!H", data[offset + 4 : offset + 6])[0])
            offset += length
    except (struct.error, IndexError, UnicodeDecodeError) as exc:
        raise ValueError(f"mDNS 回答格式不对：{exc}") from exc
    return ""


def query_adb_port(host: str, timeout: float = 1.5, attempts: int = 2) -> str:
    """单播问 ``host`` 无线调试现在的端口；它没回答、没开无线调试时返回空字符串。会阻塞，放进线程里调用。

    Raises:
        OSError: 发不出去（比如电脑没联网）。
        ValueError: 回答格式不对。
    """
    query = build_query()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        for _ in range(attempts):
            sock.sendto(query, (host, _MDNS_PORT))
            try:
                data, sender = sock.recvfrom(_MAX_PACKET)
            except TimeoutError:
                continue
            if sender[0] == host:
                return parse_service_port(data)
    return ""
