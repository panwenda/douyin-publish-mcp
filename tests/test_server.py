"""MCP 协议层与发布门禁的单测（不真跑 sau：`sau.run` 全部被打桩）。

★ 这里钉的是最要紧的一条契约：**没有确认（plan_id + confirm=true）就不许执行发布**。
  它一旦回退，模型一次误判就直接落到用户账号上，而发布是不可撤销的。
"""

import json
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from douyin_publish_mcp import douyin_web, sau
from douyin_publish_mcp.server import Server, plan_fingerprint


class FakeProc:
    """假的 `sau douyin login` 进程：`exit_after=None` 表示"永不等不到扫码结果"。

    ★ 为什么造它：登录会话必须是**异步**的（等扫码要几分钟），
      用一个真进程测会让用例要么很慢、要么不确定。这里只要行为对得上就够了：
      `wait()` 能返回/能超时、`terminate()` 能被记录。
    """

    def __init__(self, exit_after=None, lines=()):
        import io

        self.stdout = io.BytesIO(b"".join(lines))
        self.stderr = io.BytesIO(b"")
        self._exit = exit_after
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        if self._exit is None:
            if timeout is None:
                time.sleep(0.2)
                return None
            time.sleep(min(timeout, 0.05))
            raise subprocess.TimeoutExpired("fake-sau", timeout)
        return self._exit

    def terminate(self):
        self.terminated = True
        self._exit = 1

    def kill(self):
        self.killed = True
        self._exit = 1

TOOL_NAMES = {
    "douyin_account_status",
    "douyin_account_login",
    "douyin_account_logout",
    "douyin_publish_video",
    "douyin_publish_note",
    # 读取（第一批）：与发布共用登录态，但不吃 confirm
    "douyin_search_videos",
    "douyin_video_detail",
    "douyin_user_profile",
    "douyin_my_profile",
    "douyin_parse_share_link",
}


class ServerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "videos").mkdir()
        (self.root / "videos" / "a.mp4").write_bytes(b"v" * 32)
        (self.root / "imgs").mkdir()
        for i in (1, 2):
            (self.root / "imgs" / f"{i}.png").write_bytes(b"p" * 32)
        self.cfg = sau.SauConfig(cmd="sau", media_dir=str(self.root), timeout=60)
        self.server = Server(self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    # ── 辅助 ────────────────────────────────────────────────
    def call(self, name, arguments):
        resp = self.server.handle(
            {"jsonrpc": "2.0", "id": "1", "method": "tools/call",
             "params": {"name": name, "arguments": arguments}}
        )
        self.assertEqual(resp["id"], "1")
        return resp["result"]

    def text_of(self, result):
        return "\n".join(c.get("text", "") for c in result["content"])

    def plan_id_of(self, result):
        m = re.search(r'plan_id="([0-9a-f]{12})"', self.text_of(result))
        self.assertIsNotNone(m, f"预检结果里应带 plan_id：{self.text_of(result)}")
        return m.group(1)


class TestProtocol(ServerCase):
    def test_initialize_回协议版本与身份(self):
        resp = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        result = resp["result"]
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertEqual(result["serverInfo"]["name"], "douyin-publish-mcp")
        self.assertIn("tools", result["capabilities"])

    def test_通知不回响应(self):
        self.assertIsNone(
            self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_tools_list_十个工具且_schema_完整(self):
        resp = self.server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        tools = resp["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, TOOL_NAMES)
        for t in tools:
            self.assertTrue(t["description"].strip(), t["name"])
            self.assertEqual(t["inputSchema"]["type"], "object")
            # ★ 必须有 required 字段（可以是空数组：读自己这类工具确实没有必填参数），
            #   缺了它客户端给的 schema 就不完整
            self.assertIn("required", t["inputSchema"], t["name"])
        # 发布工具必须把"先确认"写进描述 —— 模型只会读这里
        for name in ("douyin_publish_video", "douyin_publish_note"):
            desc = next(t["description"] for t in tools if t["name"] == name)
            self.assertIn("不会发布任何东西", desc)
            self.assertIn("confirm=true", desc)
        # 登录工具要如实说明"二维码在用户屏幕上"（抖音这条链路拿不到二维码图）
        login_desc = next(t["description"] for t in tools if t["name"] == "douyin_account_login")
        self.assertIn("用户自己的屏幕上", login_desc)
        # 重置登录态也要两步
        logout_desc = next(t["description"] for t in tools if t["name"] == "douyin_account_logout")
        self.assertIn("confirm", logout_desc)

    def test_补齐的参数都在_schema_里(self):
        tools = self.server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})["result"]["tools"]
        by_name = {t["name"]: t["inputSchema"]["properties"] for t in tools}
        for key in (
            "thumbnail_portrait",
            "thumbnail_landscape",
            "product_link",
            "product_title",
            "declaration",
            "collection",
        ):
            self.assertIn(key, by_name["douyin_publish_video"], key)
        for key in ("note_file", "bgm"):
            self.assertIn(key, by_name["douyin_publish_note"], key)

    def test_ping(self):
        resp = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.assertEqual(resp["result"], {})

    def test_未知方法与未知工具(self):
        r1 = self.server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
        self.assertEqual(r1["error"]["code"], -32601)
        r2 = self.server.handle(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "not_a_tool", "arguments": {}}}
        )
        self.assertEqual(r2["error"]["code"], -32602)

    def test_参数错误以工具错误返回_不崩(self):
        result = self.call("douyin_publish_video", {"account": "main"})
        self.assertTrue(result["isError"])
        self.assertIn("file", self.text_of(result))


