#!/usr/bin/env python3
"""发版一致性闸门（mcp/）

存在的理由
----------
发版前有两类错误，靠人记和靠文档提醒都失败过，只能靠机器拦：

1. **版本号三处不一致**：package.json / server.json / packages[0].version。
   2026-11-10 就因为 server.json 漏改，npm 已经发成功、Registry 才 400，
   白烧掉一个版本号。
2. **工具清单三处漂移**：server.py 的 TOOLS（权威）、mcp/README.md 的工具表
   （Glama/npm 页面读它）、test_protocol.py 的 EXPECT_TOOLS（回归基准）。
   实测已经漂移过一次：server.py 有 12 个工具，README 只写了 11 个。

用法
----
    python mcp/check_release.py                  # 工具清单 + 三处版本互相一致
    python mcp/check_release.py --tag mcp-v1.13.0   # 再加「与 tag 一致」（CI 用）
    python mcp/check_release.py --json           # 机器可读输出

也会被 `prepublishOnly` 自动调用：本地 `npm publish` 前先过闸门，
所以「绕过 CI 手发」这条路也被堵住（这是唯一能真正拦住人的一层）。

退出码：0 全部一致 / 1 有不一致（消息里直接指出该改哪个文件）
"""

import argparse
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _read(name):
    return io.open(os.path.join(HERE, name), encoding="utf-8").read()


def _fail(msg):
    print(msg, file=sys.stderr)


def tools_from_server():
    """权威来源：server.py 里 TOOLS 各元素的 "name"（8 空格缩进，非 inputSchema 属性）"""
    src = _read("server.py")
    try:
        seg = src[src.index("TOOLS = ["):]
    except ValueError:
        raise SystemExit("::error file=mcp/server.py::找不到 TOOLS 列表")
    seg = seg[: seg.index("\n]\n") + 3]
    names = re.findall(r'\n        "name":\s*"([A-Za-z_][A-Za-z0-9_]*)"', seg)
    if not names:
        raise SystemExit("::error file=mcp/server.py::TOOLS 解析出 0 个工具，解析规则失效了")
    return names


