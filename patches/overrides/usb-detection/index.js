// usb-detection 대체 구현 (Linux, 네이티브 코드 없음).
// /sys/bus/usb/devices 를 주기적으로 읽어 원본 모듈과 같은 이벤트/객체 형태로 알린다.
// 원본 prebuild는 Electron 13.6.9의 V8 심볼과 맞지 않아 로드 시 프로세스가 죽는다.
'use strict';
const fs = require('fs');
const path = require('path');
const { EventEmitter } = require('events');

const SYSFS = '/sys/bus/usb/devices';
const POLL_MS = 400;

function readAttr(dir, name) {
  try {
    return fs.readFileSync(path.join(dir, name), 'utf8').trim();
  } catch (e) {
    return '';
  }
}

function scan() {
  const found = new Map();
  let entries = [];
  try {
    entries = fs.readdirSync(SYSFS);
  } catch (e) {
    return found;
  }
  for (const name of entries) {
    if (name.includes(':')) continue; // 인터페이스 노드는 건너뛴다
    const dir = path.join(SYSFS, name);
    const vid = readAttr(dir, 'idVendor');
    if (!vid) continue;
    const busnum = parseInt(readAttr(dir, 'busnum'), 10) || 0;
    const devnum = parseInt(readAttr(dir, 'devnum'), 10) || 0;
    found.set(name, {
      locationId: busnum,
      vendorId: parseInt(vid, 16),
      productId: parseInt(readAttr(dir, 'idProduct'), 16),
      deviceName: readAttr(dir, 'product') || 'USB Device',
      manufacturer: readAttr(dir, 'manufacturer'),
      serialNumber: readAttr(dir, 'serial'),
      deviceAddress: devnum,
      // 같은 포트에 재열거되면 devnum이 바뀌므로 키에 포함
      _key: `${name}#${devnum}`,
    });
  }
  return found;
}

const detector = new EventEmitter();
detector.setMaxListeners(0);

let known = new Map(); // _key -> device (보고된 장치)
let pending = new Map(); // _key -> device (한 번만 본 장치, 드라이버 바인딩 대기)
let timer = null;

function emitAll(kind, device) {
  const out = Object.assign({}, device);
  delete out._key;
  const v = out.vendorId;
  const p = out.productId;
  if (kind === 'add') {
    for (const ev of [`add:${v}:${p}`, `insert:${v}:${p}`, `add:${v}`, `insert:${v}`, 'add', 'insert']) detector.emit(ev, out);
  } else {
    for (const ev of [`remove:${v}:${p}`, `remove:${v}`, 'remove']) detector.emit(ev, out);
  }
  for (const ev of [`change:${v}:${p}`, `change:${v}`, 'change']) detector.emit(ev, out);
}

function poll() {
  const now = new Map();
  for (const d of scan().values()) now.set(d._key, d);
  for (const [k, d] of known) {
    if (!now.has(k)) {
      known.delete(k);
      emitAll('remove', d);
    }
  }
  const nextPending = new Map();
  for (const [k, d] of now) {
    if (known.has(k)) continue;
    // 두 번 연속 보였을 때 알린다: hidraw 등 하위 드라이버가 붙을 시간을 준다
    if (pending.has(k)) {
      known.set(k, d);
      emitAll('add', d);
    } else {
      nextPending.set(k, d);
    }
  }
  pending = nextPending;
}

function list(vid, pid) {
  return [...scan().values()]
    .filter((d) => (vid == null || d.vendorId === vid) && (pid == null || d.productId === pid))
    .map((d) => {
      const o = Object.assign({}, d);
      delete o._key;
      return o;
    });
}

detector.find = function (...args) {
  const cb = typeof args[args.length - 1] === 'function' ? args.pop() : null;
  const [vid, pid] = args;
  const result = list(vid, pid);
  if (cb) {
    setImmediate(() => cb(undefined, result));
    return undefined;
  }
  return Promise.resolve(result);
};

detector.startMonitoring = function () {
  if (timer) return;
  // 이미 연결된 장치는 add 이벤트 없이 기준선으로 둔다 (원본 동작과 동일)
  known = new Map();
  for (const d of scan().values()) known.set(d._key, d);
  pending = new Map();
  timer = setInterval(poll, POLL_MS);
};

detector.stopMonitoring = function () {
  if (timer) clearInterval(timer);
  timer = null;
};

detector.version = 'sysfs-shim';
module.exports = detector;