class TestPublishGate(ServerCase):
    """★ 发布门禁：预检不执行、确认对不上不执行、对上才执行一次"""

    def test_预检只交计划_绝不执行发布(self):
        with mock.patch.object(sau, "run") as run:
            result = self.call(
                "douyin_publish_video",
                {"account": "main", "file": "videos/a.mp4", "title": "周末随拍"},
            )
        run.assert_not_called()
        text = self.text_of(result)
        self.assertIn("还没有发给抖音", text)
        self.assertIn("周末随拍", text)
        # 计划里的素材是**绝对路径**（JSON 转义过的反斜杠要先还原再比对）
        self.assertIn(str(self.root / "videos" / "a.mp4"), text.replace("\\\\", "\\"))
        self.plan_id_of(result)

    def test_确认且_plan_id_一致_才执行一次(self):
        prep = self.call(
            "douyin_publish_video",
            {"account": "main", "file": "videos/a.mp4", "title": "周末随拍"},
        )
        plan_id = self.plan_id_of(prep)
        fake = sau.SauResult(argv=["sau"], exit_code=0, stdout="上传完成", stderr="")
        with mock.patch.object(sau, "run", return_value=fake) as run:
            result = self.call(
                "douyin_publish_video",
                {
                    "account": "main",
                    "file": "videos/a.mp4",
                    "title": "周末随拍",
                    "plan_id": plan_id,
                    "confirm": True,
                },
            )
        self.assertEqual(run.call_count, 1)
        argv = run.call_args.args[1]
        self.assertEqual(argv[:2], ["douyin", "upload-video"])
        self.assertIn("--headless", argv)
        self.assertFalse(result["isError"])
        # ★ 不许把"命令成功"说成"已发布上线"
        self.assertIn("审核", self.text_of(result))

    def test_plan_id_对不上时拒绝执行(self):
        with mock.patch.object(sau, "run") as run:
            result = self.call(
                "douyin_publish_video",
                {
                    "account": "main",
                    "file": "videos/a.mp4",
                    "title": "周末随拍",
                    "plan_id": "deadbeef1234",
                    "confirm": True,
                },
            )
        run.assert_not_called()
        self.assertTrue(result["isError"])
        self.assertIn("没有发任何东西", self.text_of(result))

    def test_确认的是A却发B_被指纹挡住(self):
        prep = self.call(
            "douyin_publish_video",
            {"account": "main", "file": "videos/a.mp4", "title": "方案A"},
        )
        plan_id = self.plan_id_of(prep)
        with mock.patch.object(sau, "run") as run:
            result = self.call(
                "douyin_publish_video",
                {
                    "account": "main",
                    "file": "videos/a.mp4",
                    "title": "方案B",
                    "plan_id": plan_id,
                    "confirm": True,
                },
            )
        run.assert_not_called()
        self.assertTrue(result["isError"])

    def test_素材越界在预检阶段就被拦下(self):
        outside = self.root.parent / "outside.mp4"
        outside.write_bytes(b"x")
        try:
            with mock.patch.object(sau, "run") as run:
                result = self.call(
                    "douyin_publish_video",
                    {"account": "main", "file": str(outside), "title": "标题"},
                )
            run.assert_not_called()
            self.assertTrue(result["isError"])
            self.assertIn("素材目录", self.text_of(result))
        finally:
            outside.unlink()

    def test_图文_预检与确认(self):
        prep = self.call(
            "douyin_publish_note",
            {"account": "main", "images": ["imgs/1.png", "imgs/2.png"], "title": "图文标题"},
        )
        plan_id = self.plan_id_of(prep)
        self.assertIn("imgs", self.text_of(prep).replace("\\\\", "/"))
        fake = sau.SauResult(argv=["sau"], exit_code=0, stdout="ok", stderr="")
        with mock.patch.object(sau, "run", return_value=fake) as run:
            result = self.call(
                "douyin_publish_note",
                {
                    "account": "main",
                    "images": ["imgs/1.png", "imgs/2.png"],
                    "title": "图文标题",
                    "plan_id": plan_id,
                    "confirm": True,
                },
            )
        argv = run.call_args.args[1]
        self.assertEqual(argv[:2], ["douyin", "upload-note"])
        self.assertFalse(result["isError"])

    def test_图文超过35张被拦下(self):
        with mock.patch.object(sau, "run") as run:
            result = self.call(
                "douyin_publish_note",
                {
                    "account": "main",
                    "images": [f"imgs/{i}.png" for i in range(36)],
                    "title": "图文标题",
                },
            )
        run.assert_not_called()
        self.assertTrue(result["isError"])
        self.assertIn("35", self.text_of(result))

    def test_定时参数进计划且被规范化(self):
        prep = self.call(
            "douyin_publish_video",
            {
                "account": "main",
                "file": "videos/a.mp4",
                "title": "标题",
                "schedule": "2026-03-24T21:30:00",
            },
        )
        text = self.text_of(prep)
        self.assertIn("2026-03-24 21:30", text)
        self.assertIn("schedule", text)


