#!/usr/bin/env node
'use strict';
// beacon-mfg-mcp 自更新：**薄壳**，只负责找到 Python 并把 selfupdate.py 拉起来。
//
// 为什么改成薄壳：自更新逻辑的**唯一事实来源**是 mcp/selfupdate.py。
// 原先这里有一份完整的 JS 实现，而 server.py 直连的用户根本走不到它 —— 于是
// 「只有 npm 启动器会检查更新」成了盲区，且两份实现容易漂移。
// 现在：server.py 启动时会自行拉起 selfupdate.py（任何接入方式都覆盖），
// 本文件仅为兼容旧调用路径（launcher / 手动执行）保留。
//
// 用法：node bin/selfupdate.js [--check|--print-mode|--force]

const { spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const script = path.join(__dirname, '..', 'selfupdate.py');
if (!fs.existsSync(script)) {
  // 没有 Python 侧实现就静默退出：自更新失败绝不影响 MCP 服务
  process.exit(0);
}

const candidates = ['python3', 'python', 'py'];
let chosen = null;
for (const c of candidates) {
  const r = spawnSync(c, ['--version'], { stdio: 'ignore' });
  if (r.status === 0) { chosen = c; break; }
}
if (!chosen) process.exit(0);

const child = spawnSync(chosen, [script, ...process.argv.slice(2)], { stdio: 'inherit' });
process.exit(child.status === null ? 0 : child.status);
