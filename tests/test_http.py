"""streamable_http 形态的端到端单测（真起本机服务，再当客户端打它）。

钉三件事：
1. **协议形状**：`POST /mcp` 的 initialize / notifications / tools/* 回得对，
   且带上客户端需要的 `mcp-session-id`（pm-web-app 的 mcp_session.rs 会回带它）。
2. **鉴权**：配了 token 就真的挡住（/mcp 401），而 /health 永远能探活。
3. **登录页**：`/` 能打开、`/check` 能真的跑起来并把状态落到 `/state.json`。
"""

import http.client
import json
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from douyin_publish_mcp import account as account_mod
from douyin_publish_mcp import config as config_mod
from douyin_publish_mcp import douyin_browser as browser
from douyin_publish_mcp import http_server
from douyin_publish_mcp import login_session
from douyin_publish_mcp.account import AccountCheck

TOOL_NAMES = {
    "douyin_account_status",
    "douyin_account_login",
    "douyin_account_logout",
    "douyin_publish_video",
    "douyin_publish_note",
    "douyin_search_videos",
    "douyin_video_detail",
    "douyin_user_profile",
    "douyin_my_profile",
    "douyin_parse_share_link",
}


class HttpCase(unittest.TestCase):
    token = ""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "videos").mkdir()
        (self.root / "videos" / "a.mp4").write_bytes(b"v" * 16)
        # 凭据默认落在 DOUYIN_DATA_DIR（本服务自己管登录态）
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "douyin_main.json").write_text(
            '{"cookies":[{"name":"sessionid","value":"x","domain":".douyin.com"},'
            '{"name":"ttwid","value":"t","domain":".douyin.com"}]}',
            encoding="utf-8",
        )
        env = mock.patch.dict(
            "os.environ",
            {"DOUYIN_DATA_DIR": str(self.data), "DOUYIN_COOKIE_FILE": "", "DOUYIN_COOKIE": ""},
        )
        env.start()
        self.addCleanup(env.stop)
        self.cfg = config_mod.RuntimeConfig(media_dir=str(self.root), timeout=60)
        self.app = http_server.App(self.cfg, token=self.token)
        self.httpd = http_server.ThreadingHTTPServer(("127.0.0.1", 0), http_server.make_handler(self.app))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.app.port = self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    # ── 客户端辅助 ──────────────────────────────────────────
    def request(self, method, path, body=None, token=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        payload = raw
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "application/json, text/event-stream"
        tok = self.token if token is None else token
        if tok:
            headers["Authorization"] = "Bearer " + tok
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        result = (resp.status, dict(resp.getheaders()), data)
        conn.close()
        return result

    def mcp(self, body, token=None):
        status, headers, data = self.request("POST", "/mcp", body=body, token=token)
        parsed = json.loads(data.decode("utf-8")) if data else None
        return status, headers, parsed

    def state(self):
        status, _headers, data = self.request("GET", "/state.json")
        self.assertEqual(status, 200)
        return json.loads(data.decode("utf-8"))


class TestProtocol(HttpCase):
    def test_initialize_回协议版本与会话头(self):
        status, headers, body = self.mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(body["result"]["serverInfo"]["name"], "douyin-publish-mcp")
        self.assertTrue(headers.get("mcp-session-id"), headers)
        self.assertEqual(headers.get("MCP-Protocol-Version"), "2024-11-05")

    def test_通知回202且无正文(self):
        status, _headers, body = self.mcp({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(status, 202)
        self.assertIsNone(body)

    def test_tools_list(self):
        status, _headers, body = self.mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(status, 200)
        self.assertEqual({t["name"] for t in body["result"]["tools"]}, TOOL_NAMES)

    def test_ping(self):
        _s, _h, body = self.mcp({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.assertEqual(body["result"], {})

    def test_坏JSON回400与_parse_error(self):
        status, _headers, data = self.request("POST", "/mcp", raw=b"{ not json")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data.decode())["error"]["code"], -32700)

    def test_未知方法回_32601_但HTTP仍是200(self):
        status, _headers, body = self.mcp({"jsonrpc": "2.0", "id": 4, "method": "nope"})
        self.assertEqual(status, 200)
        self.assertEqual(body["error"]["code"], -32601)

    def test_GET_mcp_回405(self):
        status, headers, _data = self.request("GET", "/mcp")
        self.assertEqual(status, 405)
        self.assertIn("POST", headers.get("Allow", ""))

    def test_DELETE_mcp_兜住会话结束(self):
        status, _headers, data = self.request("DELETE", "/mcp")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data.decode())["ok"])

    def test_health_探活(self):
        status, _headers, data = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data.decode())["status"], "ok")