class TestPublishParamParity(ServerCase):
    """★ 补齐的那些参数要真的走到 CLI，且**进计划指纹**（否则"确认 A 发 B"的洞又开了）"""

    def test_封面走素材白名单_越界被拦(self):
        outside = self.root.parent / "cover.png"
        outside.write_bytes(b"c")
        try:
            with mock.patch.object(sau, "run") as run:
                result = self.call(
                    "douyin_publish_video",
                    {
                        "account": "main",
                        "file": "videos/a.mp4",
                        "title": "标题",
                        "thumbnail_portrait": str(outside),
                    },
                )
            run.assert_not_called()
            self.assertTrue(result["isError"])
            self.assertIn("素材目录", self.text_of(result))
        finally:
            outside.unlink()

    def test_封面带货声明合集都进计划(self):
        (self.root / "cover.png").write_bytes(b"p" * 32)
        prep = self.call(
            "douyin_publish_video",
            {
                "account": "main",
                "file": "videos/a.mp4",
                "title": "标题",
                "thumbnail_portrait": "cover.png",
                "product_link": "https://haohuo/detail/1",
                "product_title": "面膜",
                "declaration": "虚构演绎，仅供娱乐",
                "collection": "我的合集",
            },
        )
        text = self.text_of(prep)
        for key in ("thumbnail_portrait", "product_link", "product_title", "declaration", "collection"):
            self.assertIn(key, text)
        plan_id = self.plan_id_of(prep)
        fake = sau.SauResult(argv=["sau"], exit_code=0, stdout="ok", stderr="")
        with mock.patch.object(sau, "run", return_value=fake) as run:
            result = self.call(
                "douyin_publish_video",
                {
                    "account": "main",
                    "file": "videos/a.mp4",
                    "title": "标题",
                    "thumbnail_portrait": "cover.png",
                    "product_link": "https://haohuo/detail/1",
                    "product_title": "面膜",
                    "declaration": "虚构演绎，仅供娱乐",
                    "collection": "我的合集",
                    "plan_id": plan_id,
                    "confirm": True,
                },
            )
        self.assertEqual(run.call_count, 1)
        argv = run.call_args.args[1]
        for flag in ("--thumbnail-portrait", "--product-link", "--declaration", "--collection"):
            self.assertIn(flag, argv, argv)
        self.assertFalse(result["isError"])

    def test_改了声明_旧plan_id失效(self):
        prep = self.call(
            "douyin_publish_video",
            {"account": "main", "file": "videos/a.mp4", "title": "标题", "declaration": "甲"},
        )
        plan_id = self.plan_id_of(prep)
        with mock.patch.object(sau, "run") as run:
            result = self.call(
                "douyin_publish_video",
                {
                    "account": "main",
                    "file": "videos/a.mp4",
                    "title": "标题",
                    "declaration": "乙",
                    "plan_id": plan_id,
                    "confirm": True,
                },
            )
        run.assert_not_called()
        self.assertTrue(result["isError"])

    def test_图文_notef_与_bgm_进计划与命令行(self):
        (self.root / "body.md").write_bytes("正文".encode("utf-8"))
        prep = self.call(
            "douyin_publish_note",
            {
                "account": "main",
                "images": ["imgs/1.png"],
                "title": "标题",
                "note_file": "body.md",
                "bgm": "轻快 纯音乐",
            },
        )
        text = self.text_of(prep)
        self.assertIn("note_file", text)
        self.assertIn("bgm", text)
        plan_id = self.plan_id_of(prep)
        fake = sau.SauResult(argv=["sau"], exit_code=0, stdout="ok", stderr="")
        with mock.patch.object(sau, "run", return_value=fake) as run:
            self.call(
                "douyin_publish_note",
                {
                    "account": "main",
                    "images": ["imgs/1.png"],
                    "title": "标题",
                    "note_file": "body.md",
                    "bgm": "轻快 纯音乐",
                    "plan_id": plan_id,
                    "confirm": True,
                },
            )
        argv = run.call_args.args[1]
        self.assertIn("--notef", argv)
        self.assertIn("--bgm", argv)

    def test_正文两种写法同给_预检就拦下(self):
        (self.root / "body.md").write_bytes("正文".encode("utf-8"))
        with mock.patch.object(sau, "run") as run:
            result = self.call(
                "douyin_publish_note",
                {
                    "account": "main",
                    "images": ["imgs/1.png"],
                    "title": "标题",
                    "note": "文本",
                    "note_file": "body.md",
                },
            )
        run.assert_not_called()
        self.assertTrue(result["isError"])
        self.assertIn("只能给一种", self.text_of(result))


