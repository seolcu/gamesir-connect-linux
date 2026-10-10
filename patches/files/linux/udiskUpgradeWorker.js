// Linux version of dfu/udiskUpgradWoker.js.
// Takes the same workerData and posts messages in the same format to parentPort.
// The actual upgrade is done by jlemu.py emulating the official DLL
// (same order as the original worker: load UFW → query/compare device PID → set dual backup → JL_upgradeDevice).
'use strict';
const { parentPort, workerData } = require('worker_threads');
const { spawn } = require('child_process');
const readline = require('readline');
const helper = require('./index.js');

const DualBackupBurningDeviceList = ['GS_G7_Pro_CE_DV', 'GS_XREAL_DV'];

function debug(message) {
  parentPort.postMessage(message);
}
function onProcessChange(data) {
  parentPort.postMessage(data);
}
function sendAction(action, payload) {
  parentPort.postMessage({ action, payload });
}

function run() {
  const { udisk, ufwPath } = workerData;
  const requirePidMatch = workerData.requirePidMatch !== false;
  debug('upgrade start (linux) ' + JSON.stringify({ udisk, ufwPath, requirePidMatch }));

  // Dual backup is decided by the UFW PID (the original uses the device PID, but they are equal after the PID match check)
  const pre = helper.runJlemu(['ufwinfo', ufwPath]).events.find((e) => e.event === 'ufwinfo');
  const args = ['-I', helper.JLEMU, '--dll', helper.DLL, '--device', udisk, 'upgrade', ufwPath];
  if (!requirePidMatch) args.push('--no-pid-check');
  if (pre && DualBackupBurningDeviceList.includes(pre.pid)) args.push('--dual-backup');

  const child = spawn(helper.python(), args, { stdio: ['ignore', 'pipe', 'pipe'] });
  let finished = false;
  readline.createInterface({ input: child.stderr }).on('line', (l) => debug(l));
  readline.createInterface({ input: child.stdout }).on('line', (line) => {
    let ev;
    try {
      ev = JSON.parse(line);
    } catch (e) {
      return;
    }
    if (ev.event === 'progress') {
      onProcessChange({ ret: 1, progress: ev.progress, messags: '' });
    } else if (ev.event === 'result') {
      finished = true;
      if (ev.ret === 0) {
        onProcessChange({ ret: 1, progress: 100, messags: '' });
      } else {
        onProcessChange({ ret: ev.ret, progress: 0, messags: ev.message || '' });
        sendAction('UDiskUpgradeError', { ret: ev.ret });
      }
    } else {
      debug(ev);
    }
  });
  child.on('exit', (code, signal) => {
    if (!finished) {
      onProcessChange({ ret: -99, progress: 0, messags: `upgrade error: jlemu exited (${code ?? signal})` });
      sendAction('UDiskUpgradeError', { ret: -99 });
    }
    debug('worker end');
  });
}

try {
  run();
} catch (e) {
  debug('upgrade error: ' + (e && e.stack ? e.stack : e));
  onProcessChange({ ret: -99, progress: 0, messags: 'upgrade error: upgrade exits unexpectedly' });
}
