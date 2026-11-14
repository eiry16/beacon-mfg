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
    "account", "get_onboarding_guide",
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

    qchecked = False
    for q, city in SMOKE_QUERIES:
        r3 = call("tools/call", {"name": "search_vendors",
                                 "arguments": {"query": q, "city": city, "limit": 3}})
        n, err, data = 0, "", None
        if r3 and "result" in r3:
            try:
                data = json.loads(r3["result"]["content"][0]["text"])
                n = len(data.get("results", []))
            except Exception as e:
                err = "解析失败 %s" % e
        else:
            err = "无回包"
        _check("%s search %s/%s" % (label, city, q), n > 0, err or "命中 %d" % n)
        # 2026-11-14：本地 tools/call 回执统一挂 quota（缓存版）。只查一次，
        # 避免三次 SMOKE_QUERY 重复计数；三条链路（http/stdio/adapter）都过。
        if not qchecked and isinstance(data, dict):
            qchecked = True
            qr = data.get("quota") or {}
            _check("%s quota 回执（本地缓存版）" % label,
                   qr.get("channel") == "local" and qr.get("dimension") == "ops"
                   and qr.get("precision") == "tier_only"
                   and qr.get("as_of") == "local_cache"
                   and qr.get("used") is None and qr.get("remaining") is None,
                   "得到 %r" % (qr,))
    if not qchecked:
        _check("%s quota 回执（本地缓存版）" % label, False, "没有可解析的 tools/call 回执")

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


def test_quota_receipt():
    """本地 quota 回执（2026-11-14 缓存版）：零联网、不伪造用量、不覆盖已有字段。

    这条比「回执里有没有 quota」更重要：早期方案想在每个 tools/call 里同步打
    `/v1/identity/me`，但 identity.py 记录过「无 UA → Cloudflare 1010 且静默降级」
    的故障，而且本地缓存里根本没有权威 used。所以把三个 HTTP 出口换成炸弹，
    确保任何人日后「顺手改成实时查」都会在这里被拦下。
    """
    print("\n[*] 本地 quota 回执  (缓存版 · 离线)")
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import identity
        import server
    except Exception as e:
        _check("server 可导入", False, "%s: %s" % (type(e).__name__, e))
        return

    keep = (identity._post, identity._post_auth, identity.ensure_token)
    called = []

    def bomb(*a, **kw):
        called.append(a)
        raise AssertionError("quota 回执触发了联网")

    identity._post = bomb
    identity._post_auth = bomb
    identity.ensure_token = bomb
    try:
        q = server._quota_receipt()
    finally:
        (identity._post, identity._post_auth, identity.ensure_token) = keep

    _check("quota 回执零联网", not called, "触发 %d 次 HTTP 出口" % len(called))
    _check("quota 形状：本地渠道 / op 口径 / 档位精度",
           q.get("channel") == "local" and q.get("dimension") == "ops"
           and q.get("unit") == "次" and q.get("precision") == "tier_only"
           and q.get("as_of") == "local_cache" and q.get("window") == "rolling_24h",
           "得到 %r" % (q,))
    _check("不伪造 used/remaining",
           q.get("used") is None and q.get("remaining") is None,
           "缓存里没有权威用量，给了数字就是假的")
    _check("写明三本账口径", "三本账互不共享" in (q.get("channels_note") or ""))
    _check("写明观察模式", "BEACON_ENFORCE" in (q.get("hint") or ""))

    src = {"results": [1], "total_matched": 1}
    out = server._attach_quota_receipt("search_vendors", src)
    _check("普通工具挂 quota 且不污染原对象",
           "quota" in out and "quota" not in src,
           "浅拷贝失效会污染可能被缓存复用的 dict")
    _check("account 自带 quota 不被覆盖",
           server._attach_quota_receipt("account", {"quota": {"server": 1}})["quota"]
           == {"server": 1})
    _check("已有 quota 键的工具不被覆盖",
           server._attach_quota_receipt("x", {"quota": 7})["quota"] == 7)
    _check("非 dict 回执原样返回",
           server._attach_quota_receipt("x", [1, 2]) == [1, 2])


def test_card_whitelist():
    """能力卡白名单的形态必须「两代清单 × 两种入参」都能命中（离线，不走网）。

    这个判据已经错了两次，两次都是**静默错**：不报错、不发请求，直接答
    `has_card=false`，客户端 Agent 从此拿不到 L1 能力卡。

      ① 一律取数字后缀 → token 入参没有连字符，取出来还是 token，判成没卡；
      ② 只认 token 形态 → 内部 id 取后缀得 `0020317`，与 token 化的白名单
         永不相交，于是**任何有卡企业都答没卡**（2026-11-13 实测耐特斯）。

    正确行为是「候选键取交集」，外加反向一条：真没卡时必须短路、零请求。
    """
    print("\n[*] 能力卡白名单判据  (两代清单 × 两种入参 · 离线)")
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import vid_token
        import server
    except Exception as e:
        _check("server 可导入", False, "%s: %s" % (type(e).__name__, e))
        return

    secret = "regression-secret-0123456789abcdef"
    vid = "CN-MFG-0020317"
    tok = vid_token.encode(vid, secret)
    keep = (server._vid_has_key, server._vid_secret, server._CAP_CARD_IDS,
            server.load_manifest, server.fetch_text)
    server._vid_has_key = lambda: True
    server._vid_secret = lambda: secret
    called = []

    def fake_fetch(rel):
        called.append(rel)
        return '{"id": "%s"}' % vid

    server.fetch_text = fake_fetch
    try:
        for label, ids in (("token 清单", tok), ("数字后缀清单", vid.rsplit("-", 1)[-1])):
            server.load_manifest = (lambda ids=ids: {
                "metadata": {"cap_cards": {"count": 3, "ids": ids}}})
            for arg_label, arg in (("内部 id", vid), ("token", tok)):
                server._CAP_CARD_IDS = None
                called.clear()
                r = server.get_capability_card(arg)
                _check("%s + %s 入参" % (label, arg_label),
                       r.get("has_card") is True and bool(called),
                       "得到 %r，请求 %d 次" % (r.get("has_card"), len(called)))

        # 反向：不在白名单 → 立刻答没卡，且一次请求都不发（这是白名单存在的唯一理由）
        server.load_manifest = lambda: {"metadata": {"cap_cards": {"count": 3, "ids": tok}}}
        server._CAP_CARD_IDS = None
        called.clear()
        r = server.get_capability_card("CN-MFG-0999999")
        _check("不在白名单 → 短路零请求",
               r.get("has_card") is False and not called,
               "得到 %r，请求 %d 次" % (r.get("has_card"), len(called)))
    finally:
        (server._vid_has_key, server._vid_secret, server._CAP_CARD_IDS,
         server.load_manifest, server.fetch_text) = keep


def main():
    print("=" * 60)
    print("BeaconMFG MCP 协议回归测试")
    print("数据源: BEACON_REPO=%s" % (os.environ.get("BEACON_REPO") or "(默认/联网)"))
    print("=" * 60)

    test_vid_args()
    test_alias_keep()
    test_card_whitelist()
    test_quota_receipt()
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