class TestAccountTools(ServerCase):
    """账号三件套：状态（退出码判据）、登录（会话式、不阻塞到底）、重置（两步删除）"""

    def test_状态用退出码判定(self):
        with mock.patch.object(
            sau, "run", return_value=sau.SauResult(argv=["sau"], exit_code=0, stdout="valid", stderr="")
        ):
            result = self.call("douyin_account_status", {"account": "main"})
        self.assertIn("logged_in=True", self.text_of(result))
        self.assertFalse(result["isError"])

        with mock.patch.object(
            sau, "run", return_value=sau.SauResult(argv=["sau"], exit_code=1, stdout="invalid", stderr="")
        ):
            result = self.call("douyin_account_status", {"account": "main"})
        text = self.text_of(result)
        self.assertIn("logged_in=False", text)
        self.assertIn("douyin_account_login", text)
        # ★「未登录」是**正常结论**，不该标成工具错误（否则模型会去重试而不是去登录）
        self.assertFalse(result["isError"])

    def test_登录_等不到扫码时回报会话仍在等(self):
        with mock.patch.object(sau, "spawn", return_value=FakeProc(exit_after=None)):
            result = self.call(
                "douyin_account_login", {"account": "main", "wait_seconds": 0}
            )
        text = self.text_of(result)
        self.assertIn("正在等扫码", text)
        self.assertIn("再调一次本工具", text)
        self.assertFalse(result["isError"])

    def test_登录_扫码成功后再检查一次(self):
        spawned = FakeProc(exit_after=0, lines=[b"Douyin login flow completed\n"])
        checked = sau.SauResult(argv=["sau"], exit_code=0, stdout="valid", stderr="")
        with mock.patch.object(sau, "spawn", return_value=spawned), mock.patch.object(
            sau, "run", return_value=checked
        ):
            result = self.call(
                "douyin_account_login", {"account": "main", "wait_seconds": 3}
            )
            # 会话结束后的自动 check 在守护线程里，给它一点时间
            self.server.sessions.wait(3)
        self.assertIn("登录成功", self.text_of(result))
        self.assertIs(True, self.server.sessions.snapshot()["loggedIn"])

    def test_登录_同刻只保留一个待扫码会话(self):
        first = FakeProc(exit_after=None)
        second = FakeProc(exit_after=None)
        with mock.patch.object(sau, "spawn", side_effect=[first, second]):
            self.call("douyin_account_login", {"account": "main", "wait_seconds": 0})
            self.call("douyin_account_login", {"account": "main2", "wait_seconds": 0})
        self.assertTrue(first.terminated, "开新会话必须把旧的关掉")
        self.assertEqual(self.server.sessions.snapshot()["account"], "main2")

    def test_重置登录态_两步(self):
        calls = []

        def fake_logout(cfg, account, confirm=False):
            calls.append(confirm)
            return sau.LogoutPlan(
                account=account, path=Path("x.json"), existed=True, deleted=confirm
            )

        with mock.patch.object(sau, "logout", side_effect=fake_logout):
            first = self.call("douyin_account_logout", {"account": "main"})
            second = self.call("douyin_account_logout", {"account": "main", "confirm": True})
        self.assertEqual(calls, [False, True], "第一次必须不带 confirm")
        self.assertIn("重新扫码", self.text_of(second))
        self.assertFalse(first["isError"])


