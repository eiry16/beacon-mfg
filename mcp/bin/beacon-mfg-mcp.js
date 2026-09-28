#!/usr/bin/env node
'use strict';
// beacon-mfg-mcp 启动器：本 MCP 服务是纯 Python 标准库实现，这里只负责
// 在用户机器上找到 Python 解释器并拉起 server.py（stdio 协议）。
const { spawnSync } = require('child_process');
const path = require('path');

const serverPy = path.join(__dirname, '..', 'server.py');

// 探测可用的 Python 解释器（优先 python3，兼容 python / py）
const candidates = ['python3', 'python', 'py'];
let chosen = null;
for (const c of candidates) {
  const r = spawnSync(c, ['--version'], { stdio: 'ignore' });
  if (r.status === 0) {
    chosen = c;
    break;
  }
}
if (!chosen) {
  process.stderr.write(
    '[beacon-mfg-mcp] 未找到 Python 3。请先安装 Python 3.8+ 并加入 PATH，再重试。\n'
  );
  process.exit(1);
}

const child = spawnSync(chosen, [serverPy, ...process.argv.slice(2)], {
  stdio: 'inherit',
  // 显式锁定 UTF-8：中文 Windows 上 python 默认按 GBK 写 stdout，
  // MCP 客户端按 UTF-8 解码会乱码。从启动器层面保证子进程用 UTF-8 输出。
  env: Object.assign({}, process.env, {
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
  }),
});
process.exit(child.status === null ? 1 : child.status);
