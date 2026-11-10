#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BeaconMFG MCP —— 发布前协议回归测试（pre-release smoke test）。

为什么需要它
------------
`server.py` 同时服务两条链路：本地 stdio（npm / git 安装）与远程 Streamable HTTP
（`http_server.py`）。业务逻辑一改，两条链路都可能悄悄坏掉，且**症状隐蔽**
（能握手但查不出数据、或只有部分工具挂掉）。本脚本把「协议能握手 + 工具齐全 +
真实检索有结果」三件事固化为一条命令，每次发版前跑一遍。

用法
----
    python mcp/test_protocol.py                 # 自动起 HTTP 端点自测
    python mcp/test_protocol.py --stdio         # 额外测 stdio(server.py)
    python mcp/test_protocol.py --adapter       # 额外测标准分帧适配层
    BEACON_REPO=/path/to/repo python mcp/test_protocol.py

退出码：0 全部通过；1 有失败项。
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")
HTTP_SERVER = os.path.join(HERE, "http_server.py")
ADAPTER = os.path.join(HERE, "mcp_adapter.py")

EXPECT_TOOLS = {
    "search_vendors", "get_vendor", "get_capability_card",
    "suggest_filters", "list_industries",
    "start_sourcing", "answer_sourcing", "refine_sourcing",
    # 1.5.3 之后陆续加的两个，之前一直没进这个清单 —— 清单漏项等于
    # 「工具在某条链路上没注册」这类故障测不出来。
    "account", "vendor_onboarding",
    # 2026-11-07 加的踩坑留言区读写对：漏项等于「新工具在某条链路没注册」测不出
    "read_notes", "report_note",
}

# 三个城市各取一个高频词，覆盖不同行业与不同分片，避免「只测通一个分片」
SMOKE_QUERIES = [("修车", "嘉兴"), ("面馆", "苏州"), ("米粉", "贵阳")]

_results = []


def _check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name, ("  -> " + detail) if detail else ""))
    return ok


def _child_env():
    env = dict(os.environ)
    env.setdefault("BEACON_MASK_PHONE", "1")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# --------------------------------------------------------------------------
# 公共断言：拿到工具列表 + 跑真实检索
# --------------------------------------------------------------------------
def _assert_tools_and_search(call, label):
    """call(method, params, session_id) -> result dict"""
    r = call("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "smoke", "version": "1"},
    })
    _check("%s initialize" % label, r is not None and "result" in r, "" if r else "无回包")

    # 通知无回包（规范允许），不要等
    try:
        call("notifications/initialized", {}, r.get("__sid__") if isinstance(r, dict) else None)
    except Exception:
        pass

    r2 = call("tools/list", {})
    tools = set()
    if r2 and "result" in r2:
        tools = {t.get("name") for t in r2["result"].get("tools", [])}
    missing = EXPECT_TOOLS - tools
    _check("%s tools/list (%d)" % (label, len(EXPECT_TOOLS)), not missing,
           ("缺: " + ",".join(missing)) if missing else "")

    for q, city in SMOKE_QUERIES:
        r3 = call("tools/call", {"name": "search_vendors",
                                 "arguments": {"query": q, "city": city, "limit": 3}})
        n, err = 0, ""
        if r3 and "result" in r3:
            try:
                data = json.loads(r3["result"]["content"][0]["text"])
                n = len(data.get("results", []))
            except Exception as e:
                err = "解析失败 %s" % e
        else:
            err = "无回包"
        _check("%s search %s/%s" % (label, city, q), n > 0, err or "命中 %d" % n)

    # 踩坑留言区读写往返：report → read 能读到刚写的那条（用独立 topic 防污染）
    rn = call("tools/call", {"name": "report_note", "arguments": {
        "issue": "protocol-regression 探针坑", "workaround": "无需绕行",
        "agent": "test_protocol", "topic": "zz_regression"}})
    rok = rn and "result" in rn and '"ok": true' in rn["result"]["content"][0]["text"].replace(" ", " ")
    _check("%s report_note 写入" % label, bool(rok), "report_note 回包异常")
    rd = call("tools/call", {"name": "read_notes", "arguments": {"topic": "zz_regression"}})
    dok = rd and "result" in rd and "protocol-regression" in rd["result"]["content"][0]["text"]
    _check("%s read_notes 读回" % label, bool(dok), "read_notes 未读到刚写入的条目")
    # 清理探针文件，不污染留言区。
    # ⚠ 路径必须覆盖 server._notes_dir() 的**全部**分支：原先只算 BEACON_REPO
    #   与 ~/.beacon-mfg 两种，漏了「在仓库内直接跑」这一最常见的分支
    #   （server.py 会把 REPO 探测成仓库根 → 写到 mcp/notes/），于是
    #   探针文件一直残留、而 `notes/` 又在 package.json 的 files 白名单里，
    #   下次发版就把测试垃圾带进用户装到的包。
    _cands = [os.path.join(HERE, "notes", "zz_regression.md")]
    if os.environ.get("BEACON_REPO"):
        _cands.append(os.path.join(os.environ["BEACON_REPO"],
                                   "mcp", "notes", "zz_regression.md"))
    _cands.append(os.path.join(os.path.expanduser("~"), ".beacon-mfg",
                               "notes", "zz_regression.md"))
    for _p in _cands:
        try:
            if os.path.isfile(_p):
                os.remove(_p)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 1) Streamable HTTP（http_server.py）