class TestFingerprint(unittest.TestCase):
    def test_字段顺序不影响_内容变了才变(self):
        a = {"kind": "video", "account": "m", "title": "t", "tags": ["x"]}
        b = {"title": "t", "tags": ["x"], "account": "m", "kind": "video"}
        self.assertEqual(plan_fingerprint(a), plan_fingerprint(b))
        c = dict(a, title="t2")
        self.assertNotEqual(plan_fingerprint(a), plan_fingerprint(c))

    def test_长度12且是十六进制(self):
        fp = plan_fingerprint({"x": 1})
        self.assertEqual(len(fp), 12)
        int(fp, 16)


class TestServeLoop(ServerCase):
    def test_逐行读写_通知不回_未知行忽略(self):
        import io

        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "",                       # 空行
            "{ 不是 JSON",            # 坏行：忽略（不许崩）
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]
        out, err = io.StringIO(), io.StringIO()
        server = Server(self.cfg, out=out, err=err)
        server.serve(stdin=io.StringIO("\n".join(lines) + "\n"))

        responses = [json.loads(l) for l in out.getvalue().strip().splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2])   # 通知与坏行都不回
        self.assertIn("tools", responses[1]["result"])
        self.assertIn("非 JSON", err.getvalue())


class FakeWeb:
    """假的 urlopen 返回值（读取通道的用例全靠它，**不联网**）"""

    def __init__(self, payload, encoding: str = ""):
        self._payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Encoding": encoding}
        self._final = ""

    def read(self):
        return self._payload

    def geturl(self):
        return self._final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


AWEME = {
    "aweme_id": "7412345678901234567",
    "desc": "测试作品",
    "create_time": 1700000000,
    "author": {"nickname": "某人", "sec_uid": "MS4wLjABAAAAtest"},
    "video": {"play_addr": {"url_list": ["https://x/playwm/a.mp4"]}, "duration": 12000},
    "statistics": {"digg_count": 10, "comment_count": 2, "share_count": 1, "collect_count": 0},
}


