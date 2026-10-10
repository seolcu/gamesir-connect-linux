// usb-detection replacement (Linux, no native code).
// Polls /sys/bus/usb/devices and reports changes with the same events/objects as the original module.
// The original prebuild does not match Electron 13.6.9's V8 symbols and kills the process on load.
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
    if (name.includes(':')) continue; // skip interface nodes
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
      // devnum changes when re-enumerated on the same port, so include it in the key
      _key: `${name}#${devnum}`,
    });
  }
  return found;
}

const detector = new EventEmitter();
detector.setMaxListeners(0);

let known = new Map(); // _key -> device (reported devices)
let pending = new Map(); // _key -> device (seen once, waiting for drivers to bind)
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
    // Report after two consecutive sightings: gives hidraw and other drivers time to bind
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
  // Devices already connected become the baseline without add events (same as the original)
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