# --------------------------------------------------------------------------
def test_http():
    print("\n[1/3] Streamable HTTP  (mcp/http_server.py)")
    port = int(os.environ.get("MCP_TEST_PORT", "8899"))
    env = _child_env()
    env["MCP_PORT"] = str(port)
    proc = subprocess.Popen([sys.executable, "-u", HTTP_SERVER],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d/mcp" % port
    try:
        # 等端口起来
        for _ in range(40):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1).read()
                break
            except Exception:
                time.sleep(0.25)
        else:
            _check("HTTP 端点启动", False, "40 次探测仍未就绪")
            return

        def call(method, params, sid=None):
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            if method.startswith("notifications/"):
                payload.pop("id")
            data = json.dumps(payload).encode()
            req = urllib.request.Request(base, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json, text/event-stream")
            if sid:
                req.add_header("Mcp-Session-Id", sid)
            with urllib.request.urlopen(req, timeout=90) as resp:
                new_sid = resp.headers.get("Mcp-Session-Id")
                body = resp.read().decode()
            if method.startswith("notifications/"):
                return None
            obj = json.loads(body)
            if new_sid:
                obj["__sid__"] = new_sid
            return obj

        _assert_tools_and_search(call, "http")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


# --------------------------------------------------------------------------
# 2) 换行分隔 stdio（server.py）—— npm/git 安装走的默认链路
# --------------------------------------------------------------------------
def test_stdio():
    print("\n[2/3] stdio 换行分隔  (mcp/server.py)")
    proc = subprocess.Popen([sys.executable, "-u", SERVER], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, env=_child_env(),
                            encoding="utf-8", bufsize=1)
    try:
        def send(method, params, notify=False):
            msg = {"jsonrpc": "2.0", "method": method, "params": params}
            if not notify:
                msg["id"] = 1
            proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            if notify:
                return None
            return json.loads(proc.stdout.readline())

        def call(method, params, sid=None):
            if method.startswith("notifications/"):
                return send(method, params, notify=True)
            return send(method, params)

        _assert_tools_and_search(call, "stdio")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


# --------------------------------------------------------------------------
# 3) 标准分帧适配层（mcp_adapter.py）—— 严格客户端走这条
# --------------------------------------------------------------------------
def test_adapter():
    print("\n[3/3] 标准分帧适配层  (mcp/mcp_adapter.py)")
    proc = subprocess.Popen([sys.executable, "-u", ADAPTER], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, env=_child_env(), bufsize=0)

    def send(obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
        proc.stdin.flush()

    def recv():
        hdr = b""
        while not hdr.endswith(b"\r\n\r\n"):
            c = proc.stdout.read(1)
            if not c:
                return None
            hdr += c
        n = 0
        for line in hdr.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                n = int(line.split(b":")[1].strip())
        body = b""
        while len(body) < n:
            chunk = proc.stdout.read(n - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body.decode("utf-8"))

    def call(method, params, sid=None):
        msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        if method.startswith("notifications/"):
            msg.pop("id")
            send(msg)
            return None
        send(msg)
        return recv()

    try:
        _assert_tools_and_search(call, "adapter")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_dead_proxy():
    """死代理容错：环境里配了**不可用**代理时，仍应直连成功（回归 #100086）。

    背景：客户机器上若配了已失效的代理（VPN/Clash 退出、端口已关），
    urllib 会把所有请求喂给死代理 → 检索整体归零，且旧报错「检查网络」不指向真因。
    修法见 server.py 的 `_openers()`：直连优先、代理兜底。
    """
    print("\n[*] 死代理容错  (直连优先)")
    env = _child_env()
    env.pop("BEACON_REPO", None)          # 强制走联网，才能真正触发网络取数
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env[k] = "http://127.0.0.1:9"     # 9 = discard 端口，几乎必然拒绝
    env["BEACON_MIRRORS"] = ""             # 别被本地镜像配置干扰
    proc = subprocess.Popen([sys.executable, "-u", SERVER], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, env=env, encoding="utf-8", bufsize=1)
    try:
        def send(method, params, notify=False):
            msg = {"jsonrpc": "2.0", "method": method, "params": params}
            if not notify:
                msg["id"] = 1
            else:
                msg.pop("id", None)
            proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            if notify:
                return None
            return json.loads(proc.stdout.readline())

        send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                            "clientInfo": {"name": "smoke", "version": "1"}})
        send("notifications/initialized", {}, notify=True)
        r = send("tools/call", {"name": "search_vendors",
                                "arguments": {"query": "面馆", "city": "苏州", "limit": 3}})
        n, err = 0, ""
        if r and "result" in r:
            try:
                n = len(json.loads(r["result"]["content"][0]["text"]).get("results", []))
            except Exception as e:
                err = "解析失败 %s" % e
        else:
            err = "无回包（很可能被死代理拖累）"
        _check("dead-proxy 苏州/面馆", n > 0, err or "命中 %d" % n)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_vid_args():
    """id 类入参的转换名单必须覆盖「标量 + 列表」两种形态（离线，不走网）。

    同一个坑已经踩了两次，两次的症状完全一样 —— **能调通、格式正确、就是没数据**：

      ① schema 里参数叫 `id`，名单里只有 `vid` → 点开某家报「找不到 id 对应的国标码」；
      ② 1.11.9 新增 `ids=[...]` 批量，名单没跟着加，`_vid_in` 里只判 `isinstance(v, str)`
         → 列表整体透传，token 直接进了 `get_vendor`，而本地模式的分片 / idmap /
         name-index 全是内部 id 建的键，`ids` 里每一条都查不到。
         更迷惑的是 `failed` 里写着「可能该记录尚未发布」，看着像数据问题。

    所以这里钉两条：名单本身完整、列表元素真的被逐个解码。
    远端模式（数据已是 token）不解码，也不该被这条测试带偏。
    """
    print("\n[*] id 入参转换  (标量 + 列表 · 离线)")
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import vid_token
        import server
    except Exception as e:                     # 缺依赖不该让整轮测试变红
        _check("server 可导入", False, "%s: %s" % (type(e).__name__, e))
        return

    secret = "regression-secret-0123456789abcdef"
    vid = "CN-MFG-0000009"
    keep = (server._vid_has_key, server._vid_secret, server.REPO)
    server._vid_has_key = lambda: True
    server._vid_secret = lambda: secret
    server.REPO = HERE                         # 非空 = 本地模式（token 需解码）
    try:
        tok = vid_token.encode(vid, secret)
        _check("token 可往返", vid_token.decode(tok, secret) == vid, tok)

        for k in ("id", "vid", "vendor_id", "supplier_id", "company_id", "card_id"):
            got = server._vid_in({k: tok}).get(k)
            _check("标量 %s" % k, got == vid, "得到 %r" % (got,))

        # 1.11.10 修的就是这条：之前 ids 原样透传，列表里全是 token
        _check("ids 在列表名单里", "ids" in server._VID_IN_LIST_ARGS,
               "名单=%r" % (server._VID_IN_LIST_ARGS,))
        got = server._vid_in({"ids": [tok, vid, ""]})["ids"]
        _check("列表 ids 逐元素解码", got == [vid, vid, ""], "得到 %r" % (got,))
        got = server._vid_in({"ids": (tok,)}).get("ids")
        _check("列表 ids 接受元组", got == [vid], "得到 %r" % (got,))

        # 远端模式：数据侧已是 token，入参必须原样传，解码了反而查不到
        server.REPO = ""
        got = server._vid_in({"ids": [tok]})["ids"]
        _check("远端模式不解码", got == [tok], "得到 %r" % (got,))
    finally:
        server._vid_has_key, server._vid_secret, server.REPO = keep


