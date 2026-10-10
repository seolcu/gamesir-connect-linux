// Linux port patch list. Paths are relative to resources/.
// replace: [search string, replacement, expected count (default 1)]
// If the original changes and the count differs, the build fails
// (prevents silently applying a wrong patch).
'use strict';

// Map Windows system font names to the app's bundled Source Han Sans (CJK ranges only).
// On Linux these names fall back to the system Noto Sans CJK variable font,
// which Chrome 91 renders as tofu boxes.
const CJK_FONT_ALIASES = (() => {
  const families = ['Segoe UI', 'Microsoft YaHei', 'Malgun Gothic', 'Yu Gothic UI'];
  const ranges = [
    ['SourceHanSans-KO--fonts.ttf', 'U+1100-11FF,U+3130-318F,U+A960-A97F,U+AC00-D7AF,U+D7B0-D7FF'],
    ['SourceHanSans-JA--fonts.ttf', 'U+3040-30FF,U+31F0-31FF'],
    ['SourceHanSans-CN--fonts.ttf', 'U+2E80-2FDF,U+3000-303F,U+3400-4DBF,U+4E00-9FFF,U+F900-FAFF,U+FF00-FFEF'],
  ];
  const rules = [];
  for (const f of families) {
    for (const [file, range] of ranges) {
      rules.push(`@font-face{font-family:"${f}";src:url("fonts/${file}");unicode-range:${range}}`);
    }
  }
  return `<style id="gs-linux-fonts">${rules.join('')}</style>`;
})();

// Linux helper loader prepended to the main.js bundle. __dirname = resources/app/dist/electron
const LINUX_HELPER = `globalThis.__gsLinux=process.platform==="linux"?require(require("path").join(__dirname,"../../../dfu/linux/index.js")):null;`;

module.exports = [
  {
    name: 'index.html: CJK font aliases',
    file: 'app/dist/electron/index.html',
    marker: 'gs-linux:fonts',
    html: true,
    replace: [['</head>', `${CJK_FONT_ALIASES}</head>`]],
  },
  {
    name: 'main: load Linux helper',
    file: 'app/dist/electron/main.js',
    marker: 'gs-linux:helper',
    prepend: LINUX_HELPER,
  },
  {
    name: 'main: guard each service init',
    // Failing init of other models' SDKs (Windows DLLs) prevented the heartbeat from starting
    file: 'app/dist/electron/main.js',
    marker: 'gs-linux:services',
    replace: [[
      '{key:"initializeServices",value:function(){TS(),Ul.init(),qd.init(),this.startHeartbeat()}}',
      '{key:"initializeServices",value:function(){TS();for(const s of[Ul,qd])try{s.init()}catch(e){ja.a.warn("service init skipped on linux:",e&&e.message)}this.startHeartbeat()}}',
    ]],
  },
  {
    name: 'main: removable disk enumeration (PowerShell/wmic -> Linux)',
    file: 'app/dist/electron/main.js',
    marker: 'gs-linux:disks',
    replace: [[
      `return new Promise((e,t)=>{try{lS.exec('powershell -command "Get-WmiObject Win32_LogicalDisk`,
      `if(globalThis.__gsLinux)return globalThis.__gsLinux.listUbootDisks();return new Promise((e,t)=>{try{lS.exec('powershell -command "Get-WmiObject Win32_LogicalDisk`,
    ]],
  },
  {
    name: 'main: JieLi device identification (ffi DLL -> jlemu)',
    file: 'app/dist/electron/main.js',
    marker: 'gs-linux:jlutil',
    replace: [[
      '};const lS=r(121),cS=r(1),dS=r(7),{app:pS}=r(3);let fS=null;',
      '};globalThis.__gsLinux&&(uS=globalThis.__gsLinux.wrapJlUtil(uS));const lS=r(121),cS=r(1),dS=r(7),{app:pS}=r(3);let fS=null;',
    ]],
  },
  {
    name: 'main: U-disk upgrade worker (-> linux/udiskUpgradeWorker.js)',
    file: 'app/dist/electron/main.js',
    marker: 'gs-linux:worker',
    replace: [[
      'o=se("udiskUpgradWoker.js")',
      'o=se(globalThis.__gsLinux?"linux/udiskUpgradeWorker.js":"udiskUpgradWoker.js")',
    ]],
  },
];
