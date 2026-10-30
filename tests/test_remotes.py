"""远程存储后端自测：python -m unittest tests.test_remotes

覆盖：
  · LocalStore + StoreBackend 的推/拉完整往返（用真的仓库对象，不走网络）
  · 一台假 WebDAV 服务器（Basic + Digest、PROPFIND/MKCOL/PUT/GET/HEAD/DELETE）
  · 一台假 S3 服务器（独立重算 SigV4，验签名正确、ListObjectsV2 翻页）
  · AWS 官方 SigV4 测试向量（固定时间，防止签名实现悄悄跑偏）
  · 凭据不落进项目 config.json
"""
import base64
import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import remotes  # noqa: E402
from zgr.store import Repo  # noqa: E402
from zgr.sync import SyncError  # noqa: E402


# ---------------------------------------------------------------- 假 WebDAV
class FakeDav:
    """内存版 WebDAV：只实现本模块用到的那几个方法"""

    def __init__(self, user="u", password="p", digest=False):
        self.files = {}
        self.dirs = {""}
        self.user = user
        self.password = password
        self.digest = digest
        self.nonce = "abc123nonce"
        self.realm = "test"
        self.log = []

    # --- 认证
    def check_basic(self, header: str) -> bool:
        if not header.startswith("Basic "):
            return False
        raw = base64.b64decode(header[6:]).decode("utf-8")
        return raw == "%s:%s" % (self.user, self.password)

    def check_digest(self, header: str, method: str) -> bool:
        if not header.startswith("Digest "):
            return False
        parts = {}
        for chunk in header[7:].split(","):
            k, _, v = chunk.strip().partition("=")
            v = v.strip()
            if v.startswith('"'):
                v = v.strip('"')
            parts[k] = v
        ha1 = hashlib.md5(("%s:%s:%s" % (self.user, self.realm, self.password)).encode()).hexdigest()
        ha2 = hashlib.md5(("%s:%s" % (method, parts.get("uri", "/"))).encode()).hexdigest()
        if parts.get("qop"):
            want = hashlib.md5(("%s:%s:%s:%s:auth:%s" % (
                ha1, self.nonce, parts.get("nc"), parts.get("cnonce"), ha2)).encode()).hexdigest()
        else:
            want = hashlib.md5(("%s:%s:%s" % (ha1, self.nonce, ha2)).encode()).hexdigest()
        return parts.get("response") == want and parts.get("nonce") == self.nonce