def test_alias_keep():
    """别名召回只豁免「被别名条目解释掉」的关键词（离线）。

    这条规则一改就出过两个反向事故，都是因为豁免范围判错：
      · 判得太松（整段豁免）→ 限定词静默失效：「五星级宾馆」= 全城宾馆 502 条；
      · 判得太严（只认 `==`）→ 别名扩展写法不豁免：「钣金」把「钣金加工」类目里
        没写「钣金」的记录全滤掉，454 → 146。
    所以这里把两种边界都钉住。
    """
    print("\n[*] 别名豁免范围  (离线)")
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import server
    except Exception as e:
        _check("server 可导入", False, "%s: %s" % (type(e).__name__, e))
        return
    cases = [
        # (关键词, 别名条目词, 期望仍需 AND 校验的关键词)
        (["钣金"], "钣金加工", [], "别名扩展写法要豁免，否则丢召回"),
        (["钣金"], "钣金", [], "完全同词"),
        (["医院"], "宠物医院", [], "后缀扩展同理"),
        (["大", "酒店"], "酒店", ["大"], "残留限定词必须生效"),
        (["五星级", "宾馆"], "宾馆", ["五星级"], "属性限定词必须生效"),
        (["宾馆"], "", ["宾馆"], "没有别名条目时不做任何豁免"),
    ]
    for toks, word, want, why in cases:
        got = server._alias_keep_tokens(toks, word)
        _check("豁免 %s / %s" % ("+".join(toks), word or "(无)"), got == want,
               "%s → 得到 %r，期望 %r（%s）" % (why, got, want, why) if got != want
               else "")


def main():
    print("=" * 60)
    print("BeaconMFG MCP 协议回归测试")
    print("数据源: BEACON_REPO=%s" % (os.environ.get("BEACON_REPO") or "(默认/联网)"))
    print("=" * 60)

    test_vid_args()
    test_alias_keep()
    test_http()
    if "--stdio" in sys.argv:
        test_stdio()
    if "--adapter" in sys.argv:
        test_adapter()
    else:
        print("\n[2/3] stdio  (跳过，加 --stdio 启用)")
        print("[3/3] adapter (跳过，加 --adapter 启用)")
    test_dead_proxy()

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = [(n, d) for n, ok, d in _results if not ok]
    print("\n" + "=" * 60)
    print("结果: %d 通过, %d 失败" % (passed, len(failed)))
    for n, d in failed:
        print("  FAIL %s  %s" % (n, d))
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