def tools_from_readme():
    """mcp/README.md「提供的 tool」小节里表格的首列：| `tool_name` | ...

    必须限定在小节内：README 别处还有环境变量表和字段表（BEACON_REPO、
    legal_name 等），全局扫会把它们误判成工具名。
    """
    src = _read("README.md")
    m = re.search(r"^##\s*提供的\s*tool.*$", src, re.M)
    if not m:
        raise SystemExit("::error file=mcp/README.md::找不到「提供的 tool」小节，无法校验工具表")
    body = src[m.end():]
    nxt = re.search(r"^##\s", body, re.M)
    if nxt:
        body = body[: nxt.start()]
    # 只取小节里的**第一张表**：同节后面还有字段说明表（legal_name/uscc 等），
    # 全局扫会把它们误判成工具名。
    rows, seen = [], False
    for ln in body.split("\n"):
        if ln.lstrip().startswith("|"):
            rows.append(ln)
            seen = True
        elif seen:
            break
    return re.findall(r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*\|", "\n".join(rows), re.M)


def tools_from_test():
    """test_protocol.py 的 EXPECT_TOOLS 集合。

    该文件被 `.gitignore` 的 `mcp/test_*.py` 排除，CI 的 checkout 里可能**不存在**。
    这时返回 None 表示「本维度无法校验」，由调用方降级为警告 ——
    否则闸门会在 CI 里崩掉，把「文件没入库」误报成「清单不一致」。
    """
    path = os.path.join(HERE, "test_protocol.py")
    if not os.path.isfile(path):
        return None
    src = io.open(path, encoding="utf-8").read()
    try:
        seg = src[src.index("EXPECT_TOOLS = {"):]
    except ValueError:
        raise SystemExit("::error file=mcp/test_protocol.py::找不到 EXPECT_TOOLS")
    seg = seg[: seg.index("}")]
    return re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', seg)


def check_tools():
    """返回 [(文件, 问题描述)]，空列表 = 一致"""
    server = tools_from_server()
    readme = tools_from_readme()
    test = tools_from_test()

    problems = []
    s_set, r_set = set(server), set(readme)
    t_set = set(test) if test else set()

    missing_doc = [t for t in server if t not in r_set]
    extra_doc = [t for t in readme if t not in s_set]
    missing_test = [t for t in server if t not in t_set] if test else []
    extra_test = [t for t in test if t not in s_set] if test else []

    if missing_doc:
        problems.append((
            "mcp/README.md",
            "README 工具表缺 %d 个（server.py 有、文档没写）：%s"
            % (len(missing_doc), ", ".join(sorted(missing_doc))),
        ))
    if extra_doc:
        problems.append((
            "mcp/README.md",
            "README 工具表多出 %d 个（已从 server.py 删除、文档还在）：%s"
            % (len(extra_doc), ", ".join(sorted(extra_doc))),
        ))
    if missing_test:
        problems.append((
            "mcp/test_protocol.py",
            "EXPECT_TOOLS 缺 %d 个（新工具没进回归基准，等于没测）：%s"
            % (len(missing_test), ", ".join(sorted(missing_test))),
        ))
    if extra_test:
        problems.append((
            "mcp/test_protocol.py",
            "EXPECT_TOOLS 多出 %d 个（已删除的工具还在基准里）：%s"
            % (len(extra_test), ", ".join(sorted(extra_test))),
        ))

    # README 标题里用「共 N 个」写死总数，与真实数量比对
    # （只说「8 个只读」这类子集表述不校验：那是分类计数，不是总数）
    head = _read("README.md")
    m = re.search(r"#+\s*提供的\s*tool[（(][^）)]*?共\s*(\d+)\s*个", head)
    if m:
        claimed = int(m.group(1))
        if claimed != len(server):
            problems.append((
                "mcp/README.md",
                "标题写「共 %d 个」tool，server.py 实际 %d 个" % (claimed, len(server)),
            ))

    # server.json 描述里若出现「N tools」也要对得上
    try:
        desc = json.loads(_read("server.json")).get("description", "")
        md = re.search(r"(\d+)\s*tools?\b", desc, re.I)
        if md and int(md.group(1)) != len(server):
            problems.append((
                "mcp/server.json",
                'description 写「%s tools」，实际 %d 个' % (md.group(1), len(server)),
            ))
    except (ValueError, KeyError):
        pass

    return problems, server, readme, test


def check_versions(tag=None):
    """版本一致性。

    有 tag：tag / package.json / server.json / packages[0] 四者同一值。
    无 tag（本地直接 `npm publish` 走的路径）：三者**互相**一致即可 —— 这样
    prepublishOnly 不需要知道即将打的 tag 也能拦住版本漂移。
    """
    problems = []
    want = None
    if tag:
        want = tag[len("mcp-v"):] if tag.startswith("mcp-v") else tag

    try:
        pkg = json.loads(_read("package.json"))
    except ValueError as e:
        return [("mcp/package.json", "解析失败：%s" % e)]
    try:
        srv = json.loads(_read("server.json"))
    except ValueError as e:
        return [("mcp/server.json", "解析失败：%s" % e)]

    pkgs = (srv.get("packages") or [{}])[0]
    pairs = [
        ("mcp/package.json", "version", pkg.get("version")),
        ("mcp/server.json", "version", srv.get("version")),
        ("mcp/server.json", "packages[0].version", pkgs.get("version")),
    ]
    if want:
        for path, field, val in pairs:
            if val != want:
                problems.append((path, "%s=%r，但 tag 是 %s" % (field, val, want)))
    else:
        vals = {val for _, _, val in pairs}
        if len(vals) > 1:
            problems.append((
                "mcp/server.json",
                "三处版本不一致（tag 模式外的手工发布）：package.json=%r / "
                "server.json=%r / packages[0]=%r"
                % (pkg.get("version"), srv.get("version"), pkgs.get("version")),
            ))

    if not pkg.get("mcpName"):
        problems.append((
            "mcp/package.json",
            "缺 mcpName（Registry 靠它证明 npm 包归属，缺了 publish 会 403）",
        ))
    if srv.get("name") != pkg.get("mcpName"):
        problems.append((
            "mcp/server.json",
            "name=%r 与 package.json 的 mcpName=%r 不一致"
            % (srv.get("name"), pkg.get("mcpName")),
        ))

    # 远程端点的版本回执（pages/_worker.js 里的 MCP_SERVER_VERSION）。
    # 只影响 initialize 告诉客户端的版本号，不影响任何行为 —— 但两边不一致会让
    # 「线上跑的是不是新版」变成猜谜，而这是排查时第一个要问的问题（2026-11-13 加）。
    # 文件不存在时**跳过不报**：npm 包内不含 pages/，从包目录跑 prepublishOnly 会走到这。
    wpath = os.path.join(HERE, "..", "pages", "_worker.js")
    try:
        with io.open(wpath, encoding="utf-8") as f:
            m = re.search(r'^const MCP_SERVER_VERSION = "([^"]+)"', f.read(), re.M)
    except OSError:
        m = None
    if m and m.group(1) != pkg.get("version"):
        problems.append((
            "pages/_worker.js",
            'MCP_SERVER_VERSION=%r 与 package.json version=%r 不一致'
            '（远程端点 /mcp 会对外报旧版本号）' % (m.group(1), pkg.get("version")),
        ))

    return problems


def main():
    ap = argparse.ArgumentParser(description="发版一致性闸门")
    ap.add_argument("--tag", help="即将发布的 tag，如 mcp-v1.13.0")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    problems, server, readme, test = check_tools()
    # 无 --tag 时也查版本：校验三处互相一致（拦「本地直接 npm publish」这条路）
    problems += check_versions(args.tag)

    if args.json:
        print(json.dumps({
            "ok": not problems,
            "tools_in_server": len(server),
            "problems": [{"file": f, "detail": d} for f, d in problems],
        }, ensure_ascii=False, indent=2))
    else:
        print("工具清单：server.py %d / README %d / test %s"
              % (len(server), len(set(readme)),
                 len(set(test)) if test else "不在仓库（跳过）"))
        if problems:
            for f, d in problems:
                # GitHub Actions 注解格式：CI 里会直接标在对应文件上
                print("::error file=%s::%s" % (f, d))
                _fail("  ✗ %s —— %s" % (f, d))
            _fail("\n闸门未通过：%d 处不一致（见上）" % len(problems))
            return 1
        print("✓ 全部一致" + ("（含版本 %s）" % args.tag if args.tag else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
