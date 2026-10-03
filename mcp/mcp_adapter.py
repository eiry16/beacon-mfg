#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BeaconMFG MCP —— 标准协议适配层（adapter / shim）。

为什么需要它：
  beacon-mfg 的 mcp/server.py 用「换行分隔 JSON-RPC 子集」做传输
  （`for raw in sys.stdin` + `json + "\n"`），并不是标准 MCP 的
  `Content-Length` 分帧。标准 MCP 客户端（WorkBuddy 连接器、Claude Desktop
  等）按 LSP 分帧跟它握手会解析失败。

本适配层做什么：
  - 对外（父进程 = MCP host）：讲标准 MCP stdio（Content-Length 分帧）。
  - 对内（子进程 = server.py）：把请求转成换行分隔 JSON 转发，并把 server.py
    的换行回包重新分帧后返回。
  - 不修改 server.py 任何代码；server.py 的「通知无回包」语义天然保留
    （转发通知后不等待回包）。

用法：
  python mcp_adapter.py
环境变量（可选）：BEACON_REPO / BEACON_MASK_PHONE / BEACON_SOURCE
"""
import json
import os
import sys
import subprocess
import threading
import queue

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("BEACON_REPO") or os.path.dirname(_HERE)
SERVER = os.path.join(_HERE, "server.py")
MASK = os.environ.get("BEACON_MASK_PHONE", "1")


def _spawn_server():
    return subprocess.Popen(
        [sys.executable, "-u", SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={**os.environ, "BEACON_REPO": REPO, "BEACON_MASK_PHONE": MASK},
        bufsize=0, text=True, encoding="utf-8",
    )


def _read_stdin_framed():
    """从父进程(stdio)读取一条标准 MCP 分帧消息，返回 dict；EOF 返回 None。"""
    buf = b""
    while True:
        c = sys.stdin.buffer.read(1)
        if not c:
            return None
        buf += c
        if buf.endswith(b"\r\n\r\n") or buf.endswith(b"\n\n"):
            break
    clen = None
    for line in buf.split(b"\n"):
        low = line.strip().lower()
        if low.startswith(b"content-length:"):
            try:
                clen = int(low.split(b":", 1)[1].strip())
            except ValueError:
                pass
    if clen is None:
        return None
    body = b""
    while len(body) < clen:
        chunk = sys.stdin.buffer.read(clen - len(body))
        if not chunk:
            return None
        body += chunk
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def _write_stdout_framed(obj):
    """向父进程(stdio)写一条标准 MCP 分帧消息。"""
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def main():
    child = _spawn_server()
    responses: "queue.Queue" = queue.Queue()
    client_protocol = None  # 客户端在 initialize 里请求的协议版本

    def reader():
        # 子进程用换行分隔 JSON；逐行读、入队。
        try:
            for line in child.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                responses.put(obj)
        except Exception:
            pass

    threading.Thread(target=reader, daemon=True).start()

    try:
        while True:
            msg = _read_stdin_framed()
            if msg is None:
                break
            method = msg.get("method")
            mid = msg.get("id")
            is_notification = (mid is None) or (isinstance(method, str) and method.startswith("notifications/"))

            if method == "initialize":
                # 记下客户端请求版本，用于回包时回写（兼容严格客户端）
                client_protocol = (msg.get("params") or {}).get("protocolVersion")

            # 转发给子进程（换行分隔）
            child.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            child.stdin.flush()

            if is_notification:
                continue  # 通知无需回包，server.py 自身也是 continue

            # 请求：等子进程回包（按到达顺序，server.py 严格顺序响应）
            try:
                resp = responses.get(timeout=60)
            except queue.Empty:
                if mid is not None:
                    _write_stdout_framed({
                        "jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32000, "message": "no response from server.py"},
                    })
                continue

            # 兼容严格客户端：server.py 回的是固定 protocolVersion，
            # 改写回客户端在 initialize 里请求的版本，避免握手因版本不一致卡住。
            if method == "initialize" and client_protocol and isinstance(resp.get("result"), dict):
                resp["result"]["protocolVersion"] = client_protocol

            _write_stdout_framed(resp)
    finally:
        try:
            child.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
