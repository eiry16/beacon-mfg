#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BeaconMFG MCP —— 标准协议适配层（adapter / shim）。

【为什么存在】
  mcp/server.py 用**换行分隔 JSON-RPC**（`for raw in sys.stdin` + `json + "\n"`），
  这正是 **MCP stdio 规范**的分帧方式。但有一类宿主用 LSP 式的
  `Content-Length: N\\r\\n\\r\\n{...}` 分帧。本适配层让同一个 server 同时吃两种帧。

【2026-10-06 两处致命缺陷（实测代价：常见 MCP 客户端 里 249 轮探测全部超时，0 个工具）】

  ① **只认 Content-Length**。旧版断言「标准 MCP 客户端用 Content-Length 分帧」，
     于是收到换行的 `{...}\\n` 时不认为一条消息结束，一直阻塞等一个永远不来的空行；
     宿主 15s 判 `initialize timed out`，杀进程重试。实测两端分歧：
       printf '{...}\\n'  | python server.py       → 正常回包  ✅
       printf '{...}\\n'  | python mcp_adapter.py  → stdout 0 字节、静默退出 ❌
       Content-Length 分帧 | 两者都正常                       ✅

  ② **「一请求等一响应」的严格配对会死锁**。旧版每转发一个带 id 的请求，就阻塞等子进程
     回一条；而 server.py 里 `notifications/initialized` 是**不回包**的。宿主只要把这条
     通知带上 id 发出来（部分宿主确实这么干），adapter 就永久卡在等待上，
     之后所有请求（含 tools/list）都拿不到响应 → 宿主报 `tools/list timed out`。
     现象与 ① 极像（都是超时），但根因完全不同，只修 ① 会停在 tools/list 上原地打转。

【现在的设计：纯分帧桥】
  不再做请求/响应的配对，**双向直通**：
    主线程   客户端 → 子进程（自动识别帧，统一转成换行分隔）
    读线程   子进程 → 客户端（按客户端用的那一种帧回写，逐条即时转发）
  唯一的状态是「客户端请求的 protocolVersion」——server.py 回的是固定版本号，
  严格客户端会因版本不一致卡住握手，所以回包时按 id 认领并改写。
  这样无论宿主发通知带不带 id、发不发未知方法，都不可能死锁。

用法：
  python mcp_adapter.py
