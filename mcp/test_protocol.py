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
    "start_sourcing", "answer_sourcing", "refine_sourcing",
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
    _check("%s tools/list (6)" % label, not missing, ("缺: " + ",".join(missing)) if missing else "")

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


def main():
    print("=" * 60)
    print("BeaconMFG MCP 协议回归测试")
    print("数据源: BEACON_REPO=%s" % (os.environ.get("BEACON_REPO") or "(默认/联网)"))
    print("=" * 60)

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
