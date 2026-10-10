// GameSir Connect Linux helper (loaded by the patched main.js as globalThis.__gsLinux)
'use strict';
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const JL_UBOOT_VID = '4c4a';
const DFU_DIR = path.join(__dirname, '..');
const RESOURCES = path.join(DFU_DIR, '..');
const JLEMU = path.join(__dirname, 'jlemu.py');
const DLL = path.join(DFU_DIR, 'jl_firmware_upgrade_x86.dll');

function python() {
  const venv = path.join(RESOURCES, 'pyenv', 'bin', 'python');
  return fs.existsSync(venv) ? venv : 'python3';
}

// Finds the SCSI generic nodes (/dev/sgN) of USB mass storage devices in JieLi UBOOT mode.
// These paths take the place of the "removable drive letters" used on Windows.
function listUbootDisks() {
  const out = [];
  let sgs = [];
  try {
    sgs = fs.readdirSync('/sys/class/scsi_generic');
  } catch (e) {
    return Promise.resolve(out);
  }
  for (const sg of sgs) {
    let dir;
    try {
      dir = fs.realpathSync(path.join('/sys/class/scsi_generic', sg, 'device'));
    } catch (e) {
      continue;
    }
    // Walk up to the USB device node (the one with idVendor)
    for (let d = dir; d !== '/' && d !== '.'; d = path.dirname(d)) {
      let vid;
      try {
        vid = fs.readFileSync(path.join(d, 'idVendor'), 'utf8').trim();
      } catch (e) {
        continue;
      }
      if (vid.toLowerCase() === JL_UBOOT_VID) out.push(`/dev/${sg}`);
      break;
    }
  }
  return Promise.resolve(out);
}

// Runs jlemu.py and returns its JSON event lines
function runJlemu(args, timeoutMs = 60000) {
  const r = spawnSync(python(), ['-I', JLEMU, '--dll', DLL, ...args], {
    encoding: 'utf8',
    timeout: timeoutMs,
    maxBuffer: 16 * 1024 * 1024,
  });
  const events = [];
  for (const line of (r.stdout || '').split('\n')) {
    if (!line.trim()) continue;
    try {
      events.push(JSON.parse(line));
    } catch (e) {
      // ignore
    }
  }
  if (r.error || r.status === null) {
    events.push({ event: 'result', ret: -99, message: String(r.error || 'killed') });
  }
  return { events, stderr: r.stderr || '' };
}

const JL_ERROR_NO_FIRMWARE = -18;

// Wraps the original uDiskDfuUtil (uS) object with a Linux implementation.
// The original called the DLL directly via ffi in the main process; here jlemu.py runs synchronously.
// The DLL queries the device using the most recently loaded UFW (chip KEY), so that path is remembered and passed along.
function wrapJlUtil(orig) {
  let loadedUfw = null;
  return Object.assign({}, orig, {
    JL_upgradeInit() {
      return 0;
    },
    JL_loadFirmware(ufwPath) {
      const { events } = runJlemu(['ufwinfo', ufwPath]);
      const info = events.find((e) => e.event === 'ufwinfo');
      const ret = info ? info.ret : JL_ERROR_NO_FIRMWARE;
      loadedUfw = ret === 0 ? ufwPath : null;
      return ret;
    },
    JL_queryDevicePidVid(disk) {
      const args = ['--device', disk, 'query'];
      if (loadedUfw) args.push('--ufw', loadedUfw);
      const { events } = runJlemu(args);
      const q = events.find((e) => e.event === 'query');
      if (!q) return { ret: -9, descRet: -9 };
      const out = { ret: q.ret, descRet: q.descRet, deviceDesc: q.deviceDesc || undefined };
      if (q.ret === 0) {
        out.vid = q.vid;
        out.pid = q.pid;
      }
      return out;
    },
    JL_queryDeviceDesc(disk) {
      const { events } = runJlemu(['--device', disk, 'query']);
      const q = events.find((e) => e.event === 'query');
      if (!q || q.descRet !== 0) return { descRet: q ? q.descRet : -9 };
      return { descRet: 0, deviceDesc: q.deviceDesc };
    },
  });
}

module.exports = { listUbootDisks, wrapJlUtil, runJlemu, python, JLEMU, DLL };
