// GameSir Connect Linux 헬퍼 (패치된 main.js에서 globalThis.__gsLinux 로 로드됨)
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

// JieLi UBOOT 모드로 붙은 USB 저장장치의 SCSI generic 노드(/dev/sgN)를 찾는다.
// Windows판의 "이동식 드라이브 문자" 자리에 이 경로가 들어간다.
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
    // 상위로 올라가며 USB 장치 노드(idVendor)를 찾는다
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

// jlemu.py 를 실행하고 JSON 이벤트 줄들을 돌려준다
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
      // 무시
    }
  }
  if (r.error || r.status === null) {
    events.push({ event: 'result', ret: -99, message: String(r.error || 'killed') });
  }
  return { events, stderr: r.stderr || '' };
}

const JL_ERROR_NO_FIRMWARE = -18;

// 원본 uDiskDfuUtil(uS) 객체를 Linux 구현으로 감싼다.
// 원본은 메인 프로세스에서 ffi로 DLL을 직접 불렀다. 여기서는 jlemu.py를 동기 실행한다.
// DLL은 마지막에 불러온 UFW(칩 KEY)를 기준으로 장치를 조회하므로, 그 경로를 기억해 함께 넘긴다.
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
