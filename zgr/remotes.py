# -*- coding: utf-8 -*-
"""远程存储后端：WebDAV、S3（含 MinIO / 群晖 / 阿里云 OSS 的 S3 兼容模式）、
以及「本地/网络共享目录」（SMB、NAS 挂载点、UNC 路径都用这个）。

为什么要这一层
--------------
原来只有两种远端：
  · `HttpBackend`  —— 连另一台装了追光几何的机器（对端跑着服务）；
  · `ShareBackend` —— 共享目录里放一个**完整仓库**，靠文件复制同步。
很多场景两样都不合适：NAS 只给 WebDAV、对象存储只给 S3、或者共享目录里不想放
一整套 .zgr。于是补一个「傻存储」后端：远端只需要能存/取一个个文件。

远端布局（和本地对象库一一对应，随时能用别的工具检查）
    <prefix>/<项目名>/objects/<oid 前两位>/<oid>    对象原始字节（与本地 .zgr/objects 相同）
    <prefix>/<项目名>/refs/<分支名>                该分支当前提交号（纯文本）

对象是内容寻址的（sha256），所以「远端有没有」只跟对象号有关，天然幂等、可重传。
要算「远端缺哪些对象」，先只把提交/树这些很小的对象取回来，在本地走一遍
`sync.reachable()`，再只搬运差的文件——和 git 的 fetch 一个思路。

凭据
----
密码/密钥**不写进项目的 config.json**（项目目录经常被整个拷来拷去），
统一存在 `%APPDATA%/zhuiguang-record/secrets.json`，按「仓库路径哈希 + 远端名」索引。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .store import Repo, RepoError
from .sync import SyncError, reachable

CHUNK = 1 << 20
MAX_TRY = 3
UA = "ZhuiguangGeometry/remote"


# ===================================================================== 底层存储
class LocalStore:
    """本地磁盘 / 网络共享（SMB、NAS 挂载点、\\\\server\\share 这类 UNC 路径）"""

    kind = "local"

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expandvars(os.path.expanduser(root)))

    def describe(self) -> str:
        return self.root

    def _path(self, key: str) -> str:
        parts = [p for p in key.split("/") if p]
        if any(p in (".", "..") or ":" in p for p in parts):
            raise SyncError("远端键名不合法：%s" % key)
        return os.path.join(self.root, *parts)

    def get(self, key: str) -> bytes:
        try:
            with open(self._path(key), "rb") as f:
                return f.read()
        except OSError as e:
            raise SyncError("读远端文件失败：%s（%s）" % (key, e)) from None

    def put(self, key: str, data: bytes):
        dst = self._path(key)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".tmp.%d" % os.getpid()
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)

    def delete(self, key: str):
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass
        except OSError as e:
            raise SyncError("删远端文件失败：%s（%s）" % (key, e)) from None

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._path(key))

    def list(self, prefix: str):
        base = self._path(prefix)
        out = []
        if not os.path.isdir(base):
            return out
        for dirpath, _dirs, files in os.walk(base):
            for n in files:
                full = os.path.join(dirpath, n)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                out.append(rel)
        return out


class WebDavStore:
    """WebDAV（群晖、威联通、Nextcloud、坚果云、各类 NAS 自带 WebDAV）

    支持 Basic 与 Digest 两种认证（老 NAS 常用 Digest）。只用标准库。
    """

    kind = "webdav"

    def __init__(self, base: str, user: str = "", password: str = "",
                 timeout: int = 60, verify_tls: bool = True):
        if not base:
            raise SyncError("WebDAV 地址不能为空")
        self.base = base.rstrip("/")
        self.user = user or ""
        self.password = password or ""
        self.timeout = timeout
        self.verify_tls = verify_tls
        self._digest = None          # 首次 401 之后记住 nonce 等参数
        scheme = urllib.parse.urlsplit(self.base).scheme.lower()
        if scheme not in ("http", "https"):
            raise SyncError("WebDAV 地址要以 http:// 或 https:// 开头")

    def describe(self) -> str:
        u = urllib.parse.urlsplit(self.base)
        return "%s://%s%s" % (u.scheme, u.hostname, u.path)

    # ---------------------------------------------------------------- 认证
    def _auth_header(self, method: str, url: str) -> dict:
        if not self.user and not self.password:
            return {}
        if self._digest:
            return {"Authorization": self._digest_header(method, url)}
        raw = ("%s:%s" % (self.user, self.password)).encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

    def _digest_header(self, method: str, url: str) -> str:
        d = self._digest
        path = urllib.parse.urlsplit(url).path or "/"
        ha1 = _md5("%s:%s:%s" % (self.user, d["realm"], self.password))
        ha2 = _md5("%s:%s" % (method, path))
        nc = "%08x" % (d["nc"] + 1)
        d["nc"] += 1
        cnonce = hashlib.sha256(os.urandom(8)).hexdigest()[:16]
        if d.get("qop"):
            resp = _md5("%s:%s:%s:%s:auth:%s" % (ha1, d["nonce"], nc, cnonce, ha2))
        else:
            resp = _md5("%s:%s:%s" % (ha1, d["nonce"], ha2))
        parts = ['username="%s"' % self.user, 'realm="%s"' % d["realm"],
                 'nonce="%s"' % d["nonce"], 'uri="%s"' % path,
                 'algorithm=%s' % (d.get("algorithm") or "MD5"),
                 'response="%s"' % resp]
        if d.get("opaque"):
            parts.append('opaque="%s"' % d["opaque"])
        if d.get("qop"):
            parts += ["qop=auth", "nc=" + nc, 'cnonce="%s"' % cnonce]
        return "Digest " + ", ".join(parts)

    def _parse_challenge(self, header: str) -> bool:
        if not header or not header.lower().startswith("digest"):
            return False
        d = {"nc": 0}
        for k, v in re.findall(r'(\w+)=("[^"]*"|[^,]*)', header):
            name = k.lower()
            val = (v or "").strip().strip('"')
            d[name] = val
        if "realm" not in d or "nonce" not in d:
            return False
        self._digest = d
        return True

    # ---------------------------------------------------------------- HTTP
    def _request(self, method: str, key: str = "", data: bytes | None = None,
                 headers: dict | None = None, body: bytes | None = None):
        url = self.key_url(key)
        hdrs = {"User-Agent": UA}
        if headers:
            hdrs.update(headers)
        hdrs.update(self._auth_header(method, url))
        payload = data if data is not None else body
        last = None
        for attempt in range(MAX_TRY):
            req = urllib.request.Request(url, data=payload, headers=hdrs, method=method)
            ctx = None
            if url.lower().startswith("https") and not self.verify_tls:
                import ssl
                ctx = ssl._create_unverified_context()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout,
                                            context=ctx) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    if self._parse_challenge(e.headers.get("WWW-Authenticate") or ""):
                        hdrs.update(self._auth_header(method, url))
                        continue
                if e.code in (500, 502, 503, 504) and attempt < MAX_TRY - 1:
                    last = e
                    time.sleep(0.6 * (attempt + 1))
                    continue
                return e.code, dict(e.headers or {}), e.read()
            except (urllib.error.URLError, OSError) as e:
                last = e
                if attempt < MAX_TRY - 1:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                raise SyncError("连不上 WebDAV：%s（%s）" % (self.base, e)) from None
        raise SyncError("WebDAV 请求失败：%s（%s）" % (self.base, last))

    def key_url(self, key: str) -> str:
        if not key:
            return self.base + "/"
        segs = [urllib.parse.quote(p, safe="") for p in key.split("/") if p]
        return self.base + "/" + "/".join(segs)

    def mkdirs(self, key: str):
        """逐级 MKCOL；已存在（405/301）忽略"""
        parts = [p for p in key.split("/") if p]
        cur = ""
        for p in parts:
            cur = (cur + "/" + p) if cur else p
            code, _h, _b = self._request("MKCOL", cur)
            if code not in (200, 201, 204, 301, 405):
                raise SyncError("WebDAV 建目录失败：%s（HTTP %s）" % (cur, code))

    # ---------------------------------------------------------------- 接口
    def get(self, key: str) -> bytes:
        code, _h, body = self._request("GET", key)
        if code != 200:
            raise SyncError("WebDAV 下载失败：%s（HTTP %s）" % (key, code))
        return body

    def put(self, key: str, data: bytes):
        parent = key.rsplit("/", 1)[0] if "/" in key else ""
        if parent:
            self.mkdirs(parent)
        code, _h, body = self._request("PUT", key, data=data,
                                       headers={"Content-Type": "application/octet-stream"})
        if code not in (200, 201, 204):
            raise SyncError("WebDAV 上传失败：%s（HTTP %s %s）"
                            % (key, code, body[:120].decode("utf-8", "replace")))

    def delete(self, key: str):
        self._request("DELETE", key)

    def exists(self, key: str) -> bool:
        code, _h, _b = self._request("HEAD", key)
        if code == 200:
            return True
        code, _h, _b = self._request("PROPFIND", key,
                                     headers={"Depth": "0",
                                              "Content-Type": "application/xml"})
        return code in (200, 207)

    def list(self, prefix: str) -> list[str]:
        """PROPFIND Depth:1 列一层；本模块的目录结构是固定的，递归即可"""
        out = []
        self._list_dir(prefix, out, 0)
        return out

    def _list_dir(self, key: str, out: list, depth: int):
        if depth > 4:
            return
        body = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                b'<d:resourcetype/><d:getcontentlength/></d:prop></d:propfind>')
        code, _h, raw = self._request("PROPFIND", key, data=body,
                                      headers={"Depth": "1",
                                               "Content-Type": "application/xml"})
        if code not in (200, 207):
            return
        base_path = urllib.parse.urlsplit(self.key_url(key)).path.rstrip("/")
        dirs, files = [], []
        for href, is_dir in _parse_propfind(raw):
            path = urllib.parse.unquote(urllib.parse.urlsplit(href).path)
            if path.rstrip("/") == base_path:
                continue
            rel = path[len(base_path):].lstrip("/") if path.startswith(base_path) else path
            if not rel:
                continue
            full = (key + "/" + rel) if key else rel
            (dirs if is_dir else files).append(full)
        out.extend(files)
        for d in dirs:
            self._list_dir(d, out, depth + 1)


class S3Store:
    """S3 兼容对象存储：AWS S3、MinIO、群晖 C2/S3、阿里云 OSS(S3 模式)、Backblaze B2 等

    只用标准库实现 SigV4 签名（对小文件一次性读进内存，够用且简单）。
    """

    kind = "s3"

    def __init__(self, bucket: str, endpoint: str = "https://s3.amazonaws.com",
                 region: str = "us-east-1", access_key: str = "", secret_key: str = "",
                 prefix: str = "", path_style: bool = True, session_token: str = "",
                 timeout: int = 60):
        if not bucket:
            raise SyncError("S3 bucket 不能为空")
        if not access_key or not secret_key:
            raise SyncError("S3 需要 access_key 和 secret_key")
        self.bucket = bucket
        self.endpoint = endpoint.rstrip("/")
        self.region = region or "us-east-1"
        self.access_key = access_key
        self.secret_key = secret_key
        self.prefix = prefix.strip("/")
        self.path_style = path_style
        self.session_token = session_token
        self.timeout = timeout
        u = urllib.parse.urlsplit(self.endpoint)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise SyncError("S3 端点要以 http:// 或 https:// 开头")

    def describe(self) -> str:
        u = urllib.parse.urlsplit(self.endpoint)
        return "%s/%s" % (u.netloc, self.bucket) + ("/" + self.prefix if self.prefix else "")

    # ---------------------------------------------------------------- SigV4
    @staticmethod
    def _enc(s: str) -> str:
        return urllib.parse.quote(s, safe="-_.~")

    def _canonical_uri(self, key: str) -> str:
        parts = [self._enc(p) for p in key.split("/")]
        path = "/" + "/".join(parts) if parts != [""] else "/"
        if not self.path_style:
            return path
        return "/" + self._enc(self.bucket) + path

    def _host(self) -> str:
        """Host 头里的主机名（不含端口）；虚拟主机风格会把 bucket 加在前面"""
        u = urllib.parse.urlsplit(self.endpoint)
        host = u.hostname or ""
        if self.path_style:
            return host
        return "%s.%s" % (self.bucket, host)

    def _sign(self, method: str, key: str, query: dict | None = None,
              payload: bytes = b"", now=None) -> dict:
        """返回要带上的请求头。now 可指定时间（测试用固定向量）。"""
        now = now or getattr(self, "clock", None) or time.gmtime()
        amz_date = time.strftime("%Y%m%dT%H%M%SZ", now)
        date = amz_date[:8]
        payload_hash = hashlib.sha256(payload).hexdigest()
        q = sorted((self._enc(k), self._enc(str(v))) for k, v in (query or {}).items())
        cq = "&".join("%s=%s" % kv for kv in q)
        headers = {"host": self._host(),
                   "x-amz-content-sha256": payload_hash,
                   "x-amz-date": amz_date}
        if self.session_token:
            headers["x-amz-security-token"] = self.session_token
        signed = ";".join(sorted(headers))
        canonical_headers = "".join("%s:%s\n" % (k, headers[k]) for k in sorted(headers))
        creq = "\n".join([method, self._canonical_uri(key), cq, canonical_headers,
                          signed, payload_hash])
        scope = "%s/%s/s3/aws4_request" % (date, self.region)
        sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(creq.encode("utf-8")).hexdigest()])
        k = ("AWS4" + self.secret_key).encode("utf-8")
        for part in (date, self.region, "s3", "aws4_request"):
            k = hmac.new(k, part.encode("utf-8"), hashlib.sha256).digest()
        sig = hmac.new(k, sts.encode("utf-8"), hashlib.sha256).hexdigest()
        out = {h: headers[h] for h in headers if h != "host"}
        out["Host"] = headers["host"]
        out["Authorization"] = ("AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, "
                                "Signature=%s" % (self.access_key, scope, signed, sig))
        return out

    def _url(self, key: str, query: dict | None = None) -> str:
        u = urllib.parse.urlsplit(self.endpoint)
        netloc = self._host()
        if u.port:
            netloc = "%s:%d" % (netloc, u.port)
        path = self._canonical_uri(key)
        q = "&".join("%s=%s" % (self._enc(k), self._enc(str(v)))
                     for k, v in sorted((query or {}).items()))
        return urllib.parse.urlunsplit((u.scheme, netloc, path, q, ""))

    def _do(self, method: str, key: str, query: dict | None = None,
            payload: bytes = b"", raw: bool = False):
        url = self._url(key, query)
        headers = self._sign(method, key, query, payload)
        if payload:
            headers["Content-Type"] = "application/octet-stream"
        last = None
        for attempt in range(MAX_TRY):
            req = urllib.request.Request(url, data=payload if payload else None,
                                         headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.status, r.read() if raw else b""
            except urllib.error.HTTPError as e:
                if e.code in (500, 502, 503, 504) and attempt < MAX_TRY - 1:
                    last = e
                    time.sleep(0.6 * (attempt + 1))
                    continue
                return e.code, e.read() if raw else b""
            except (urllib.error.URLError, OSError) as e:
                last = e
                if attempt < MAX_TRY - 1:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                raise SyncError("连不上 S3：%s（%s）" % (self.endpoint, e)) from None
        raise SyncError("S3 请求失败：%s（%s）" % (self.endpoint, last))

    # ---------------------------------------------------------------- 接口
    def get(self, key: str) -> bytes:
        code, body = self._do("GET", key, raw=True)
        if code != 200:
            raise SyncError("S3 下载失败：%s（HTTP %s）" % (key, code))
        return body

    def put(self, key: str, data: bytes):
        code, body = self._do("PUT", key, payload=data, raw=True)
        if code not in (200, 204):
            raise SyncError("S3 上传失败：%s（HTTP %s %s）"
                            % (key, code, body[:160].decode("utf-8", "replace")))

    def delete(self, key: str):
        self._do("DELETE", key)

    def exists(self, key: str) -> bool:
        code, _ = self._do("HEAD", key)
        return code == 200

    def list(self, prefix: str) -> list[str]:
        out, token = [], ""
        while True:
            q = {"list-type": "2", "prefix": prefix.rstrip("/") + "/" if prefix else ""}
            if token:
                q["continuation-token"] = token
            code, body = self._do("GET", "", query=q, raw=True)
            if code != 200:
                raise SyncError("S3 列目录失败（HTTP %s）" % code)
            keys, nxt = _parse_s3_list(body)
            out.extend(keys)
            if not nxt:
                return out
            token = nxt


# ===================================================================== 高层后端
class StoreBackend:
    """把上面的「傻存储」包装成与 HttpBackend / ShareBackend 相同的接口"""

    kind = "store"

    def __init__(self, store, prefix: str = "", project: str = "", name: str = "",
                 repo=None):
        self.store = store
        self.prefix = (prefix or "").strip("/")
        self.project = _safe_name(project) or "project"
        self.name = name or store.describe()
        self._repo = repo          # manifest() 没有 repo 参数，只能先记在这里
        self._walk_cache = None

    def _need_repo(self) -> Repo:
        if self._repo is None:
            raise SyncError("StoreBackend 还没绑定本地仓库，无法走远端对象图")
        return self._repo

    # ---------------------------------------------------------------- 键
    def _base(self) -> str:
        return "/".join(p for p in (self.prefix, self.project) if p)

    def _objkey(self, oid: str) -> str:
        return "%s/objects/%s/%s" % (self._base(), oid[:2], oid)

    def _refkey(self, branch: str) -> str:
        return "%s/refs/%s" % (self._base(), urllib.parse.quote(branch, safe=""))

    # ---------------------------------------------------------------- 引用
    def refs(self):
        branches = {}
        try:
            keys = self.store.list(self._base() + "/refs")
        except SyncError:
            keys = []
        for key in keys:
            name = urllib.parse.unquote(key.rsplit("/", 1)[-1])
            try:
                head = self.store.get(key).decode("ascii", "ignore").strip()
            except SyncError:
                continue
            if head:
                branches[name] = head
        return {"ok": True, "name": self.name, "branches": branches,
                "default": "main"}

    def _branch_head(self, branch: str) -> str:
        try:
            return self.store.get(self._refkey(branch)).decode("ascii", "ignore").strip()
        except SyncError:
            return ""

    # ---------------------------------------------------------------- 走一遍远端对象图
    def _walk(self, repo: Repo, head: str) -> set:
        """把远端 head 可达的对象号摸清楚；提交/树这些小块顺手拉回本地。

        文件对象（可能很大）只记号不下载 —— 真正要不要下载由调用方决定。
        """
        if not head:
            return set()
        if self._walk_cache and self._walk_cache[0] == head:
            return self._walk_cache[1]
        seen, stack = set(), [head]
        tmpdir = os.path.join(repo.meta, "tmp")
        os.makedirs(tmpdir, exist_ok=True)
        while stack:
            oid = stack.pop()
            if not oid or oid in seen:
                continue
            seen.add(oid)
            if not repo.has(oid):
                raw = self.store.get(self._objkey(oid))
                tmp = os.path.join(tmpdir, oid + ".remote")
                with open(tmp, "wb") as f:
                    f.write(raw)
                try:
                    repo.put_raw_file(oid, tmp)
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            kind, payload = repo.get(oid)
            if kind == "commit":
                c = json.loads(payload)
                stack.extend(c.get("parents") or [])
                stack.append(c["tree"])
            elif kind == "tree":
                for it in json.loads(payload):
                    if it["t"] == "d":
                        stack.append(it["h"])
                    else:
                        seen.add(it["h"])
        self._walk_cache = (head, seen)
        return seen

    def manifest(self, branch, have):
        head = self._branch_head(branch)
        if not head:
            return []
        repo = self._need_repo()
        remote_all = self._walk(repo, head)
        local = reachable(repo, have) if have else set()
        return sorted(remote_all - local)

    def fetch(self, repo: Repo, oids, log=print) -> int:
        self._repo = repo
        got = 0
        tmpdir = os.path.join(repo.meta, "tmp")
        os.makedirs(tmpdir, exist_ok=True)
        for i, oid in enumerate(oids, 1):
            if repo.has(oid):
                continue
            raw = self.store.get(self._objkey(oid))
            tmp = os.path.join(tmpdir, oid + ".part")
            with open(tmp, "wb") as f:
                f.write(raw)
            try:
                if repo.put_raw_file(oid, tmp):
                    got += 1
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if i % 200 == 0:
                log("  已接收 %d/%d" % (i, len(oids)))
        self._walk_cache = None
        return got

    def push(self, repo: Repo, oids, log=print) -> int:
        self._repo = repo
        sent = 0
        for i, oid in enumerate(oids, 1):
            self.store.put(self._objkey(oid), repo.object_bytes(oid))
            sent += 1
            if i % 100 == 0:
                log("  已上传 %d/%d" % (i, len(oids)))
        self._walk_cache = None
        return sent

    def set_ref(self, branch, old, new):
        cur = self._branch_head(branch)
        if old and cur and cur != old:
            raise SyncError("远端分支 %s 已被更新（现在是 %s），请先拉取再推送"
                            % (branch, cur[:12]))
        self.store.put(self._refkey(branch), (new or "").encode("ascii"))
        self._walk_cache = None
        return {"ok": True, "head": new}


# ===================================================================== 工具
def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _safe_name(name: str) -> str:
    """项目名当目录用：去掉路径分隔符等危险字符，中文保留"""
    name = (name or "").strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    return name.strip(". ") or "project"


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_propfind(raw: bytes):
    """解析 WebDAV 目录列表：返回 [(href, 是否目录)]"""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out = []
    for resp in root.iter():
        if _strip_ns(resp.tag) != "response":
            continue
        href = ""
        is_dir = False
        for node in resp.iter():
            tag = _strip_ns(node.tag)
            if tag == "href" and not href:
                href = (node.text or "").strip()
            elif tag == "collection":
                is_dir = True
        if href:
            out.append((href, is_dir))
    return out


def _parse_s3_list(raw: bytes):
    """解析 ListObjectsV2 结果：返回 (keys, 下一页 token)"""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return [], ""
    keys, token = [], ""
    for node in root.iter():
        tag = _strip_ns(node.tag)
        if tag == "Key" and node.text:
            keys.append(node.text)
        elif tag == "NextContinuationToken" and node.text:
            token = node.text.strip()
    return keys, token


# ===================================================================== 配置与凭据
def repo_key(repo: Repo) -> str:
    """凭据索引：跟着仓库路径走，项目目录被拷贝时凭据不会跟着跑"""
    p = os.path.normcase(os.path.abspath(repo.root))
    return hashlib.sha1(p.encode("utf-8", "ignore")).hexdigest()[:16]


def secrets_path() -> str:
    from .registry import appdata_dir
    return os.path.join(appdata_dir(), "secrets.json")


def _read_secrets() -> dict:
    try:
        with open(secrets_path(), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _write_secrets(data: dict):
    """原子写凭据文件。

    临时名以前是固定的 `<path>.tmp`：命令行 `zgr remote` 和常驻服务同时保存远端时，
    两个进程会抢同一个临时文件——一个已经把临时文件 rename 走，另一个 rename 时
    目标已经不存在，直接抛错，刚输入的密码就没了。带上 pid/线程/时间就不会撞。
    """
    path = secrets_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d.%d.%d" % (path, os.getpid(), threading.get_ident(), time.time_ns())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


SECRET_FIELDS = ("password", "secret_key", "token", "access_key")


def get_secret(repo: Repo, name: str, field: str = "password") -> str:
    return ((_read_secrets().get(repo_key(repo)) or {}).get(name) or {}).get(field) or ""


def set_secret(repo: Repo, name: str, values: dict):
    data = _read_secrets()
    bucket = data.setdefault(repo_key(repo), {})
    cur = bucket.get(name) or {}
    for k, v in values.items():
        if v:
            cur[k] = v
    bucket[name] = cur
    _write_secrets(data)


def drop_secret(repo: Repo, name: str):
    data = _read_secrets()
    bucket = data.get(repo_key(repo)) or {}
    if name in bucket:
        bucket.pop(name, None)
        _write_secrets(data)


def list_remotes(repo: Repo) -> list[dict]:
    conf = repo.config or {}
    out = []
    for item in conf.get("remotes") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        clean = {k: v for k, v in item.items() if k not in SECRET_FIELDS}
        clean["has_secret"] = bool(get_secret(repo, item["name"]) or
                                   get_secret(repo, item["name"], "secret_key"))
        out.append(clean)
    return out


def find_remote(repo: Repo, name: str) -> dict:
    for item in list_remotes(repo):
        if item["name"] == name:
            return item
    raise SyncError("没有这个远端：%s（现有的：%s）"
                    % (name, "、".join(x["name"] for x in list_remotes(repo)) or "无"))


def save_remote(repo: Repo, conf: dict, secret: dict | None = None):
    """写入一个远端配置；secret 里的东西只进 secrets.json"""
    name = (conf.get("name") or "").strip()
    if not name:
        raise SyncError("远端要有个名字")
    kind = (conf.get("kind") or "").strip().lower()
    if kind not in ("webdav", "s3", "local", "share", "http"):
        raise SyncError("不认识的远端类型：%s（webdav / s3 / local / http）" % kind)
    item = {k: v for k, v in conf.items() if k not in SECRET_FIELDS and v not in (None, "")}
    item["name"] = name
    item["kind"] = kind
    def _put(cfg):
        # 远端列表是整份读-改-写：两台机器/两个窗口同时加远端时，
        # 后写的那份会把别人刚加的整条盖掉（远端配置丢了不报错，只是同步突然没得选）。
        remotes = [r for r in (cfg.get("remotes") or [])
                   if isinstance(r, dict) and r.get("name") != name]
        remotes.append(item)
        cfg["remotes"] = remotes
    repo.update_config(_put)
    if secret:
        set_secret(repo, name, secret)


def remove_remote(repo: Repo, name: str):
    def _drop(cfg):
        cfg["remotes"] = [r for r in (cfg.get("remotes") or [])
                          if not (isinstance(r, dict) and r.get("name") == name)]
    repo.update_config(_drop)
    drop_secret(repo, name)


def make_store(conf: dict, repo: Repo) -> object:
    kind = (conf.get("kind") or "").lower()
    name = conf.get("name") or ""
    if kind in ("local", "share"):
        path = conf.get("path") or conf.get("url") or ""
        if not path:
            raise SyncError("远端 %s 缺少路径" % name)
        return LocalStore(path)
    if kind == "webdav":
        return WebDavStore(conf.get("url") or "",
                           user=conf.get("user") or "",
                           password=get_secret(repo, name, "password"),
                           verify_tls=bool(conf.get("verify_tls", True)))
    if kind == "s3":
        return S3Store(bucket=conf.get("bucket") or "",
                       endpoint=conf.get("endpoint") or "https://s3.amazonaws.com",
                       region=conf.get("region") or "us-east-1",
                       access_key=get_secret(repo, name, "access_key"),
                       secret_key=get_secret(repo, name, "secret_key"),
                       prefix=conf.get("prefix") or "",
                       path_style=bool(conf.get("path_style", True)),
                       session_token=get_secret(repo, name, "token"))
    raise SyncError("远端 %s 的类型不支持：%s" % (name, kind))


def make_remote_backend(repo: Repo, name: str):
    """按配置造一个可以直接交给 sync() 的后端"""
    conf = find_remote(repo, name)
    from .sync import HttpBackend
    if conf["kind"] == "http":
        return HttpBackend(conf.get("url") or "", token=get_secret(repo, name, "token"),
                           project=conf.get("project") or repo.name)
    store = make_store(conf, repo)
    be = StoreBackend(store, prefix=conf.get("prefix") or "",
                      project=conf.get("project") or repo.name, name=name)
    be._repo = repo
    return be


def test_remote(repo: Repo, name: str) -> dict:
    """连通性自检：能列目录/读写一个小文件就算通"""
    be = make_remote_backend(repo, name)
    conf = find_remote(repo, name)
    detail = {"name": name, "kind": conf["kind"], "backend": getattr(be, "kind", "")}
    if conf["kind"] == "http":
        refs = be.refs()
        detail.update(ok=True, remote=refs.get("name") or "",
                      branches=len(refs.get("branches") or {}))
        return detail
    store = be.store
    detail["store"] = store.describe()
    probe = "%s/.zgr-probe" % be._base()
    token = ("probe %s" % time.strftime("%Y-%m-%d %H:%M:%S")).encode("ascii")
    store.put(probe, token)
    back = store.get(probe)
    if back != token:
        raise SyncError("写进去又读回来不一致（远端可能有多副本缓存）")
    store.delete(probe)
    try:
        listing = store.list(be._base() + "/objects")
    except SyncError:
        listing = []
    detail.update(ok=True, objects=len(listing))
    return detail


# ===================================================================== 便捷入口
def sync_remote(repo: Repo, name: str, push=True, pull=True, checkout=False,
                force=False, branch=None, log=print) -> dict:
    from .sync import sync
    be = make_remote_backend(repo, name)
    return sync(repo, push=push, pull=pull, checkout=checkout, force=force,
                branch=branch, log=log, backend=be)