class TestAuth(HttpCase):
    token = "s3cret-token"

    def test_health_免鉴权(self):
        self.assertEqual(self.request("GET", "/health", token="")[0], 200)

    def test_未带token被拒(self):
        status, headers, _data = self.request("POST", "/mcp", body={"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="")
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")

    def test_错误token被拒(self):
        status, _h, _d = self.request(
            "POST", "/mcp", body={"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="wrong"
        )
        self.assertEqual(status, 401)

    def test_正确token放行(self):
        status, _h, body = self.mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"], {})

    def test_页面路由可用_query_token_打开(self):
        status, _h, data = self.request("GET", "/?token=" + self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"<html", data)

    def test_页面路由无token被拒(self):
        self.assertEqual(self.request("GET", "/", token="")[0], 401)

    def test_非环回无token拒绝启动(self):
        with self.assertRaises(SystemExit):
            http_server.serve_http(self.cfg, host="0.0.0.0", port=0, token="")

    def test_401_不吃掉长连接_后续请求仍正常(self):
        """★ 401 时必须把请求体读干净：否则残留字节会被当成下一个请求行，连接错位。"""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode("utf-8")
        # 第一条：不带 token（应 401）
        conn.request("POST", "/mcp", body=payload, headers={"Content-Type": "application/json"})
        first = conn.getresponse()
        first.read()
        self.assertEqual(first.status, 401)
        # 第二条：同一条连接、带上正确 token（应 200；若上面没读干净，这里会错位）
        conn.request(
            "POST", "/mcp", body=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token},
        )
        second = conn.getresponse()
        body = json.loads(second.read().decode("utf-8"))
        conn.close()
        self.assertEqual(second.status, 200)
        self.assertEqual(body["result"], {})


class TestPublishGateOverHttp(HttpCase):
    def test_预检不执行发布(self):
        with mock.patch.object(browser, "run_helper") as run:
            status, _h, body = self.mcp(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "douyin_publish_video",
                        "arguments": {"account": "main", "file": "videos/a.mp4", "title": "冒烟"},
                    },
                }
            )
        run.assert_not_called()
        self.assertEqual(status, 200)
        text = body["result"]["content"][0]["text"]
        self.assertIn("还没有发给抖音", text)
        self.assertRegex(text, r'plan_id="[0-9a-f]{12}"')

    def test_确认后执行一次(self):
        _s, _h, prep = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "douyin_publish_video",
                    "arguments": {"account": "main", "file": "videos/a.mp4", "title": "冒烟"},
                },
            }
        )
        plan_id = re.search(r'plan_id="([0-9a-f]{12})"', prep["result"]["content"][0]["text"]).group(1)
        fake = {"ok": True, "state": "published", "message": "ok", "steps": []}
        with mock.patch.object(browser, "run_helper", return_value=fake) as run:
            _s, _h, body = self.mcp(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "douyin_publish_video",
                        "arguments": {
                            "account": "main",
                            "file": "videos/a.mp4",
                            "title": "冒烟",
                            "plan_id": plan_id,
                            "confirm": True,
                        },
                    },
                }
            )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[1]["action"], "publish_video")
        self.assertFalse(body["result"]["isError"])


