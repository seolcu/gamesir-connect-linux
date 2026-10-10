#!/usr/bin/env node
// Applies the Linux patches to the GameSir Connect bundle.
// Each patch replaces an exact string the expected number of times; if the original
// changed and the string no longer matches, the build fails.
'use strict';
const fs = require('fs');
const path = require('path');

const res = process.argv[2];
if (!res) {
  console.error('usage: apply.js <resources dir>');
  process.exit(2);
}

const patches = require('./patches');

let failed = false;
for (const p of patches) {
  const file = path.join(res, p.file);
  let src = fs.readFileSync(file, 'utf8');
  if (src.includes(p.marker)) {
    console.log(`  - ${p.name}: already applied`);
    continue;
  }
  let ok = true;
  for (const [from, to, count = 1] of p.replace || []) {
    const n = src.split(from).length - 1;
    if (n !== count) {
      console.error(`  ! ${p.name}: expected '${from.slice(0, 60)}…' ${count} time(s), found ${n}`);
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
