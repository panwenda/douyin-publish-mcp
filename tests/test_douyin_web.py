"""读取通道的单测：凭据解析、请求构造、错误分类、DTO 裁剪。

★ 这些用例**不联网**：`urlopen` 全部被打桩。钉的是三条契约：
  ① 凭据只从 sau 的 storage_state / 显式配置里来，缺了就报"去登录"而不是"空结果"；
  ② 风控（blocked / 空响应 / 非 JSON）必须与"真的没搜到"分开；
  ③ 交给模型的字段是裁剪过的（不把上游原始结构整份塞出去）。
"""

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from douyin_publish_mcp import douyin_cred, douyin_web
from douyin_publish_mcp.douyin_web import DouyinWebClient, DouyinWebError, WebConfig


class FakeResp:
    """假的 urlopen 返回值（够用就行：read() + headers）"""

    def __init__(self, payload, encoding: str = ""):
        self._payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Encoding": encoding}

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def storage_state(cookies):
    return json.dumps(
        {
            "cookies": [
                {"name": n, "value": v, "domain": d, "path": "/"}
                for n, v, d in cookies
            ],
            "origins": [],
        }
    )


class CredentialCase(unittest.TestCase):
    def test_storage_state_只取抖音域的_cookie(self):
        raw = storage_state(
            [
                ("sessionid", "s1", ".douyin.com"),
                ("ttwid", "t1", ".douyin.com"),
                ("other_site", "x", ".example.com"),
            ]
        )
        pairs = douyin_cred.cookie_header_from_cookies(douyin_cred.parse_storage_state(raw))
        names = [n for n, _ in pairs]
        self.assertIn("sessionid", names)
        self.assertIn("ttwid", names)
        self.assertNotIn("other_site", names)

    def test_坏_json_返回空而不是抛异常(self):
        self.assertEqual(douyin_cred.parse_storage_state("{ not json"), [])

    def test_裸_cookie_串_可解析且识别登录态(self):
        cred = douyin_cred.credential_from_raw("sessionid=abc; ttwid=def; odd=1", "测试")
        self.assertTrue(cred.has_login)
        self.assertTrue(cred.has_device)
        self.assertTrue(cred.usable)

    def test_cookie_头_常用键排在前面(self):
        cred = douyin_cred.credential_from_raw("zzz=1; sessionid=abc; ttwid=def", "测试")
        header, keys = cred.cookie, cred.keys
        self.assertTrue(header.startswith("ttwid=def"))
        self.assertEqual(keys[:2], ["ttwid", "sessionid"])

    def test_describe_不回显_cookie_值(self):
        cred = douyin_cred.credential_from_raw("sessionid=SUPERSECRET; ttwid=t", "测试来源")
        text = cred.describe()
        self.assertIn("sessionid", text)
        self.assertNotIn("SUPERSECRET", text)

    def test_优先用显式配置文件(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explicit = root / "cookie.json"
            explicit.write_text(storage_state([("sessionid", "explicit", ".douyin.com")]), "utf-8")
            project = root / "sau"
            (project / "cookies").mkdir(parents=True)
            (project / "cookies" / "douyin_main.json").write_text(
                storage_state([("sessionid", "from-sau", ".douyin.com")]), "utf-8"
            )
            from douyin_publish_mcp.sau import SauConfig

            cfg = SauConfig(project_dir=str(project))
            with mock.patch.dict("os.environ", {"DOUYIN_COOKIE_FILE": str(explicit)}):
                cred = douyin_cred.resolve_credential(cfg, "main")
            self.assertIn("explicit", cred.cookie)
            self.assertIn("DOUYIN_COOKIE_FILE", cred.source)

    def test_回落到_sau_凭据文件(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "sau"
            (project / "cookies").mkdir(parents=True)
            (project / "cookies" / "douyin_main.json").write_text(
                storage_state([("sessionid", "from-sau", ".douyin.com"), ("ttwid", "t", ".douyin.com")]),
                "utf-8",
            )
            from douyin_publish_mcp.sau import SauConfig

            cfg = SauConfig(project_dir=str(project))
            env = {k: v for k, v in __import__("os").environ.items()}
            env.pop("DOUYIN_COOKIE_FILE", None)
            env.pop("DOUYIN_COOKIE", None)
            with mock.patch.dict("os.environ", env, clear=True):
                cred = douyin_cred.resolve_credential(cfg, "main")
            self.assertTrue(cred.usable)
            self.assertIn("sau 凭据文件", cred.source)

    def test_没有凭据文件时不抛异常而是_empty(self):
        from douyin_publish_mcp.sau import SauConfig

        with tempfile.TemporaryDirectory() as tmp:
            cfg = SauConfig(project_dir=str(Path(tmp) / "nope"))
            env = {k: v for k, v in __import__("os").environ.items()}
            env.pop("DOUYIN_COOKIE_FILE", None)
            env.pop("DOUYIN_COOKIE", None)
            with mock.patch.dict("os.environ", env, clear=True):
                cred = douyin_cred.resolve_credential(cfg, "main")
        self.assertFalse(cred.usable)
        self.assertEqual(cred.cookie, "")


    def test_只配_SAU_CMD_时给出补救路径而不是抛异常(self):
        """只用 SAU_CMD（没有项目目录）时，读取定位不到凭据文件 —— 要指路，不要崩。"""
        import os

        from douyin_publish_mcp.sau import SauConfig

        cfg = SauConfig(cmd=r"C:\some\where\sau.exe")
        env = {k: v for k, v in os.environ.items()}
        env.pop("DOUYIN_COOKIE_FILE", None)
        env.pop("DOUYIN_COOKIE", None)
        with mock.patch.dict("os.environ", env, clear=True):
            cred = douyin_cred.resolve_credential(cfg, "main")
        self.assertFalse(cred.usable)
        self.assertIn("DOUYIN_COOKIE_FILE", cred.describe())


class RequestCase(unittest.TestCase):
    def setUp(self):
        self.cred = douyin_cred.credential_from_raw("sessionid=s; ttwid=t", "测试")
        self.client = DouyinWebClient(self.cred, WebConfig(timeout=5, retries=0))

    def test_没凭据时报_no_credential_并指明去登录(self):
        client = DouyinWebClient(douyin_cred.Credential(), WebConfig())
        with self.assertRaises(DouyinWebError) as ctx:
            client.search_videos("美食")
        self.assertEqual(ctx.exception.kind, "no_credential")
        self.assertIn("douyin_account_login", ctx.exception.describe())

    def test_请求带上固定指纹与_msToken(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["headers"] = {k.lower(): v for k, v in request.header_items()}
            return FakeResp({"data": []})

        with mock.patch.object(douyin_web.urllib.request, "urlopen", fake_urlopen):
            self.client.search_videos("美食", count=3)
        self.assertIn("device_platform=webapp", seen["url"])
        self.assertIn("aid=6383", seen["url"])
        self.assertIn("msToken=", seen["url"])
        # 常用键（ttwid/sessionid）排前：风控会看 cookie 的组织顺序
        self.assertEqual(seen["headers"].get("cookie"), "ttwid=t; sessionid=s")

    def test_评分_blocked_单独分类(self):
        with mock.patch.object(
            douyin_web.urllib.request, "urlopen", lambda *a, **k: FakeResp({"status_code": 0, "status_msg": "blocked"})
        ):
            with self.assertRaises(DouyinWebError) as ctx:
                self.client.search_videos("美食")
        self.assertEqual(ctx.exception.kind, "blocked")
        self.assertIn("ttwid", ctx.exception.hint)

    def test_空响应判定为风控而不是没搜到(self):
        with mock.patch.object(douyin_web.urllib.request, "urlopen", lambda *a, **k: FakeResp(b"")):
            with self.assertRaises(DouyinWebError) as ctx:
                self.client.search_videos("美食")
        self.assertEqual(ctx.exception.kind, "blocked")

    def test_非_JSON_响应给出可读原因(self):
        with mock.patch.object(
            douyin_web.urllib.request, "urlopen", lambda *a, **k: FakeResp(b"<html>verify</html>")
        ):
            with self.assertRaises(DouyinWebError) as ctx:
                self.client.search_videos("美食")
        self.assertEqual(ctx.exception.kind, "parse")
        self.assertIn("验证码", ctx.exception.hint)

    def test_gzip_响应能解开(self):
        payload = gzip.compress(json.dumps({"data": []}).encode("utf-8"))
        with mock.patch.object(
            douyin_web.urllib.request, "urlopen", lambda *a, **k: FakeResp(payload, "gzip")
        ):
            self.assertEqual(self.client.search_videos("美食"), [])

    def test_网络错误归类为_network_并提示内网代理(self):
        import urllib.error

        def boom(*a, **k):
            raise urllib.error.URLError("no route")

        with mock.patch.object(douyin_web.urllib.request, "urlopen", boom):
            with self.assertRaises(DouyinWebError) as ctx:
                self.client.search_videos("美食")
        self.assertEqual(ctx.exception.kind, "network")
        self.assertIn("代理", ctx.exception.hint)


class PickCase(unittest.TestCase):
    def test_搜索结果的_aweme_info_包裹能解开(self):
        item = {
            "aweme_info": {
                "aweme_id": "123",
                "desc": "标题",
                "create_time": 1700000000,
                "author": {"nickname": "某人", "sec_uid": "MS4wLjABAAAA"},
                "video": {"play_addr": {"url_list": ["http://x/playwm/a.mp4"]}, "duration": 15000},
                "statistics": {"digg_count": 7},
            }
        }
        client = DouyinWebClient(douyin_cred.credential_from_raw("ttwid=t", "测试"), WebConfig())
        with mock.patch.object(
            douyin_web.urllib.request, "urlopen", lambda *a, **k: FakeResp({"data": [item]})
        ):
            videos = client.search_videos("x")
        self.assertEqual(len(videos), 1)
        v = videos[0]
        self.assertEqual(v["aweme_id"], "123")
        self.assertEqual(v["author"]["nickname"], "某人")
        self.assertEqual(v["stats"]["like"], 7)
        # 去水印 + 分享地址
        self.assertNotIn("playwm", v["download_url"])
        self.assertEqual(v["share_url"], "https://www.douyin.com/video/123")

    def test_缺_id_的作品被丢掉而不是报错(self):
        self.assertEqual(douyin_web.pick_video({"desc": "没有 id"}), {})

    def test_用户字段缺失也不崩(self):
        picked = douyin_web.pick_user({"nickname": "某人", "sec_uid": "MS4w"})
        self.assertEqual(picked["nickname"], "某人")
        self.assertEqual(picked["follower_count"], 0)
        self.assertEqual(picked["profile_url"], "https://www.douyin.com/user/MS4w")

    def test_翻页尊重上限(self):
        pages = [
            {"aweme_list": [{"aweme_id": "1"}], "has_more": 1, "max_cursor": 10},
            {"aweme_list": [{"aweme_id": "2"}], "has_more": 1, "max_cursor": 20},
            {"aweme_list": [{"aweme_id": "3"}], "has_more": 0, "max_cursor": 30},
        ]
        seen = {"n": 0}

        def fake_urlopen(request, timeout=None):
            page = pages[min(seen["n"], len(pages) - 1)]
            seen["n"] += 1
            return FakeResp(page)

        cred = douyin_cred.credential_from_raw("ttwid=t", "测试")
        client = DouyinWebClient(cred, WebConfig(page_interval=0))
        with mock.patch.object(douyin_web.urllib.request, "urlopen", fake_urlopen):
            videos = client.user_videos_all("MS4w", limit=2)
        self.assertEqual([v["aweme_id"] for v in videos], ["1", "2"])
        self.assertEqual(seen["n"], 2)


class ExtractAwemeIdTest(unittest.TestCase):
    """作品 id 抽取。

    ★ 「纯数字 id」是**回归点**：`douyin_video_detail` 会先把入参过一遍
    `extract_aweme_id`（容错"模型把整条链接塞进 aweme_id"），而兜底那条正则
    `\\d{15,25}` 没有分组，写成 `group(1)` 会抛 `IndexError: no such group`
    —— 2026-09-29 真机验收就是在这里挂的（搜索通过、详情内部错误）。
    """

    def test_纯数字id不再抛异常(self):
        self.assertEqual(
            douyin_web.extract_aweme_id("7664911070490126501"), "7664911070490126501"
        )

    def test_各种链接形态都能抽出id(self):
        cases = {
            "https://www.douyin.com/video/7664911070490126501": "7664911070490126501",
            "https://www.douyin.com/discover?modal_id=7664911070490126501": "7664911070490126501",
            "https://www.douyin.com/user/MS4wLjABAAAA/7664911070490126501": "7664911070490126501",
        }
        for text, expected in cases.items():
            self.assertEqual(douyin_web.extract_aweme_id(text), expected, text)

    def test_抽不到时返回空串而不是抛异常(self):
        for text in ("没有 id 的文本", "", "1234"):
            self.assertEqual(douyin_web.extract_aweme_id(text), "", text)

    def test_分享短链与id混在一段话里(self):
        text = "7.65 复制打开抖音 https://v.douyin.com/iRabcdEf/ 看这个 7664911070490126501"
        self.assertEqual(douyin_web.extract_aweme_id(text), "7664911070490126501")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