class TestLoginPage(HttpCase):
    def test_状态页可打开且显示配置(self):
        status, _h, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        text = data.decode("utf-8")
        self.assertIn("本机状态页", text)
        self.assertIn("DOUYIN_MEDIA_DIR", text)
        self.assertIn("/mcp", text)          # 端点写在页面上，用户能直接粘去客户端

    def test_check_动作真的跑起来并落状态(self):
        fake = AccountCheck(account="main", logged_in=True, ok=True, output="已登录")
        with mock.patch.object(login_session, "check_account", return_value=fake):
            status, headers, _data = self.request(
                "POST", "/check", raw=b"account=main",
                token=self.token,
            )
            # 表单提交要 303 回状态页：刷新不会重复提交
            self.assertEqual(status, 303)
            self.assertEqual(headers.get("Location"), "/")
            deadline = time.time() + 5
            while time.time() < deadline:
                if not self.state()["checking"]:
                    break
                time.sleep(0.05)
        st = self.state()
        self.assertFalse(st["checking"], st)
        self.assertIs(st["loggedIn"], True)
        self.assertEqual(st["lastCheck"]["account"], "main")
        self.assertIn("已登录", st["notice"])

    def test_账号名非法被拒且不跑CLI(self):
        with mock.patch.object(login_session, "check_account") as run:
            _s, _h, _d = self.request("POST", "/check", raw=b"account=bad name;rm -rf")
            time.sleep(0.2)
        run.assert_not_called()
        self.assertIn("账号名", self.state()["notice"])

    def test_登录中的二维码能通过接口取到(self):
        # 造一张二维码图，并让快照指向它（真链路上由登录会话发现）
        qr = self.root / "qrcode.png"
        qr.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        real = self.app.sessions.snapshot

        def fake_snapshot():
            snap = real()
            snap["qrAvailable"] = True
            snap["qrPath"] = str(qr)
            return snap

        with mock.patch.object(self.app.sessions, "snapshot", side_effect=fake_snapshot):
            status, headers, data = self.request("GET", "/qr.png")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Content-Type"), "image/png")
            self.assertTrue(data.startswith(b"\x89PNG"))
            self.assertTrue(self.state()["qrAvailable"])

    def test_没二维码时_qr接口回404(self):
        self.assertEqual(self.request("GET", "/qr.png")[0], 404)

    def test_取消等待扫码(self):
        with mock.patch.object(self.app.sessions, "cancel_login") as cancel:
            status, _h, _d = self.request("POST", "/cancel-login", raw=b"")
        self.assertEqual(status, 303)
        cancel.assert_called_once()
        self.assertIn("取消", self.state()["notice"])

    def test_重置登录态_页面两步(self):
        data_dir = Path(self.root) / "data"
        data_dir.mkdir(exist_ok=True)
        cred = data_dir / "douyin_main.json"
        cred.write_text('{"sessionid":"x"}', encoding="utf-8")
        # 凭据默认落点由 DOUYIN_DATA_DIR 决定（v0.3.0 起本服务自己管登录态）
        env = mock.patch.dict("os.environ", {"DOUYIN_DATA_DIR": str(data_dir)})
        env.start()
        try:
            # 第一步：只看不删
            status, _h, _d = self.request("POST", "/logout", raw=b"account=main&confirm=0")
            self.assertEqual(status, 303)
            self.assertTrue(cred.is_file(), "没确认就不许删")
            self.assertIn(str(cred), self.state()["notice"])

            # 第二步：确认后真删
            self.request("POST", "/logout", raw=b"account=main&confirm=1")
            self.assertFalse(cred.exists())
            self.assertIn("已删除", self.state()["notice"])
        finally:
            env.stop()

    def test_状态页带重置登录态表单(self):
        _s, _h, data = self.request("GET", "/")
        text = data.decode("utf-8")
        self.assertIn("/logout", text)
        self.assertIn("确认删除凭据", text)
        self.assertIn("取消等待扫码", text)


class TestEntryPoint(unittest.TestCase):
    def test_默认是stdio_http要显式开(self):
        from douyin_publish_mcp.__main__ import build_parser

        self.assertFalse(build_parser().parse_args([]).http)
        args = build_parser().parse_args(["--http", "--port", "18099", "--token", "t"])
        self.assertTrue(args.http)
        self.assertEqual(args.port, 18099)
        self.assertEqual(args.token, "t")

    def test_端口可复用检测用的_ephemeral_绑定(self):
        # 说明性用例：测试全程用 0 端口，避免和真实部署的 18080 撞车
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            self.assertGreater(s.getsockname()[1], 0)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