class TestReadTools(unittest.TestCase):
    """读取工具：登录态来自 sau 的凭据文件，失败要说清"下一步做什么"。

    ★ 钉两件事：① 风控/没登录**不能**被说成"没搜到"（那会让用户白折腾关键词）；
              ② 输出里只有 cookie 名，没有 cookie 值。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "cookies").mkdir()
        self._write_credential()
        self.cfg = sau.SauConfig(cmd="sau", project_dir=str(self.root), media_dir=str(self.root))
        self.server = Server(self.cfg)
        import os

        env = {k: v for k, v in os.environ.items() if k not in ("DOUYIN_COOKIE", "DOUYIN_COOKIE_FILE")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_credential(self):
        (self.root / "cookies" / "douyin_main.json").write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "sessionid", "value": "SECRET", "domain": ".douyin.com"},
                        {"name": "ttwid", "value": "t", "domain": ".douyin.com"},
                    ]
                }
            ),
            encoding="utf-8",
        )

    def call(self, name, arguments):
        resp = self.server.handle(
            {"jsonrpc": "2.0", "id": "1", "method": "tools/call",
             "params": {"name": name, "arguments": arguments}}
        )
        return resp["result"]

    def text_of(self, result):
        return "\n".join(c.get("text", "") for c in result["content"])

    def _stub(self, payload):
        return mock.patch.object(douyin_web.urllib.request, "urlopen", lambda *a, **k: FakeWeb(payload))

    # ── 搜索 ────────────────────────────────────────────────
    def test_搜索成功时给出_id_作者与无水印直链(self):
        with self._stub({"data": [{"aweme_info": AWEME}]}):
            result = self.call("douyin_search_videos", {"keyword": "测试"})
        text = self.text_of(result)
        self.assertFalse(result["isError"])
        self.assertIn("7412345678901234567", text)
        self.assertIn("某人", text)
        self.assertIn("无水印", text)
        self.assertNotIn("playwm", text)

    def test_真没搜到不算错误(self):
        with self._stub({"data": []}):
            result = self.call("douyin_search_videos", {"keyword": "测试"})
        self.assertFalse(result["isError"])
        self.assertIn("0 条", self.text_of(result))

    def test_被风控时不能说成没搜到(self):
        with self._stub({"status_code": 0, "status_msg": "blocked"}):
            result = self.call("douyin_search_videos", {"keyword": "测试"})
        text = self.text_of(result)
        self.assertTrue(result["isError"])
        self.assertIn("blocked", text)

    def test_没有凭据时给出扫码指引且不泄漏值(self):
        (self.root / "cookies" / "douyin_main.json").unlink()
        result = self.call("douyin_search_videos", {"keyword": "测试"})
        text = self.text_of(result)
        self.assertTrue(result["isError"])
        self.assertIn("douyin_account_login", text)
        self.assertNotIn("SECRET", text)

    def test_描述里只出现_cookie_名(self):
        with self._stub({"data": []}):
            self.call("douyin_search_videos", {"keyword": "测试"})
        # 工具输出里不该出现 cookie 的值（凭据只用于发请求）
        self.assertNotIn("SECRET", self.text_of(self.call("douyin_account_status", {"account": "main"})))

    # ── 详情 / 分享 ─────────────────────────────────────────
    def test_详情工具能吃整条链接(self):
        with self._stub({"aweme_detail": AWEME}):
            result = self.call(
                "douyin_video_detail",
                {"aweme_id": "https://www.douyin.com/video/7412345678901234567"},
            )
        self.assertIn("7412345678901234567", self.text_of(result))

    def test_分享短链会跟随跳转再读详情(self):
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            if "v.douyin.com" in request.full_url:
                resp = FakeWeb(b"")
                resp._final = "https://www.douyin.com/video/7412345678901234567"
                return resp
            return FakeWeb({"aweme_detail": AWEME})

        with mock.patch.object(douyin_web.urllib.request, "urlopen", fake_urlopen):
            result = self.call(
                "douyin_parse_share_link",
                {"share_text": "7.32 复制打开抖音 https://v.douyin.com/abc/ 看看这个"},
            )
        self.assertIn("7412345678901234567", self.text_of(result))
        self.assertEqual(len(seen), 2, "第一次跟随短链、第二次读详情")

    def test_分享文本里没有链接时说明原因(self):
        result = self.call("douyin_parse_share_link", {"share_text": "今天天气不错"})
        self.assertTrue(result["isError"])
        self.assertIn("没有链接", self.text_of(result))

    # ── 用户 ────────────────────────────────────────────────
    def test_用户主页带作品列表(self):
        payload = {
            "user": {"nickname": "某人", "sec_uid": "MS4wLjABAAAAtest", "follower_count": 5},
            "aweme_list": [AWEME],
            "has_more": 0,
            "max_cursor": 0,
        }
        with self._stub(payload):
            result = self.call("douyin_user_profile", {"sec_user_id": "MS4wLjABAAAAtest"})
        text = self.text_of(result)
        self.assertIn("粉丝 5", text)
        self.assertIn("7412345678901234567", text)

    def test_我的主页会读当前登录账号(self):
        payload = {
            "user": {"nickname": "我自己", "sec_uid": "MS4wLjABAAAAtest"},
            "aweme_list": [],
            "has_more": 0,
            "max_cursor": 0,
        }
        with self._stub(payload):
            result = self.call("douyin_my_profile", {"include_videos": False})
        self.assertIn("我自己", self.text_of(result))


if __name__ == "__main__":
    unittest.main()
