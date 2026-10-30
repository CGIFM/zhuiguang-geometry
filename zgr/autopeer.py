"""局域网自动配对：扫自己所在的 /24，找到同样开着追光几何、且有同名项目的那台机器。

只做两件事：读对方的项目名（/api/sync/discover 不返回任何本地路径），
以及给调用方一个可用的 http://ip:port 地址。不写任何东西。
"""
from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DISCOVER_PATH = "/api/sync/discover"
MAX_SUBNETS = 2


_FAKE_NETS = [ipaddress.ip_network("198.18.0.0/15")]   # Clash TUN 的 fake-ip 段


def _usable(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version != 4 or a.is_loopback or a.is_link_local or a.is_multicast or a.is_unspecified:
        return False
    return not any(a in n for n in _FAKE_NETS)


def primary_ip() -> str:
    """不发包的 UDP connect：拿本机默认出口地址，最可靠的取本机 LAN IP 的办法。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def local_ips() -> list[str]:
    ips = []
    for ip in [primary_ip()]:
        if ip and _usable(ip) and ip not in ips:
            ips.append(ip)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _usable(ip) and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def scan_groups(port: int) -> list[list[str]]:
    """按网段分组的待扫地址；第一组是默认出口网段，最可能有对方那台。"""
    mine = set(local_ips())
    groups, seen = [], set()
    for ip in local_ips():
        try:
            net = ipaddress.ip_network(ip + "/24", strict=False)
        except ValueError:
            continue
        key = str(net)
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > MAX_SUBNETS:
            break
        groups.append([str(h) for h in net.hosts() if str(h) not in mine])
    return groups


def scan_targets(port: int) -> list[str]:
    out = []
    for g in scan_groups(port):
        out.extend(g)
    return out


def _get_json(url: str, token: str, timeout: float):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("X-ZGR-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(1 << 20).decode("utf-8"))


def probe(base: str, token: str = "", timeout: float = 2.0):
    """已知地址探活，返回对方项目清单（失败返回 None）。"""
    base = (base or "").rstrip("/")
    if not base:
        return None
    try:
        d = _get_json(base + DISCOVER_PATH, token, timeout)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(d, dict) or not d.get("ok"):
        return None
    d["url"] = base
    return d


def refs(base: str, token: str, project: str, timeout: float = 2.0):
    """问对方某个项目的分支与根提交，用来判断两边是不是同一段历史。"""
    base = (base or "").rstrip("/")
    if not base:
        return None
    url = base + "/api/sync/refs?roots=1&project=" + urllib.parse.quote(project or "")
    try:
        d = _get_json(url, token, timeout)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("ok") else None


def _probe_host(ip: str, port: int, token: str, timeout: float):
    base = "http://%s:%d" % (ip, port)
    d = probe(base, token, timeout)
    if d:
        d["ip"] = ip
    return d


def discover(port: int = 8795, token: str = "", timeout: float = 0.45,
             workers: int = 96) -> list[dict]:
    """扫局域网，返回可用的追光几何服务（按网段顺序，命中即停）。"""
    found = []
    for targets in scan_groups(port):
        if not targets:
            continue
        with ThreadPoolExecutor(max_workers=max(4, min(workers, len(targets)))) as ex:
            for d in ex.map(lambda ip: _probe_host(ip, port, token, timeout), targets):
                if d:
                    found.append(d)
        if found:      # 默认出口网段已经有结果，没必要再扫 VPN/虚拟网卡
            break
    found.sort(key=lambda d: tuple(int(x) for x in d.get("ip", "0.0.0.0").split(".")))
    return found


def pick(peers: list[dict], project_name: str, local_host: str = ""):
    """挑出有同名项目的另一台；绝不把自己配成自己的「对方」。"""
    name = (project_name or "").strip().lower()
    me = (local_host or "").lower()
    hits = []
    for p in peers or []:
        if me and (p.get("host") or "").lower() == me:
            continue
        names = [(x.get("name") or "").lower() for x in (p.get("projects") or [])]
        if name and name in names:
            hits.append(p)
    return hits[0] if hits else None
