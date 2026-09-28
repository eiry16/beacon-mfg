#!/usr/bin/env node
'use strict';
// prepack 步骤：把仓库平级的 skills/rfq-kernel 拷进 mcp/rfq-kernel，
// 使 npm 包与 GitHub Release tarball 自带 rfq-kernel —— 否则 start_sourcing /
// answer_sourcing / refine_sourcing 三个寻源 tool 会因桥接缺失而降级失效。
// 幂等：每次先清掉旧副本再拷；跳过 __pycache__ 与 *.pyc。
const fs = require('fs');
const path = require('path');

const HERE = __dirname;                 // mcp
const SRC = path.resolve(HERE, '..', 'skills', 'rfq-kernel');
const DEST = path.join(HERE, 'rfq-kernel');

function rmSync(p) {
  if (!fs.existsSync(p)) return;
  const st = fs.statSync(p);
  if (st.isDirectory()) {
    for (const e of fs.readdirSync(p)) rmSync(path.join(p, e));
    try { fs.rmdirSync(p); } catch (e) {}
  } else {
    try { fs.unlinkSync(p); } catch (e) {}
  }
}

function copySync(src, dest) {
  const st = fs.statSync(src);
  if (st.isDirectory()) {
    if (path.basename(src) === '__pycache__') return;   // 不携带编译缓存
    fs.mkdirSync(dest, { recursive: true });
    for (const e of fs.readdirSync(src)) copySync(path.join(src, e), path.join(dest, e));
  } else {
    if (src.endsWith('.pyc')) return;
    fs.copyFileSync(src, dest);
  }
}

if (!fs.existsSync(SRC) || !fs.statSync(SRC).isDirectory()) {
  process.stderr.write('[copy-rfk] 源目录不存在，跳过：' + SRC + '\n');
  process.exit(0);
}
rmSync(DEST);
copySync(SRC, DEST);
rmSync(path.join(DEST, '__pycache__'));
process.stdout.write('[copy-rfk] 已同步 rfq-kernel -> ' + DEST + '\n');