环境变量（可选）：BEACON_REPO / BEACON_MASK_PHONE / BEACON_SOURCE
诊断（可选）：BEACON_ADAPTER_TRACE=1 把收发实录写进 %TEMP%/beacon_adapter_trace.log
"""
import json
import os
import sys
import subprocess
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("BEACON_REPO") or os.path.dirname(_HERE)
SERVER = os.path.join(_HERE, "server.py")
MASK = os.environ.get("BEACON_MASK_PHONE", "1")

# 输入缓冲（字节级，跨次 read 保留）
_BUF = b""
# 客户端用的帧格式：None=未知，'lsp'=Content-Length，'lines'=换行分隔
_FRAMING = {"kind": None}
# initialize 的 id 与客户端请求的协议版本（回包时按 id 认领并改写）
_INIT = {"id": None, "proto": None}
# 诊断开关：默认**关**（排查「宿主不吭声」时设 BEACON_ADAPTER_TRACE=1 打开，
# 收发实录会写进 %TEMP%/beacon_adapter_trace.log）。2026-10-06 定位 tools/list 超时
# 就是靠它一眼看出「子进程吐到一半就死了」。
_TRACE_ON = os.environ.get("BEACON_ADAPTER_TRACE") == "1"


def _trace(msg: str) -> None:
    if not _TRACE_ON:
        return
    try:
        p = os.path.join(tempfile.gettempdir(), "beacon_adapter_trace.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write("%.3f %d %s\n" % (time.time(), os.getpid(), msg[:400]))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 读：客户端 → 我们（自动识别两种帧）
# --------------------------------------------------------------------------- #
def _read_some() -> bytes:
    """非阻塞语义地取一块可用字节（单次 os.read，有多少拿多少）。EOF 返回 b''。"""
    try:
        return sys.stdin.buffer.raw.read(65536)
    except Exception:
        return b""


def _read_line() -> bytes | None:
    """读到 \\n 为止，返回不含换行的 bytes；EOF 且无残留返回 None。"""
    global _BUF
    while b"\n" not in _BUF:
        chunk = _read_some()
        if not chunk:
            if _BUF:
                line, _BUF = _BUF, b""
                return line
            return None
        _BUF += chunk
    line, _, _BUF = _BUF.partition(b"\n")
    return line


def _read_exact(n: int) -> bytes | None:
    global _BUF
    while len(_BUF) < n:
        chunk = _read_some()
        if not chunk:
            return None
        _BUF += chunk
    body, _BUF = _BUF[:n], _BUF[n:]
    return body


def _read_msg() -> dict | None:
    """读一条客户端消息，自动识别 Content-Length 分帧 / 换行分隔。真 EOF → None。

    单条脏数据**不终止会话**（旧版把它当 EOF 直接退，宿主只会看到超时）。
    """
    while True:
        line = _read_line()
        while line is not None and not line.strip():
            line = _read_line()          # 跳过空行
        if line is None:
            _trace("RECV eof")
            return None
        head = line.strip()

        if head.lower().startswith(b"content-length:"):
            _FRAMING["kind"] = "lsp"
            clen = None
            try:
                clen = int(head.split(b":", 1)[1].strip())
            except ValueError:
                pass
            while True:                  # 吃掉剩余头直到空行
                h = _read_line()
                if h is None:
                    _trace("RECV eof(header)")
                    return None
                hs = h.strip()
                if not hs:
                    break
                if clen is None and hs.lower().startswith(b"content-length:"):
                    try:
                        clen = int(hs.split(b":", 1)[1].strip())
                    except ValueError:
                        pass
            if clen is None:
                _trace("DROP lsp 头里没有 Content-Length")
                continue
            body = _read_exact(clen)
            if body is None:
                _trace("RECV eof(body) 期待 %d 字节" % clen)
                return None
            try:
                obj = json.loads(body.decode("utf-8"))
            except Exception as e:
                _trace("DROP lsp body 解析失败 %s raw=%r" % (e, body[:200]))
                continue
            _trace("RECV lsp method=%s id=%s" % (obj.get("method"), obj.get("id")))
            return obj

        # MCP stdio 规范：一条消息 = 一行 JSON（不带 Content-Length）
        if _FRAMING["kind"] is None:
            _FRAMING["kind"] = "lines"
        try:
            obj = json.loads(head.decode("utf-8"))
        except Exception as e:
            _trace("DROP lines 解析失败 %s raw=%r" % (e, head[:200]))
            continue
        _trace("RECV lines method=%s id=%s" % (obj.get("method"), obj.get("id")))
        return obj


# --------------------------------------------------------------------------- #
# 写：我们 → 客户端（按客户端进来的那一种帧回写）
# --------------------------------------------------------------------------- #
def _write_msg(obj: dict) -> None:
    """两种帧**不能混**，且 LSP 帧的 body 一个字节都不能少。

    只写 `Content-Length` 头、漏掉 body，客户端会一直等那 N 个字节，
    表现同样是「timed out」——极难从现成日志里看出来，务必别手滑。
    """
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if _FRAMING["kind"] == "lsp":
        sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    else:
        sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()
    _trace("SEND framing=%s id=%s bytes=%d" % (_FRAMING["kind"], obj.get("id"), len(data)))


def _spawn_server():
    return subprocess.Popen(
        [sys.executable, "-u", SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # ⚠ 必须显式注入 UTF-8（npm 启动器 bin/beacon-mfg-mcp.js 就是这么干的，本适配层
        #   曾漏掉）：中文 Windows 上 python 默认按 cp936 写 stdout，而 tool 描述里含
        #   `⚠`(U+26A0) 这类 GBK 编不出的字符 —— initialize 能过，`tools/list` 必崩，
        #   宿主只看到「tools/list timed out」。2026-10-06 实测：常见 MCP 客户端 249 轮探测全废。
        env={**os.environ, "BEACON_REPO": REPO, "BEACON_MASK_PHONE": MASK,
             "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        bufsize=0, text=True, encoding="utf-8",
    )


def main():
    child = _spawn_server()

    def pump_down():
        """子进程 → 客户端。逐条即时转发，不做任何配对判断。"""
        try:
            for line in child.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception as e:
                    _trace("DROP child 输出解析失败 %s raw=%r" % (e, line[:200]))
                    continue
                # 按 id 认领 initialize 回包，改写回客户端请求的协议版本
                if (_INIT["proto"] and obj.get("id") is not None
                        and obj.get("id") == _INIT["id"]
                        and isinstance(obj.get("result"), dict)):
                    obj["result"]["protocolVersion"] = _INIT["proto"]
                _write_msg(obj)
        except Exception as e:
            _trace("child 读线程异常 %s" % e)
        finally:
            _trace("child stdout 关闭")

    pump = threading.Thread(target=pump_down, daemon=True)
    pump.start()

    try:
        while True:
            msg = _read_msg()
            if msg is None:
                break
            if msg.get("method") == "initialize":
                _INIT["id"] = msg.get("id")
                _INIT["proto"] = (msg.get("params") or {}).get("protocolVersion")
            child.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            child.stdin.flush()
    except Exception as e:
        _trace("主循环异常 %s" % e)
    finally:
        # ⚠ 别急着 terminate：客户端 stdin 一关就可能还有回包在子进程/管道里，
        #   直接杀进程会把最后几条响应丢掉（宿主侧表现还是「超时」，白折腾）。
        #   正确顺序：关子进程 stdin（它读完就自然退）→ 等读线程吐完 → 再收尾。
        _trace("客户端 stdin 结束，等待子进程吐完剩余回包")
        try:
            child.stdin.close()
        except Exception:
            pass
        pump.join(timeout=60)
        _trace("读线程结束，收尾")
        try:
            if child.poll() is None:
                child.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
