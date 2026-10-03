#!/usr/bin/env node
'use strict';
// selfupdate.js 自更新逻辑的单元测试（不联网、不装包，只测纯逻辑）
//
// 【踩过的坑，留给下一个人】
// 测试用 src.slice(start, end) 截取被测代码段再 new Function 求值，两个隐性依赖
// 会让测试「莫名 FAIL 而不报错」：
//   1. 起点必须覆盖 selfupdate.js 顶部那批 const（PKG/CURRENT/STATE_DIR/…），
//      否则函数体里是未定义引用；又因为函数内有 try/catch，异常被吞掉，
//      症状只是「写盘没生效」，排查成本极高。
//   2. 依赖的 fs/os/path/https 要从外部注入；但**不能**注入 body 里已声明的名字，
//      否则 Identifier already declared。
// 现在下面用完整性断言把这类问题挡在测试里，而不是靠人肉排查。

const fs = require('fs');
const path = require('path');
const os = require('os');

const TARGET = path.join(__dirname, 'bin', 'selfupdate.js');
const src = fs.readFileSync(TARGET, 'utf8');

// 截取点：起点用 `const STATE_DIR`（自更新段真正的第一行），
// 终点在 `const IS_WIN` 之前（网络 / spawn 不测）。
// 为什么不从 `const PKG = require(...)` 起：new Function 里的 require 归属
// **本测试文件**，`require('../package.json')` 会相对 mcp/ 的父目录解析而找不到。
// PKG/CURRENT 改为显式注入。
const start = src.indexOf('const STATE_DIR');
const end = src.indexOf('const IS_WIN');
if (start < 0 || end < 0) {
  console.error('FAIL: 没能定位 selfupdate.js 的可测段');
  process.exit(1);
}
const body = src.slice(start, end);
for (const need of ['function writeStamp', 'function lockHeld', 'function unlock',
                     'function shouldSkip', 'function readStamp',
                     'const STATE_DIR', 'LOCK_STALE_MS']) {
  if (!body.includes(need)) {
    console.error('FAIL: 截取段不完整，缺 ' + need);
    process.exit(1);
  }
}

const mod = { exports: {} };
const CURRENT = require(path.join(__dirname, 'package.json')).version;
new Function('module', 'require', 'process', 'fs', 'os', 'path', 'https',
             'CURRENT', body +
  ';module.exports={semverGt,shouldSkip,readStamp,writeStamp,lockHeld,unlock,LOCK};'
)(mod, require, process, require('fs'), require('os'),
  require('path'), require('https'), CURRENT);
const M = mod.exports;

let pass = 0, fail = 0;
function eq(label, got, exp) {
  if (got === exp) { pass++; console.log('  ok   ' + label); }
  else { fail++; console.log('  FAIL ' + label + '  got=' + got + '  exp=' + exp); }
}

console.log('[1] semverGt(a,b) = 「a 是否新于 b」');
const T = [
  ['1.4.1', '1.4.2', false], ['1.4.1', '1.4.1', false], ['1.4.2', '1.4.1', true],
  ['1.3.3', '1.4.1', false], ['1.10.0', '1.9.0', true], ['1.9.0', '1.10.0', false],
  ['2.0.0', '1.9.9', true], ['1.4', '1.4.0', false], ['1.4.10', '1.4.9', true],
];
for (const [a, b, exp] of T) eq(a + ' > ' + b, M.semverGt(a, b), exp);

console.log('[2] 跳过条件');
process.env.BEACON_MCP_NO_UPDATE = '1';
eq('BEACON_MCP_NO_UPDATE=1', M.shouldSkip(), true);
delete process.env.BEACON_MCP_NO_UPDATE;
process.env.BEACON_SOURCE = 'file:///x';
eq('BEACON_SOURCE（离线仓库）', M.shouldSkip(), true);
delete process.env.BEACON_SOURCE;
process.env.BEACON_REPO = '/opt/beacon';
eq('BEACON_REPO（离线仓库）', M.shouldSkip(), true);
delete process.env.BEACON_REPO;
eq('默认不跳过', M.shouldSkip(), false);

console.log('[3] 节流时间戳');
const STAMP = path.join(os.homedir(), '.beacon-mfg', 'update-check.json');
const bak = fs.existsSync(STAMP) ? fs.readFileSync(STAMP) : null;
if (bak) fs.unlinkSync(STAMP);
eq('无戳时 ts=0（首启会查）', M.readStamp(), 0);
M.writeStamp({ result: 'checked' });
eq('writeStamp 后 ts>0', M.readStamp() > 0, true);
fs.writeFileSync(STAMP, JSON.stringify({ ts: Date.now() - 25 * 3600 * 1000 }));
eq('25h 前（超 24h 节流窗口）', M.readStamp() < Date.now() - 24 * 3600 * 1000, true);
fs.writeFileSync(STAMP, JSON.stringify({ ts: Date.now() - 3600 * 1000 }));
eq('1h 前（节流窗口内，不该再查）', M.readStamp() > Date.now() - 2 * 3600 * 1000, true);

console.log('[4] 锁：互斥 + 过期接管');
if (fs.existsSync(M.LOCK)) fs.unlinkSync(M.LOCK);
eq('首次加锁成功', M.lockHeld(), true);
eq('锁文件已建', fs.existsSync(M.LOCK), true);
eq('第二次加锁必须失败（互斥）', M.lockHeld(), false);
M.unlock();
eq('unlock 后锁消失', fs.existsSync(M.LOCK), false);
// 模拟「持有者被杀」的残留锁
fs.writeFileSync(M.LOCK, '99999 ' + Date.now());
eq('新鲜残留锁不可抢', M.lockHeld(), false);
fs.unlinkSync(M.LOCK);
const old = Date.now() - 30 * 60000;
fs.writeFileSync(M.LOCK, '99999 ' + old);
fs.utimesSync(M.LOCK, new Date(old), new Date(old));
eq('30 分钟前的残留锁可被接管', M.lockHeld(), true);
M.unlock();

if (bak) fs.writeFileSync(STAMP, bak);
else { try { fs.unlinkSync(STAMP); } catch (_) {} }
try { fs.unlinkSync(M.LOCK); } catch (_) {}

console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
process.exit(fail ? 1 : 0);
