#!/usr/bin/env node
// GameSir Connect 번들에 Linux용 패치를 적용한다.
// 각 패치는 정확히 일치하는 문자열을 지정된 횟수만큼 치환하며, 원본이 바뀌어 일치하지 않으면 빌드를 실패시킨다.
'use strict';
const fs = require('fs');
const path = require('path');

const res = process.argv[2];
if (!res) {
  console.error('usage: apply.js <resources dir>');
  process.exit(2);
}
const appDir = path.join(res, 'app');

const patches = require('./patches');

let failed = false;
for (const p of patches) {
  const file = path.join(res, p.file);
  let src = fs.readFileSync(file, 'utf8');
  if (src.includes(p.marker)) {
    console.log(`  - ${p.name}: 이미 적용됨`);
    continue;
  }
  let ok = true;
  for (const [from, to, count = 1] of p.replace || []) {
    const n = src.split(from).length - 1;
    if (n !== count) {
      console.error(`  ! ${p.name}: '${from.slice(0, 60)}…' ${count}회 기대, ${n}회 발견`);
      ok = false;
      break;
    }
    src = src.split(from).join(typeof to === 'function' ? to() : to);
  }
  if (!ok) { failed = true; continue; }
  if (p.prepend) src = p.prepend + src;
  src = (p.html ? `<!--${p.marker}-->` : `/*${p.marker}*/`) + src;
  fs.writeFileSync(file, src);
  console.log(`  + ${p.name}`);
}

const filesDir = path.join(__dirname, 'files');
for (const f of fs.existsSync(filesDir) ? fs.readdirSync(filesDir) : []) {
  fs.cpSync(path.join(filesDir, f), path.join(res, 'dfu', f), { recursive: true });
  console.log(`  + dfu/${f}`);
}

if (failed) process.exit(1);
void appDir;
