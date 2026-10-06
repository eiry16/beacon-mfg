#!/usr/bin/env node
'use strict';
// beacon-mfg-mcp 启动器：本 MCP 服务是纯 Python 标准库实现，这里只负责
// ① 在用户机器上找到 Python 解释器并拉起 server.py（stdio 协议）
// ② 顺手踢一个**独立进程**去做节流的自更新检查
const { spawnSync, spawn } = require('child_process');
const path = require('path');

const serverPy = path.join(__dirname, '..', 'server.py');
const selfUpdateJs = path.join(__dirname, 'selfupdate.js');

// 踢起自更新检查：detached + unref，主进程既不等它也不被它拖住。
// 为什么必须是独立进程而不是本进程里的 async 函数：
// 下面 spawnSync 是**同步阻塞**的，会冻结整个事件循环 —— async 的自更新
// 在 server 运行期间推不动，等它能跑时本进程已经 process.exit() 了，
// 更新逻辑永远跑不完。独立进程不受此影响。
// 失败、离线、没权限一律静默：它只影响「下次启动是不是最新版」。
try {
  const u = spawn(process.execPath, [selfUpdateJs], {
    stdio: 'ignore',
    detached: true,
    windowsHide: true,
  });
  u.on('error', () => { /* 没有 node？不可能，自身就是 node 起的 */ });
  u.unref();
} catch (_) { /* 自更新是加分项，绝不能影响 MCP 启动 */ }

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
