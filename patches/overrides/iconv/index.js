// iconv replacement: provides only `new Iconv(from, to).convert(buf)`, which ref-wchar-napi uses, via iconv-lite.
// The original ships Windows native binaries only.
'use strict';
const iconvLite = require('iconv-lite');

class Iconv {
  constructor(from, to) {
    this.from = from;
    this.to = to;
  }

  convert(input, encoding) {
    const buf = Buffer.isBuffer(input) ? input : Buffer.from(String(input), encoding || 'utf8');
    return iconvLite.encode(iconvLite.decode(buf, this.from), this.to);
  }
}

module.exports = { Iconv };
