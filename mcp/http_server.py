#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""beacon-mfg MCP —— Streamable HTTP 传输适配层（远程端点的第一步）。

设计要点
--------
* **不改动 server.py 本体**：直接复用它的 `_dispatch` / `TOOLS`，业务逻辑一行不动，
  因此「本地 stdio」与「远程 HTTP」两条链路共用同一份检索实现，不会分叉。
* 协议：MCP **Streamable HTTP**（2025-03-26）。请求一律 `POST /mcp`，
  响应 `application/json`；`initialize` 时下发 `Mcp-Session-Id`。
* 本服务器**不提供** server→client 的 SSE 主动推送（只读检索不需要），
  故 `GET /mcp` 返回 405 —— 这是规范允许的（MAY NOT）。
* 仅用 Python 标准库，与 server.py 的零依赖原则一致。

用法
----
    MCP_PORT=8787 python http_server.py
    # 健康检查：GET /health
    # 客户端配置：{ "type": "http", "url": "https://<你的地址>/mcp" }
"""

import json
import os
import sys
import uuid
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 保证可直接 `python http_server.py` 运行（同级 import server）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server as S  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "beacon-mfg-readonly", "version": "1.3.0"}
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")

_sessions = set()
_lock = threading.Lock()


def _log(msg: str) -> None:
    sys.stderr.write("[beacon-mcp-http] " + msg + "\n")
    sys.stderr.flush()


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _handle_msg(msg):
    """处理单条 JSON-RPC 消息。

    返回 (响应对象 | None, 是否需要新建会话)。
    响应为 None 表示这是通知（notification），按规范回 202、无 body。
    """
    if not isinstance(msg, dict):
        return _error(None, -32600, "Invalid Request"), False

    method = msg.get("method") or ""
    mid = msg.get("id")

    if method == "initialize":
        return _result(mid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }), True

    if method.startswith("notifications/"):
        return None, False

    if method == "ping":
        return _result(mid, {}), False

    if method == "tools/list":
        return _result(mid, {"tools": S.TOOLS}), False

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        res = S._dispatch(name, params.get("arguments") or {})
        return _result(mid, {
            "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
            "isError": isinstance(res, dict) and "error" in res,
        }), False

    if mid is not None:
        return _result(mid, {}), False
    return None, False


class MCPHandler(BaseHTTPRequestHandler):
    server_version = "beacon-mcp-http/1.0"
    protocol_version = "HTTP/1.1"

    # ── 工具方法 ──────────────────────────────────────────────
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version, Authorization",
        )
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")

    def _send_json(self, code, obj, sid=None, body_empty=False):
        payload = b"" if body_empty else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        if not body_empty:
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
        else:
            self.send_header("Content-Length", "0")
        if sid:
            self.send_header("Mcp-Session-Id", sid)
        self._cors()
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, fmt, *args):  # 静音默认访问日志，统一走 stderr
        _log(fmt % args)

    # ── 路由 ─────────────────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/health"):
            self._send_json(200, {
                "ok": True,
                "server": SERVER_INFO,
                "protocolVersion": PROTOCOL_VERSION,
                "mcpPath": MCP_PATH,
                "source": getattr(S, "BEACON_SOURCE", ""),
                "tools": [t["name"] for t in S.TOOLS],
            })
            return
        if path == MCP_PATH.rstrip("/"):
            # 本服务不提供 server→client 的 SSE 主动推送（只读检索不需要）
            self._send_json(405, _error(None, -32000, "SSE stream not offered; use POST"))
            return
        self._send_json(404, _error(None, -32000, "not found"))

    def do_DELETE(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == MCP_PATH.rstrip("/"):
            sid = self.headers.get("Mcp-Session-Id")
            if sid:
                with _lock:
                    _sessions.discard(sid)
            self.send_response(204)
            self._cors()
            self.end_headers()
            return
        self._send_json(404, _error(None, -32000, "not found"))

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path != MCP_PATH.rstrip("/"):
            self._send_json(404, _error(None, -32000, "not found"))
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            msg = json.loads(raw)
        except Exception:
            self._send_json(400, _error(None, -32700, "Parse error"))
            return

        sid = self.headers.get("Mcp-Session-Id")

        # 批量请求：逐条处理，整体回数组
        if isinstance(msg, list):
            out = []
            for m in msg:
                resp, _ = _handle_msg(m)
                if resp is not None:
                    out.append(resp)
            self._send_json(200, out, sid=sid)
            return

        try:
            resp, need_new_session = _handle_msg(msg)
        except BaseException as e:  # noqa: BLE001
            self._send_json(500, _error(msg.get("id"), -32603, f"Internal error: {e}"))
            return

        if need_new_session:
            sid = uuid.uuid4().hex
            with _lock:
                _sessions.add(sid)

        if resp is None:
            self._send_json(202, None, sid=sid, body_empty=True)
            return
        self._send_json(200, resp, sid=sid)


def main() -> None:
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8787"))
    httpd = ThreadingHTTPServer((host, port), MCPHandler)
    _log(f"listening http://{host}:{port}{MCP_PATH}  source={getattr(S, 'BEACON_SOURCE', '')}")
    _log(f"tools: {[t['name'] for t in S.TOOLS]}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
