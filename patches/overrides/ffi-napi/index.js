// ffi-napi 대체 구현 (Linux).
// 앱은 ffi를 Windows DLL(XInputUtil, JieLi 업그레이드, SAPJoyUni SDK, user32/kernel32) 호출에만 쓴다.
// Linux에서는 그 DLL들을 쓸 수 없고, 원본 prebuild는 시스템 libffi.so.8과 심볼이 섞여
// 콜백 생성 시 SIGSEGV로 죽는다. 그래서 Library는 명확한 오류를 던지고,
// 모듈 로드 시점에 만들어지는 Callback은 호출되지 않는 더미를 돌려준다.
'use strict';
const ref = require('ref-napi');

class FfiUnavailableError extends Error {
  constructor(what) {
    super(`ffi-napi is not available on Linux (${what})`);
    this.code = 'FFI_UNAVAILABLE';
  }
}

function Library(libFile) {
  throw new FfiUnavailableError(`Library(${libFile})`);
}

function DynamicLibrary(libFile) {
  throw new FfiUnavailableError(`DynamicLibrary(${libFile})`);
}

function ForeignFunction() {
  throw new FfiUnavailableError('ForeignFunction');
}

function VariadicForeignFunction() {
  throw new FfiUnavailableError('VariadicForeignFunction');
}

// 네이티브 코드에 넘겨질 일이 없는 NULL 포인터 크기의 버퍼
function Callback() {
  const buf = Buffer.alloc(ref.sizeof.pointer);
  buf.type = ref.refType(ref.types.void);
  return buf;
}

module.exports = {
  Library,
  DynamicLibrary,
  ForeignFunction,
  VariadicForeignFunction,
  Callback,
  types: ref.types,
  FfiUnavailableError,
  LIB_EXT: '.so',
};
