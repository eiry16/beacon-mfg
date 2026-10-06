#!/usr/bin/env node
'use strict';
// beacon-mfg-mcp 自更新检查（独立进程，由 launcher 以 detached 方式拉起）
//
// 【为什么必须独立成进程】
// launcher 用 spawnSync 同步拉起 server.py —— 那会**阻塞 Node 整个事件循环**。
// 如果把自更新写成 async 函数在主进程里跑，它在 spawnSync 期间推不动，
// 等 server 退出、事件循环恢复时，主进程紧跟着就 process.exit() 了，
// 于是更新逻辑永远跑不完。
// 独立 detached 子进程不受主进程阻塞与退出影响，是唯一稳的做法。
//
// 【三条硬约束】
//   1. 绝不阻塞 MCP 启动：本进程是 detached + unref 的，主进程不等它。
//   2. 失败一律静默降级：离线 / 内网 / 没装 npm / 无写权限，都只当没更新过。
//   3. 节流 + 可关：默认 24h 查一次；BEACON_MCP_NO_UPDATE=1 完全关闭。

const { spawnSync, spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const https = require('https');

const PKG = require('../package.json');
const CURRENT = PKG.version || '0.0.0';

const STATE_DIR = path.join(os.homedir() || '.', '.beacon-mfg');
const STAMP = path.join(STATE_DIR, 'update-check.json');
const LOCK = path.join(STATE_DIR, 'update.lock');
const REGISTRY = 'https://registry.npmjs.org/beacon-mfg-mcp/latest';
const INTERVAL_H = Number(process.env.BEACON_MCP_UPDATE_INTERVAL_H || 24);

function note(msg) {
  // 只能写 stderr：stdout 可能是 MCP 协议通道（在独立进程里虽然安全，
  // 但保持一致，将来若被复用也不会误伤）。
  try { process.stderr.write('[beacon-mfg-mcp] ' + msg + '\n'); } catch (_) {}
}

function semverGt(a, b) {
  const pa = String(a).split('.').map((x) => parseInt(x, 10) || 0);
  const pb = String(b).split('.').map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < 3; i++) {
    if ((pa[i] || 0) > (pb[i] || 0)) return true;
    if ((pa[i] || 0) < (pb[i] || 0)) return false;
  }
  return false;
}

function shouldSkip() {
  if (process.env.BEACON_MCP_NO_UPDATE === '1') return true;
  // 离线仓库用户（数据来自本地 git/目录），联网升级只会添乱
  if (process.env.BEACON_SOURCE || process.env.BEACON_REPO) return true;
  return false;
}

function readStamp() {
  try { return JSON.parse(fs.readFileSync(STAMP, 'utf8')).ts || 0; } catch (_) { return 0; }
}
function writeStamp(extra) {
  try {
    fs.mkdirSync(STATE_DIR, { recursive: true });
    fs.writeFileSync(STAMP, JSON.stringify(
      Object.assign({ ts: Date.now(), version: CURRENT }, extra || {})));
  } catch (_) {}
}
// 锁的过期时间：超过就视为持有者已死，直接接管。
// 为什么必须有：detached 子进程有可能被中途杀掉（launcher 退出、Windows job object），
// 停在「已加锁、未解锁」之间 —— 残留锁会让**此后所有启动**
// 永久跳过自更新，而且现场毫无线索。所以锁必须能自愈。
const LOCK_STALE_MS = Number(process.env.BEACON_MCP_UPDATE_LOCK_STALE_MIN || 10) * 60000;

function lockHeld() {
  // flag 'wx' = 原子创建，文件已存在会抛错。用 writeFileSync 覆盖写是**不互斥**的
  // （两个进程都会「拿到锁」然后互相 unlink），并发保护就成了摆设。
  const grab = () => {
    const fd = fs.openSync(LOCK, 'wx');
    fs.writeSync(fd, String(process.pid) + ' ' + Date.now());
    fs.closeSync(fd);
  };
  try {
    fs.mkdirSync(STATE_DIR, { recursive: true });
    grab();
    return true;
  } catch (_) {
    try {
      const st = fs.statSync(LOCK);
      if (Date.now() - st.mtimeMs > LOCK_STALE_MS) {
        fs.unlinkSync(LOCK);      // 持有者已死，回收
        grab();
        return true;
      }
    } catch (_) { /* 回收也失败：只当没拿到锁 */ }
    return false;
  }
}
function unlock() {
  try { fs.unlinkSync(LOCK); } catch (_) {}
}

function fetchLatest(timeoutMs) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    let req;
    try {
      req = https.get(REGISTRY, { headers: { Accept: 'application/json' } },
        (r) => {
          if (r.statusCode !== 200) { r.resume(); return finish(null); }
          let b = '';
          r.setEncoding('utf8');
          r.on('data', (c) => { b += c; if (b.length > 20000) r.destroy(); });
          r.on('end', () => {
            try { finish(JSON.parse(b).version || null); } catch (_) { finish(null); }
          });
        });
    } catch (_) { return finish(null); }
    req.setTimeout(timeoutMs, () => { try { req.destroy(); } catch (_) {} finish(null); });
    req.on('error', () => finish(null));
  });
}

const IS_WIN = process.platform === 'win32';
// Windows 上 npm 是 npm.cmd 批处理，必须 shell:true，否则 FileNotFoundError。
const NPM = IS_WIN ? 'npm.cmd' : 'npm';
const NPM_OPTS = { stdio: 'ignore', shell: IS_WIN, env: process.env };

async function main() {
  if (shouldSkip()) return;
  if (Date.now() - readStamp() < INTERVAL_H * 3600 * 1000) return;  // 节流
  if (!lockHeld()) return;                                          // 并发保护

  const latest = await fetchLatest(5000);
  if (!latest) { writeStamp({ result: 'registry_unreachable' }); unlock(); return; }
  writeStamp({ result: 'checked', latest });
  if (!semverGt(latest, CURRENT)) { unlock(); return; }              // 已最新

  note('发现新版 ' + latest + '（当前 ' + CURRENT + '），正在更新…');
  // 同步等安装结果：detached 进程本身就是后台的，这里等一会儿无副作用，
  // 换来的是能把「成功/失败」明确告诉用户。
  const r = spawnSync(NPM, ['i', '-g', 'beacon-mfg-mcp@' + latest],
    Object.assign({ timeout: 180000 }, NPM_OPTS));
  unlock();
  if (r.status === 0) {
    note('已更新 ' + CURRENT + ' → ' + latest + '，下次启动生效。');
  } else {
    note('自动更新未成功（可能缺权限或网络受限）。'
       + '可手动执行：npm i -g beacon-mfg-mcp@latest');
  }
}

main().catch(() => { unlock(); process.exit(0); });
