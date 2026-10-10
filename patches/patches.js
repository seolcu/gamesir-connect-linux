// Linux 포팅 패치 목록. 경로는 resources/ 기준.
// replace: [찾을 문자열, 바꿀 문자열, 기대 횟수(기본 1)]
// 원본이 바뀌어 기대 횟수와 다르면 빌드가 실패한다 (조용히 잘못 패치되는 것을 막기 위함).
'use strict';

// Windows 시스템 글꼴 이름을 앱 내장 Source Han Sans로 연결한다 (CJK 범위만).
// Linux에서는 이 이름들이 시스템 Noto Sans CJK 가변 폰트로 대체되는데, Chrome 91이 이를 □로 그린다.
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

// main.js 번들 앞에 붙는 Linux 헬퍼 로더. __dirname = resources/app/dist/electron
const LINUX_HELPER = `globalThis.__gsLinux=process.platform==="linux"?require(require("path").join(__dirname,"../../../dfu/linux/index.js")):null;`;

module.exports = [
  {
    name: 'index.html: CJK 글꼴 별칭',
    file: 'app/dist/electron/index.html',
    marker: 'gs-linux:fonts',
    html: true,
    replace: [['</head>', `${CJK_FONT_ALIASES}</head>`]],
  },
  {
    name: 'main: Linux 헬퍼 로드',
    file: 'app/dist/electron/main.js',
    marker: 'gs-linux:helper',
    prepend: LINUX_HELPER,
  },
  {
    name: 'main: 서비스 초기화 개별 보호',
    // 다른 기종 SDK(Windows DLL) init이 실패하면 하트비트가 시작되지 않던 문제
    file: 'app/dist/electron/main.js',
    marker: 'gs-linux:services',
    replace: [[
      '{key:"initializeServices",value:function(){TS(),Ul.init(),qd.init(),this.startHeartbeat()}}',
      '{key:"initializeServices",value:function(){TS();for(const s of[Ul,qd])try{s.init()}catch(e){ja.a.warn("service init skipped on linux:",e&&e.message)}this.startHeartbeat()}}',
    ]],
  },
  {
    name: 'main: 이동식 디스크 열거 (PowerShell/wmic → Linux)',
    file: 'app/dist/electron/main.js',
    marker: 'gs-linux:disks',
    replace: [[
      `return new Promise((e,t)=>{try{lS.exec('powershell -command "Get-WmiObject Win32_LogicalDisk`,
      `if(globalThis.__gsLinux)return globalThis.__gsLinux.listUbootDisks();return new Promise((e,t)=>{try{lS.exec('powershell -command "Get-WmiObject Win32_LogicalDisk`,
    ]],
  },
  {
    name: 'main: JieLi 장치 식별 (ffi DLL → jlemu)',
    file: 'app/dist/electron/main.js',
    marker: 'gs-linux:jlutil',
    replace: [[
      '};const lS=r(121),cS=r(1),dS=r(7),{app:pS}=r(3);let fS=null;',
      '};globalThis.__gsLinux&&(uS=globalThis.__gsLinux.wrapJlUtil(uS));const lS=r(121),cS=r(1),dS=r(7),{app:pS}=r(3);let fS=null;',
    ]],
  },
  {
    name: 'main: U-disk 업그레이드 worker (→ linux/udiskUpgradeWorker.js)',
    file: 'app/dist/electron/main.js',
    marker: 'gs-linux:worker',
    replace: [[
      'o=se("udiskUpgradWoker.js")',
      'o=se(globalThis.__gsLinux?"linux/udiskUpgradeWorker.js":"udiskUpgradWoker.js")',
    ]],
  },
];
