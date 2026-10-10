// iconv 대체 구현: ref-wchar-napi가 쓰는 `new Iconv(from, to).convert(buf)`만 iconv-lite로 제공한다.
// 원본은 Windows용 네이티브 바이너리만 들어 있다.
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
