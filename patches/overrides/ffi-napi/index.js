// ffi-napi replacement (Linux).
// The app only uses ffi to call Windows DLLs (XInputUtil, JieLi upgrade, SAPJoyUni SDKs, user32/kernel32).
// Those DLLs cannot be used on Linux, and the original prebuild mixes symbols with the system
// libffi.so.8 and crashes with SIGSEGV when creating callbacks. So Library throws a clear error,
// and Callback (created at module load time) returns a dummy that is never invoked.
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

// A pointer-sized NULL buffer that is never passed to native code
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