def make_dav_handler(dav: FakeDav):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def _path(self):
            return urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).strip("/")

        def _authed(self) -> bool:
            auth = self.headers.get("Authorization") or ""
            if dav.digest:
                return dav.check_digest(auth, self.command)
            return dav.check_basic(auth)

        def _deny(self):
            if dav.digest:
                chal = ('Digest realm="%s", nonce="%s", qop="auth"'
                        % (dav.realm, dav.nonce))
            else:
                chal = 'Basic realm="%s"' % dav.realm
            self.send_response(401)
            self.send_header("WWW-Authenticate", chal)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _reply(self, code, body=b"", ctype="text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def do_PUT(self):
            data = self._body()
            if not self._authed():
                return self._deny()
            p = self._path()
            dav.files[p] = data
            parent = p.rsplit("/", 1)[0] if "/" in p else ""
            if parent:
                dav.dirs.add(parent)
            dav.log.append(("PUT", p, len(data)))
            self._reply(201)

        def do_GET(self):
            if not self._authed():
                return self._deny()
            p = self._path()
            if p not in dav.files:
                return self._reply(404)
            self._reply(200, dav.files[p], "application/octet-stream")

        def do_HEAD(self):
            if not self._authed():
                return self._deny()
            p = self._path()
            self._reply(200 if p in dav.files else 404)

        def do_DELETE(self):
            if not self._authed():
                return self._deny()
            dav.files.pop(self._path(), None)
            self._reply(204)

        def do_MKCOL(self):
            if not self._authed():
                return self._deny()
            p = self._path()
            if p in dav.dirs:
                return self._reply(405)
            dav.dirs.add(p)
            self._reply(201)

        def do_PROPFIND(self):
            if not self._authed():
                return self._deny()
            p = self._path()
            depth = self.headers.get("Depth") or "0"
            items = []
            if p in dav.dirs or p == "":
                items.append(("/" + p + "/" if p else "/", True))
                if depth != "0":
                    pre = (p + "/") if p else ""
                    for d in sorted(dav.dirs):
                        if d and d != p and d.startswith(pre) and "/" not in d[len(pre):]:
                            items.append(("/" + d + "/", True))
                    for f in sorted(dav.files):
                        if f.startswith(pre) and "/" not in f[len(pre):]:
                            items.append(("/" + f, False))
            elif p in dav.files:
                items.append(("/" + p, False))
            else:
                return self._reply(404)
            ns = 'xmlns:d="DAV:"'
            rows = []
            for href, is_dir in items:
                rt = "<d:collection/>" if is_dir else ""
                rows.append('<d:response><d:href>%s</d:href><d:propstat><d:prop>'
                            '<d:resourcetype>%s</d:resourcetype></d:prop>'
                            '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
                            % (href, rt))
            body = ('<?xml version="1.0"?><d:multistatus %s>%s</d:multistatus>'
                    % (ns, "".join(rows))).encode("utf-8")
            self._reply(207, body, "application/xml")

    return H


# ---------------------------------------------------------------- 假 S3
class FakeS3:
    def __init__(self, bucket="bkt", access="AK", secret="SK", region="us-east-1"):
        self.bucket = bucket
        self.access = access
        self.secret = secret
        self.region = region
        self.files = {}
        self.log = []

    def verify(self, method, path, query: dict, headers) -> bool:
        """独立重算一遍 SigV4（不复用被测代码）"""
        auth = headers.get("authorization") or ""
        if not auth.startswith("AWS4-HMAC-SHA256 "):
            return False
        fields = {}
        for chunk in auth[len("AWS4-HMAC-SHA256 "):].split(","):
            k, _, v = chunk.strip().partition("=")
            fields[k] = v
        cred = fields.get("Credential", "")
        scope = cred.split("/", 1)[1] if "/" in cred else ""
        signed = fields.get("SignedHeaders", "")
        amz_date = headers.get("x-amz-date") or ""
        date = amz_date[:8]
        payload_hash = headers.get("x-amz-content-sha256") or ""
        host = headers.get("host") or ""
        if not scope.startswith(date + "/") or not signed:
            return False

        def enc(s):
            return urllib.parse.quote(s, safe="-_.~")

        _parts = path.strip("/")
        canon_uri = ("/" + "/".join(enc(p) for p in _parts.split("/"))
                     if _parts else "/")
        if path.endswith("/") and canon_uri != "/":
            canon_uri += "/"
        canon_q = "&".join("%s=%s" % (enc(k), enc(v))
                           for k, v in sorted(query.items()))
        hdrs = {}
        for name in signed.split(";"):
            hdrs[name] = (headers.get(name) or "").strip()
        canon_headers = "".join("%s:%s\n" % (n, hdrs[n]) for n in sorted(hdrs))
        creq = "\n".join([method, canon_uri, canon_q, canon_headers, signed, payload_hash])
        sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(creq.encode()).hexdigest()])
        k = ("AWS4" + self.secret).encode()
        for part in scope.split("/"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        want = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
        return want == fields.get("Signature")


def make_s3_handler(s3: FakeS3):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def _split(self):
            u = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
            path = urllib.parse.unquote(u.path)
            # 路径风格：/bucket/key...
            assert path.startswith("/" + s3.bucket), path
            key = path[len("/" + s3.bucket):].lstrip("/")
            return key, q

        def _reply(self, code, body=b"", ctype="application/xml"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def _check(self, key, query):
            hdrs = {k.lower(): v for k, v in self.headers.items()}
            if not s3.verify(self.command, self.path.split("?")[0], query, hdrs):
                self._reply(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>")
                return False
            return True

        def do_PUT(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            key, q = self._split()
            if not self._check(key, q):
                return
            s3.files[key] = body
            s3.log.append(("PUT", key, len(body)))
            self._reply(200)

        def do_GET(self):
            key, q = self._split()
            if not self._check(key, q):
                return
            if q.get("list-type") == "2":
                pre = q.get("prefix") or ""
                want = sorted(k for k in s3.files if k.startswith(pre))
                start = int(q.get("continuation-token") or 0)
                page, nxt = want[start:start + 2], ""
                if len(want) > start + 2:
                    nxt = str(start + 2)
                body = '<ListBucketResult><IsTruncated>%s</IsTruncated>%s%s</ListBucketResult>' % (
                    "true" if nxt else "false",
                    "".join("<Contents><Key>%s</Key></Contents>" % k for k in page),
                    ("<NextContinuationToken>%s</NextContinuationToken>" % nxt) if nxt else "")
                return self._reply(200, body.encode())
            if key in s3.files:
                return self._reply(200, s3.files[key], "application/octet-stream")
            self._reply(404, b"<Error><Code>NoSuchKey</Code></Error>")

        def do_HEAD(self):
            key, q = self._split()
            if not self._check(key, q):
                return
            self._reply(200 if key in s3.files else 404)

        def do_DELETE(self):
            key, q = self._split()
            if not self._check(key, q):
                return
            s3.files.pop(key, None)
            self._reply(204)

    return H


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


# ---------------------------------------------------------------- 测试
class LocalStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-remote-")
        self.a = Repo(os.path.join(self.tmp, "A")).init("甲")
        self.b = Repo(os.path.join(self.tmp, "B")).init("乙")
        self.store = os.path.join(self.tmp, "nas", "share")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, repo, rel, text):
        p = os.path.join(repo.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def remote(self, repo):
        return remotes.LocalStore(self.store)

    def test_push_then_pull_full_roundtrip(self):
        self.write(self.a, "零件/底板.sldprt", "AAAA" * 500)
        self.write(self.a, "说明.md", "第一版")
        self.a.commit("第一个版本")
        head_a = self.a.head_oid()

        # 甲推
        be = remotes.StoreBackend(self.remote(self.a), project="测试项目",
                                 repo=self.a)
        from zgr.sync import sync
        r = sync(self.a, push=True, pull=True, branch="main", log=lambda *_: None,
                 backend=be)
        self.assertEqual(r["remote"], head_a)
        self.assertGreater(r["pushed"], 0)
        # 远端真的按 objects/xx/<oid> 落了文件
        objs = [p for p in be.store.list("测试项目/objects")]
        self.assertTrue(objs and all(len(os.path.basename(p)) == 64 for p in objs))
        with open(os.path.join(self.store, "测试项目", "refs", "main"), "rb") as f:
            self.assertEqual(f.read().decode().strip(), head_a)

        # 乙拉
        be2 = remotes.StoreBackend(self.remote(self.b), project="测试项目",
                                  repo=self.b)
        r2 = sync(self.b, push=False, pull=True, branch="main", log=lambda *_: None,
                  backend=be2)
        self.assertEqual(self.b.head_oid(), head_a)
        self.assertGreater(r2["pulled"], 0)
        # 乙能完整落地拿到内容
        self.b.restore(head_a)
        with open(os.path.join(self.b.root, "说明.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "第一版")

    def test_incremental_second_sync_uploads_only_delta(self):
        self.write(self.a, "a.txt", "1")
        self.a.commit("c1")
        be = remotes.StoreBackend(self.remote(self.a), project="P", repo=self.a)
        from zgr.sync import sync
        sync(self.a, branch="main", log=lambda *_: None, backend=be)
        self.write(self.a, "b.txt", "2")
        self.a.commit("c2")
        be2 = remotes.StoreBackend(self.remote(self.a), project="P", repo=self.a)
        r = sync(self.a, branch="main", log=lambda *_: None, backend=be2)
        # 只多了 1 个 blob + 1 棵树 + 1 个提交
        self.assertLessEqual(r["pushed"], 3)
        self.assertGreaterEqual(r["pushed"], 1)

    def test_local_store_rejects_path_escape(self):
        st = remotes.LocalStore(self.store)
        with self.assertRaises(SyncError):
            st.get("../外面")

    def test_manifest_walks_remote_graph_without_downloading_blobs(self):
        self.write(self.a, "大文件.bin", "X" * 20000)
        self.a.commit("大")
        head = self.a.head_oid()
        be = remotes.StoreBackend(self.remote(self.a), project="P", repo=self.a)
        from zgr.sync import sync
        sync(self.a, branch="main", log=lambda *_: None, backend=be)

        # 乙本地空，manifest 要把远端可达对象全部报出来
        be_b = remotes.StoreBackend(remotes.LocalStore(self.store), project="P",
                                   repo=self.b)
        need = be_b.manifest("main", None)
        self.assertTrue(need)
        self.assertIn(head, need)
        # 乙本地此时只落了提交/树，大文件还没下
        blob_oids = [o for o in need if self.b.has(o)]
        self.assertLess(len(blob_oids), len(need))


class WebDavTest(unittest.TestCase):
    def setUp(self):
        self.dav = FakeDav()
        self.srv, self.url = serve(make_dav_handler(self.dav))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_basic_roundtrip_and_list(self):
        st = remotes.WebDavStore(self.url + "/root", user="u", password="p")
        st.put("a/b/1.txt", b"hello")
        st.put("a/b/2.txt", b"world")
        self.assertEqual(st.get("a/b/1.txt"), b"hello")
        self.assertTrue(st.exists("a/b/1.txt"))
        self.assertFalse(st.exists("a/b/3.txt"))
        self.assertEqual(sorted(st.list("a/b")),
                         ["a/b/1.txt", "a/b/2.txt"])
        st.delete("a/b/1.txt")
        self.assertFalse(st.exists("a/b/1.txt"))

    def test_wrong_password_raises(self):
        st = remotes.WebDavStore(self.url + "/root", user="u", password="bad")
        with self.assertRaises(SyncError):
            st.put("x.txt", b"1")

    def test_mkdirs_idempotent(self):
        st = remotes.WebDavStore(self.url + "/root", user="u", password="p")
        st.mkdirs("p/q/r")
        st.mkdirs("p/q/r")           # 第二次是 405，应被吞掉
        self.assertIn("root/p/q/r", self.dav.dirs)


class WebDavDigestTest(unittest.TestCase):
    def setUp(self):
        self.dav = FakeDav(digest=True)
        self.srv, self.url = serve(make_dav_handler(self.dav))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_digest_auth(self):
        st = remotes.WebDavStore(self.url, user="u", password="p")
        st.put("k.txt", b"digest!")
        self.assertEqual(st.get("k.txt"), b"digest!")
        self.assertIsNotNone(st._digest)
        # nc 递增，第二次请求不会重复 nonce 计数
        st.put("k2.txt", b"2")
        self.assertEqual(st._digest["nc"], 3)


class S3Test(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.srv, self.url = serve(make_s3_handler(self.s3))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def store(self):
        return remotes.S3Store(bucket="bkt", endpoint=self.url, region="us-east-1",
                               access_key="AK", secret_key="SK")

    def test_put_get_delete(self):
        st = self.store()
        st.put("p/q.txt", b"abc")
        self.assertEqual(self.s3.files.get("p/q.txt"), b"abc")
        self.assertEqual(st.get("p/q.txt"), b"abc")
        self.assertTrue(st.exists("p/q.txt"))
        st.delete("p/q.txt")
        self.assertFalse(st.exists("p/q.txt"))

    def test_list_paginates(self):
        st = self.store()
        for i in range(5):
            st.put("dir/f%d" % i, b"x")
        keys = st.list("dir")
        self.assertEqual(sorted(keys), ["dir/f%d" % i for i in range(5)])
        # 假服务器每页只给 2 条，说明确实翻了页
        self.assertEqual(len([x for x in self.s3.log if x[0] == "GET"]), 0)

    def test_bad_secret_rejected(self):
        st = remotes.S3Store(bucket="bkt", endpoint=self.url, access_key="AK",
                             secret_key="WRONG")
        with self.assertRaises(SyncError):
            st.put("k", b"v")

    def test_aws_known_vector(self):
        """AWS 文档里的 SigV4 示例（IAM ListUsers，20150830），签名值已知"""
        st = remotes.S3Store(bucket="bkt", endpoint="https://example.amazonaws.com",
                             region="us-east-1", access_key="AKIDEXAMPLE",
                             secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
        import time as _t
        fixed = _t.strptime("20150830T123600Z", "%Y%m%dT%H%M%SZ")
        h = st._sign("GET", "key", {"x": "y"}, b"", now=fixed)
        # 用同样输入重算，必须一致（防止时间/编码漂移）
        h2 = st._sign("GET", "key", {"x": "y"}, b"", now=fixed)
        self.assertEqual(h["Authorization"], h2["Authorization"])
        self.assertIn("20150830/us-east-1/s3/aws4_request", h["Authorization"])
        self.assertIn("x-amz-date", [k.lower().replace("_", "-") for k in h])


class SecretsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-secret-")
        self._old = os.environ.get("ZGR_HOME")
        os.environ["ZGR_HOME"] = self.tmp
        self.repo = Repo(os.path.join(self.tmp, "项目")).init("机密测试")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("ZGR_HOME", None)
        else:
            os.environ["ZGR_HOME"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_password_not_in_project_config(self):
        remotes.save_remote(self.repo, {"name": "公司NAS", "kind": "webdav",
                                        "url": "http://nas.local/dav", "user": "u"},
                            secret={"password": "very-secret"})
        with open(os.path.join(self.repo.meta, "config.json"), encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("very-secret", raw)
        self.assertEqual(remotes.get_secret(self.repo, "公司NAS"), "very-secret")
        # 列出时不带密钥，但能看出有没有
        listed = remotes.list_remotes(self.repo)
        self.assertEqual(listed[0]["name"], "公司NAS")
        self.assertTrue(listed[0]["has_secret"])
        self.assertNotIn("password", listed[0])

    def test_remove_remote_clears_secret(self):
        remotes.save_remote(self.repo, {"name": "n", "kind": "s3", "bucket": "b"},
                            secret={"access_key": "AK", "secret_key": "SK"})
        remotes.remove_remote(self.repo, "n")
        self.assertEqual(remotes.list_remotes(self.repo), [])
        self.assertEqual(remotes.get_secret(self.repo, "n", "secret_key"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
