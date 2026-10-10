#!/usr/bin/env python3
"""JieLi 펌웨어 업그레이드 DLL(jl_firmware_upgrade_x86.dll)을 Linux에서 실행한다.

공식 GameSir Connect가 쓰는 32비트 Windows DLL을 Unicorn(x86 에뮬레이터)에 올려
업그레이드 로직은 원본 그대로 실행하고, Windows API 중 장치 입출력만 Linux로 연결한다:

  CreateFileA("\\\\.\\X:")              -> /dev/sgN 열기
  DeviceIoControl(SCSI_PASS_THROUGH_DIRECT) -> SG_IO ioctl
  DeviceIoControl(STORAGE_QUERY_PROPERTY)   -> SCSI INQUIRY로 장치 설명자 생성

나머지 API(힙, 이벤트, 스레드, 리소스, CRT 초기화 등)는 최소한으로 흉내 낸다.
구현하지 않은 API가 호출되면 즉시 실패한다 (추측으로 진행하지 않는다).

DLL의 작업 스레드는 협조적으로 실행된다: 신호되지 않은 이벤트를 기다리는 스레드는
CPU 컨텍스트를 저장하고 실행 가능한 다른 스레드로 전환한다.

출력: stdout 에 JSON 한 줄씩 ({"event": ...}). 로그는 stderr.
"""
import argparse
import ctypes
import fcntl
import json
import os
import struct
import sys
import time

import pefile
from unicorn import Uc, UcError, UC_ARCH_X86, UC_MODE_32, UC_HOOK_CODE, UC_HOOK_MEM_INVALID
from unicorn.x86_const import (
    UC_X86_REG_EAX, UC_X86_REG_ECX, UC_X86_REG_EDX, UC_X86_REG_ESP, UC_X86_REG_EBP,
    UC_X86_REG_EIP, UC_X86_REG_FS, UC_X86_REG_DS, UC_X86_REG_ES, UC_X86_REG_GS, UC_X86_REG_SS,
    UC_X86_REG_GDTR,
)

# ---------------------------------------------------------------- 메모리 배치
STUB_BASE = 0x7E000000      # import 스텁 (각 16바이트, int3로 채움)
STUB_SIZE = 0x10000
HEAP_BASE = 0x20000000
HEAP_SIZE = 0x10000000      # 256MB 범프 할당기
STACK_SIZE = 0x200000
STACKS_BASE = 0x30000000    # 스레드별 스택
TEB_BASE = 0x7FFD0000       # 스레드별 TEB (0x1000 간격)
PEB_ADDR = 0x7FFDF000
MISC_BASE = 0x7FF00000      # 환경 문자열, 커맨드라인 등
MODULE_PATH = 'C:\\gsc\\dfu\\jl_firmware_upgrade_x86.dll'   # DLL이 보는 자기 경로
GDT_ADDR = 0x7FFE0000       # GDT: 1=flat data(ring3) 2=flat stack(ring0) 3+n=스레드 n의 FS(TEB)

RET_SENTINEL = STUB_BASE + STUB_SIZE - 0x10       # 최상위 호출의 복귀 주소
THREAD_EXIT = STUB_BASE + STUB_SIZE - 0x20        # 작업 스레드 함수의 복귀 주소
PROGRESS_CB = STUB_BASE + STUB_SIZE - 0x30        # 진행률 콜백 (cdecl, int)

INVALID_HANDLE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102

IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x4D014
IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400

SG_IO = 0x2285
SG_DXFER_NONE = -1
SG_DXFER_TO_DEV = -2
SG_DXFER_FROM_DEV = -3


def log(*a):
    print('[jlemu]', *a, file=sys.stderr, flush=True)


def emit(**kw):
    print(json.dumps(kw), flush=True)


class EmuError(Exception):
    pass


# ---------------------------------------------------------------- SG_IO
class SgIoHdr(ctypes.Structure):
    _fields_ = [
        ('interface_id', ctypes.c_int),
        ('dxfer_direction', ctypes.c_int),
        ('cmd_len', ctypes.c_ubyte),
        ('mx_sb_len', ctypes.c_ubyte),
        ('iovec_count', ctypes.c_ushort),
        ('dxfer_len', ctypes.c_uint),
        ('dxferp', ctypes.c_void_p),
        ('cmdp', ctypes.c_void_p),
        ('sbp', ctypes.c_void_p),
        ('timeout', ctypes.c_uint),
        ('flags', ctypes.c_uint),
        ('pack_id', ctypes.c_int),
        ('usr_ptr', ctypes.c_void_p),
        ('status', ctypes.c_ubyte),
        ('masked_status', ctypes.c_ubyte),
        ('msg_status', ctypes.c_ubyte),
        ('sb_len_wr', ctypes.c_ubyte),
        ('host_status', ctypes.c_ushort),
        ('driver_status', ctypes.c_ushort),
        ('resid', ctypes.c_int),
        ('duration', ctypes.c_uint),
        ('info', ctypes.c_uint),
    ]


class ScsiDevice:
    """/dev/sgN 에 SCSI 명령을 보낸다. readonly 이면 DATA_OUT 명령은 보내지 않고 실패시킨다."""

    def __init__(self, path, readonly, trace):
        self.path = path
        self.readonly = readonly
        self.trace = trace
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        fl = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)
        self.blocked = 0

    def close(self):
        os.close(self.fd)

    def command(self, cdb, direction, data_len, data_out=b'', timeout_ms=10000):
        """returns (ok, scsi_status, data_in, sense, transferred)"""
        if self.trace:
            log(f'SCSI {"IN " if direction == SG_DXFER_FROM_DEV else "OUT" if direction == SG_DXFER_TO_DEV else "-- "}'
                f' cdb={cdb.hex()} len={data_len}' + (f' data={data_out[:32].hex()}' if data_out else ''))
        if self.readonly and direction == SG_DXFER_TO_DEV:
            self.blocked += 1
            log(f'  -> readonly 모드: DATA_OUT 명령 차단 (cdb={cdb.hex()})')
            return False, 0, b'', b'', 0
        cmd = (ctypes.c_ubyte * len(cdb)).from_buffer_copy(cdb)
        sense = (ctypes.c_ubyte * 32)()
        buf = (ctypes.c_ubyte * max(data_len, 1))()
        if direction == SG_DXFER_TO_DEV and data_out:
            ctypes.memmove(buf, data_out, min(len(data_out), data_len))
        hdr = SgIoHdr()
        hdr.interface_id = ord('S')
        hdr.dxfer_direction = direction if data_len else SG_DXFER_NONE
        hdr.cmd_len = len(cdb)
        hdr.mx_sb_len = len(sense)
        hdr.dxfer_len = data_len
        hdr.dxferp = ctypes.addressof(buf)
        hdr.cmdp = ctypes.addressof(cmd)
        hdr.sbp = ctypes.addressof(sense)
        hdr.timeout = max(timeout_ms, 1000)
        try:
            fcntl.ioctl(self.fd, SG_IO, hdr)
        except OSError as e:
            log(f'  -> SG_IO 실패: {e}')
            return False, 0, b'', b'', 0
        got = data_len - max(hdr.resid, 0)
        data_in = bytes(buf)[:data_len] if direction == SG_DXFER_FROM_DEV else b''
        if self.trace:
            log(f'  -> status={hdr.status} host={hdr.host_status} driver={hdr.driver_status} resid={hdr.resid}'
                + (f' in={data_in[:32].hex()}' if data_in else ''))
        if hdr.host_status or (hdr.driver_status & 0xF) not in (0, 8):  # DRIVER_SENSE(8)은 정상 경로
            return False, hdr.status, data_in, bytes(sense)[:hdr.sb_len_wr], got
        return True, hdr.status, data_in, bytes(sense)[:hdr.sb_len_wr], got

    def inquiry(self):
        ok, st, data, _, _ = self.command(bytes([0x12, 0, 0, 0, 36, 0]), SG_DXFER_FROM_DEV, 36)
        if not ok or st != 0 or len(data) < 36:
            raise EmuError('INQUIRY 실패')
        return data[8:16], data[16:32], data[32:36]


# ---------------------------------------------------------------- 에뮬레이터
class Thread:
    def __init__(self, tid, teb):
        self.tid = tid
        self.teb = teb
        self.ctx = None
        self.waiting = None      # 기다리는 핸들
        self.done = False
        self.last_error = 0
        self.tls = {}


class Emulator:
    def __init__(self, dll_path, device=None, letter='E'):
        self.uc = Uc(UC_ARCH_X86, UC_MODE_32)
        self.dll_path = dll_path
        self.pe = pefile.PE(dll_path)
        self.device = device
        self.letter = letter.upper()
        self.stubs = {}           # addr -> (name, nargs, fn)
        self.heap_next = HEAP_BASE
        self.heap_sizes = {}
        self.handles = {}         # handle -> object
        self.next_handle = 0x100
        self.events = {}          # handle -> {'manual':bool,'signaled':bool}
        self.threads = []
        self.cur = None
        self.tls_next = 1
        self.progress = []
        self.progress_cb = None
        self._load()

    # ---- 기본 메모리 도구
    def r32(self, a):
        return struct.unpack('<I', self.uc.mem_read(a, 4))[0]

    def w32(self, a, v):
        self.uc.mem_write(a, struct.pack('<I', v & 0xFFFFFFFF))

    def w16(self, a, v):
        self.uc.mem_write(a, struct.pack('<H', v & 0xFFFF))

    def rbytes(self, a, n):
        return bytes(self.uc.mem_read(a, n)) if n else b''

    def cstr(self, a, maxlen=4096):
        if not a:
            return None
        out = bytearray()
        while len(out) < maxlen:
            c = self.uc.mem_read(a + len(out), 1)[0]
            if c == 0:
                break
            out.append(c)
        return out.decode('latin-1')

    def wstr(self, a, maxlen=4096):
        if not a:
            return None
        out = []
        while len(out) < maxlen:
            c = struct.unpack('<H', self.uc.mem_read(a + 2 * len(out), 2))[0]
            if c == 0:
                break
            out.append(chr(c))
        return ''.join(out)

    def alloc(self, size, zero=True):
        size = max(int(size), 1)
        addr = (self.heap_next + 15) & ~15
        if addr + size > HEAP_BASE + HEAP_SIZE:
            raise EmuError('에뮬레이터 힙 부족')
        self.heap_next = addr + size
        self.heap_sizes[addr] = size
        if zero:
            self.uc.mem_write(addr, b'\0' * size)
        return addr

    def new_handle(self, obj):
        h = self.next_handle
        self.next_handle += 4
        self.handles[h] = obj
        return h

    # ---- PE 로드
    def _load(self):
        pe = self.pe
        self.base = pe.OPTIONAL_HEADER.ImageBase
        size = (pe.OPTIONAL_HEADER.SizeOfImage + 0xFFF) & ~0xFFF
        uc = self.uc
        uc.mem_map(self.base, size)
        uc.mem_write(self.base, pe.get_memory_mapped_image(ImageBase=self.base))
        uc.mem_map(STUB_BASE, STUB_SIZE)
        uc.mem_write(STUB_BASE, b'\xCC' * STUB_SIZE)
        uc.mem_map(HEAP_BASE, HEAP_SIZE)
        uc.mem_map(STACKS_BASE, STACK_SIZE * 4)
        uc.mem_map(TEB_BASE, 0x10000)
        uc.mem_map(MISC_BASE, 0x10000)
        uc.mem_map(GDT_ADDR, 0x1000)
        self._gdt_set(1, 0, 0xFFFFF, 0xF2)    # data, present, ring3, RW
        self._gdt_set(2, 0, 0xFFFFF, 0x92)    # data, present, ring0, RW (SS)
        uc.reg_write(UC_X86_REG_GDTR, (0, GDT_ADDR, 0x1000 - 1, 0x0))
        uc.reg_write(UC_X86_REG_SS, (2 << 3) | 0)
        for r in (UC_X86_REG_DS, UC_X86_REG_ES, UC_X86_REG_GS):
            uc.reg_write(r, (1 << 3) | 3)

        # import 해석
        apis = api_table()
        slot = 0
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            for imp in entry.imports:
                name = imp.name.decode()
                addr = STUB_BASE + slot * 16
                slot += 1
                nargs, fn = apis.get(name, (None, None))
                self.stubs[addr] = (name, nargs, fn)
                self.w32(imp.address, addr)
        self.stubs[RET_SENTINEL] = ('<return>', 0, None)
        self.stubs[THREAD_EXIT] = ('<thread-exit>', 0, None)
        self.stubs[PROGRESS_CB] = ('<progress>', 0, None)

        # 공용 문자열
        self.cmdline_a = MISC_BASE
        self.uc.mem_write(self.cmdline_a, b'gscupgrade.exe\0')
        self.cmdline_w = MISC_BASE + 0x100
        self.uc.mem_write(self.cmdline_w, 'gscupgrade.exe\0'.encode('utf-16le'))
        self.env_w = MISC_BASE + 0x200
        self.uc.mem_write(self.env_w, 'A=B\0\0'.encode('utf-16le'))

        uc.hook_add(UC_HOOK_CODE, self._on_stub, begin=STUB_BASE, end=STUB_BASE + STUB_SIZE - 1)
        uc.hook_add(UC_HOOK_MEM_INVALID, self._on_bad_mem)

        # 메인 스레드
        main = self._new_thread()
        self.cur = main
        uc.reg_write(UC_X86_REG_FS, main.fs_sel)
        self.main = main

        # export 표
        self.exports = {e.name.decode(): self.base + e.address
                        for e in pe.DIRECTORY_ENTRY_EXPORT.symbols if e.name}

        # 리소스 표: (type, name) -> (rva, size)
        self.resources = {}
        rsrc = pe.DIRECTORY_ENTRY_RESOURCE.entries if hasattr(pe, 'DIRECTORY_ENTRY_RESOURCE') else []
        for t in rsrc:
            tkey = str(t.name) if t.name else t.id
            for n in t.directory.entries:
                nkey = str(n.name) if n.name else n.id
                for lang in n.directory.entries:
                    self.resources[(tkey, nkey)] = (lang.data.struct.OffsetToData, lang.data.struct.Size)

    def _gdt_set(self, idx, base, limit, access):
        flags = 0xC  # 4KB 단위, 32비트
        d = (limit & 0xFFFF) | ((base & 0xFFFFFF) << 16) | (access << 40) \
            | (((limit >> 16) & 0xF) << 48) | (flags << 52) | (((base >> 24) & 0xFF) << 56)
        self.uc.mem_write(GDT_ADDR + idx * 8, struct.pack('<Q', d))

    def _new_thread(self):
        idx = len(self.threads)
        if idx >= 4:
            raise EmuError('스레드가 너무 많음')
        teb = TEB_BASE + idx * 0x1000
        stack_top = STACKS_BASE + (idx + 1) * STACK_SIZE - 0x100
        self.w32(teb + 0x00, 0xFFFFFFFF)                  # ExceptionList
        self.w32(teb + 0x04, stack_top)                   # StackBase
        self.w32(teb + 0x08, stack_top - STACK_SIZE + 0x200)  # StackLimit
        self.w32(teb + 0x18, teb)                         # Self
        self.w32(teb + 0x20, 1)                           # ClientId.Process
        self.w32(teb + 0x24, 0x1000 + idx * 4)            # ClientId.Thread
        self.w32(teb + 0x30, PEB_ADDR)
        th = Thread(0x1000 + idx * 4, teb)
        th.stack_top = stack_top
        self._gdt_set(3 + idx, teb, 0xF, 0xF2)   # limit 0xF * 4KB
        th.fs_sel = ((3 + idx) << 3) | 3
        self.threads.append(th)
        return th

    # ---- 실행
    def call(self, addr, *args, timeout_s=600):
        """cdecl 함수 호출 (메인 스레드). 인자는 int."""
        if self.cur is not self.main:
            raise EmuError('메인 스레드가 아님')
        uc = self.uc
        esp = self.main.stack_top
        for a in reversed(args):
            esp -= 4
            self.w32(esp, a)
        esp -= 4
        self.w32(esp, RET_SENTINEL)
        uc.reg_write(UC_X86_REG_ESP, esp)
        uc.reg_write(UC_X86_REG_EBP, 0)
        self._fault = None
        try:
            uc.emu_start(addr, RET_SENTINEL, timeout=int(timeout_s * 1e6))
        except UcError as e:
            eip = uc.reg_read(UC_X86_REG_EIP)
            raise EmuError(f'에뮬레이션 오류 {e} @ {eip:#x} (thread {self.cur.tid:#x})') from None
        if self._fault:
            raise EmuError(self._fault)
        if uc.reg_read(UC_X86_REG_EIP) != RET_SENTINEL or self.cur is not self.main:
            raise EmuError(f'함수가 정상 복귀하지 않음 (eip={uc.reg_read(UC_X86_REG_EIP):#x}, 시간 초과?)')
        return uc.reg_read(UC_X86_REG_EAX)

    def export(self, name, *args, **kw):
        if name not in self.exports:
            raise EmuError(f'export 없음: {name}')
        return self.call(self.exports[name], *args, **kw)

    def run_dllmain(self):
        entry = self.base + self.pe.OPTIONAL_HEADER.AddressOfEntryPoint
        ret = self.call_stdcall(entry, self.base, 1, 0)
        if ret == 0:
            raise EmuError('DllMain(PROCESS_ATTACH) 실패')

    def call_stdcall(self, addr, *args):
        # stdcall은 피호출자가 스택을 정리하지만 복귀 후 ESP를 다시 맞추므로 cdecl과 동일하게 호출 가능
        return self.call(addr, *args)

    def _on_bad_mem(self, uc, access, addr, size, value, _):
        eip = uc.reg_read(UC_X86_REG_EIP)
        self._fault = f'잘못된 메모리 접근 addr={addr:#x} size={size} eip={eip:#x}'
        log(self._fault)
        return False

    def _on_stub(self, uc, addr, size, _):
        if addr not in self.stubs:
            return
        name, nargs, fn = self.stubs[addr]
        if name == '<return>':
            return  # emu_start until 주소
        esp = uc.reg_read(UC_X86_REG_ESP)
        ret_addr = self.r32(esp)
        if name == '<thread-exit>':
            self.cur.done = True
            self._switch(blocked=False)
            return
        if name == '<progress>':
            p = self.r32(esp + 4)
            emit(event='progress', progress=p if p < 0x80000000 else p - 0x100000000)
            if self.progress_cb:
                self.progress_cb(p)
            uc.reg_write(UC_X86_REG_ESP, esp + 4)     # cdecl: 호출자가 인자 정리
            uc.reg_write(UC_X86_REG_EIP, ret_addr)
            return
        if fn is None:
            self._fault = f'구현되지 않은 API: {name}'
            log(self._fault)
            uc.emu_stop()
            return
        args = [self.r32(esp + 4 + 4 * i) for i in range(nargs)]
        try:
            result = fn(self, *args)
        except _Block as b:
            # 현재 스레드는 대기: API가 반환된 상태로 컨텍스트를 만들어 두고 전환
            uc.reg_write(UC_X86_REG_ESP, esp + 4 + 4 * nargs)
            uc.reg_write(UC_X86_REG_EIP, ret_addr)
            uc.reg_write(UC_X86_REG_EAX, WAIT_OBJECT_0)
            self.cur.waiting = b.handle
            self._switch(blocked=True)
            return
        except EmuError as e:
            self._fault = f'{name}: {e}'
            log(self._fault)
            uc.emu_stop()
            return
        if result is None:
            result = 0
        uc.reg_write(UC_X86_REG_EAX, result & 0xFFFFFFFF)
        uc.reg_write(UC_X86_REG_ESP, esp + 4 + 4 * nargs)
        uc.reg_write(UC_X86_REG_EIP, ret_addr)

    def _runnable(self, th):
        if th.done:
            return False
        if th.waiting is None:
            return True
        ev = self.events.get(th.waiting)
        return ev is not None and ev['signaled']

    def _switch(self, blocked):
        """현재 스레드를 멈추고 실행 가능한 다른 스레드로 넘어간다."""
        uc = self.uc
        cur = self.cur
        if not cur.done:
            cur.ctx = uc.context_save()
        order = self.threads[self.threads.index(cur) + 1:] + self.threads[:self.threads.index(cur) + 1]
        for th in order:
            if self._runnable(th) and th.ctx is not None:
                if th.waiting is not None:
                    ev = self.events[th.waiting]
                    if not ev['manual']:
                        ev['signaled'] = False
                    th.waiting = None
                self.cur = th
                uc.context_restore(th.ctx)
                uc.reg_write(UC_X86_REG_EIP, uc.reg_read(UC_X86_REG_EIP))  # PC 변경을 확정
                return
        self._fault = '교착 상태: 실행 가능한 스레드가 없음'
        log(self._fault)
        uc.emu_stop()

    def start_thread(self, start, param):
        th = self._new_thread()
        esp = th.stack_top
        esp -= 4
        self.w32(esp, param)
        esp -= 4
        self.w32(esp, THREAD_EXIT)
        # 현재 컨텍스트를 바탕으로 새 스레드 컨텍스트를 만든다
        uc = self.uc
        saved = uc.context_save()
        uc.reg_write(UC_X86_REG_ESP, esp)
        uc.reg_write(UC_X86_REG_EBP, 0)
        uc.reg_write(UC_X86_REG_EIP, start)
        uc.reg_write(UC_X86_REG_FS, th.fs_sel)
        th.ctx = uc.context_save()
        uc.context_restore(saved)
        return th

    # ---- 고수준 래퍼 (app의 udiskUpgradWoker.js 와 동일한 호출 순서)
    def buf(self, data_or_size):
        if isinstance(data_or_size, int):
            return self.alloc(data_or_size)
        a = self.alloc(len(data_or_size), zero=False)
        self.uc.mem_write(a, bytes(data_or_size))
        return a


class _Block(Exception):
    def __init__(self, handle):
        self.handle = handle


# ---------------------------------------------------------------- API 구현
def api_table():
    T = {}

    def api(nargs, name=None):
        def deco(fn):
            T[name or fn.__name__] = (nargs, fn)
            return fn
        return deco

    # -- 오류/스레드 정보
    @api(0)
    def GetLastError(e):
        return e.cur.last_error

    @api(1)
    def SetLastError(e, v):
        e.cur.last_error = v

    @api(0)
    def GetCurrentThreadId(e):
        return e.cur.tid

    @api(0)
    def GetCurrentProcessId(e):
        return 1

    @api(0)
    def GetCurrentProcess(e):
        return 0xFFFFFFFF

    @api(0)
    def IsDebuggerPresent(e):
        return 0

    @api(1)
    def IsProcessorFeaturePresent(e, f):
        return 1 if f in (6, 10) else 0   # PF_XMMI_INSTRUCTIONS_AVAILABLE, PF_XMMI64

    @api(1)
    def QueryPerformanceCounter(e, p):
        e.uc.mem_write(p, struct.pack('<Q', time.perf_counter_ns() // 100))
        return 1

    @api(1)
    def GetSystemTimeAsFileTime(e, p):
        ft = int((time.time() + 11644473600) * 1e7)
        e.uc.mem_write(p, struct.pack('<Q', ft))

    @api(1)
    def GetLocalTime(e, p):
        t = time.localtime()
        ms = int((time.time() % 1) * 1000)
        e.uc.mem_write(p, struct.pack('<8H', t.tm_year, t.tm_mon, (t.tm_wday + 1) % 7, t.tm_mday,
                                      t.tm_hour, t.tm_min, t.tm_sec, ms))

    @api(1)
    def GetStartupInfoW(e, p):
        e.uc.mem_write(p, struct.pack('<I', 68) + b'\0' * 64)

    @api(1)
    def Sleep(e, ms):
        time.sleep(min(ms, 10000) / 1000)

    # -- 포인터 인코딩
    @api(1)
    def EncodePointer(e, p):
        return p

    @api(1)
    def DecodePointer(e, p):
        return p

    # -- 예외 처리 (정상 경로에서는 쓰이지 않아야 함)
    @api(1)
    def SetUnhandledExceptionFilter(e, p):
        return 0

    @api(1)
    def UnhandledExceptionFilter(e, p):
        raise EmuError('처리되지 않은 예외')

    @api(4)
    def RaiseException(e, code, flags, nargs, args):
        raise EmuError(f'RaiseException code={code:#x} (C++ 예외 등) — 정상 경로가 아님')

    @api(4)
    def RtlUnwind(e, *a):
        raise EmuError('RtlUnwind 호출 — 정상 경로가 아님')

    @api(2)
    def TerminateProcess(e, h, code):
        raise EmuError(f'TerminateProcess({code})')

    @api(1)
    def ExitProcess(e, code):
        raise EmuError(f'ExitProcess({code})')

    # -- 모듈
    @api(1)
    def GetModuleHandleW(e, name):
        n = e.wstr(name)
        if n is None:
            return e.base
        return 0x7C800000 if n.lower().startswith('kernel32') else 0

    @api(3)
    def GetModuleHandleExW(e, flags, name, out):
        if out:
            e.w32(out, e.base)
        return 1

    @api(2)
    def GetProcAddress(e, mod, name):
        return 0   # CRT는 없을 때의 대체 경로를 쓴다

    @api(3)
    def LoadLibraryExW(e, name, h, flags):
        return 0

    @api(1)
    def FreeLibrary(e, h):
        return 1

    def _modname(e, buf, size, wide):
        path = MODULE_PATH[:max(size - 1, 0)]
        if wide:
            e.uc.mem_write(buf, (path + '\0').encode('utf-16le'))
        else:
            e.uc.mem_write(buf, (path + '\0').encode('latin-1'))
        return len(path)

    @api(3)
    def GetModuleFileNameA(e, h, buf, size):
        return _modname(e, buf, size, False)

    @api(3)
    def GetModuleFileNameW(e, h, buf, size):
        return _modname(e, buf, size, True)

    # -- 리소스 (칩별 LOADER)
    @api(3)
    def FindResourceA(e, mod, name, typ):
        def key(v):
            return v if v < 0x10000 else e.cstr(v)
        k = (key(typ), key(name))
        if k not in e.resources:
            log(f'FindResourceA: 없음 {k}')
            return 0
        return e.new_handle(('res',) + e.resources[k])

    @api(2)
    def LoadResource(e, mod, h):
        return h

    @api(1)
    def LockResource(e, h):
        obj = e.handles.get(h)
        return e.base + obj[1] if obj else 0

    @api(2)
    def SizeofResource(e, mod, h):
        obj = e.handles.get(h)
        return obj[2] if obj else 0

    @api(1)
    def GlobalUnlock(e, h):
        return 0

    # -- 힙
    @api(0)
    def GetProcessHeap(e):
        return 0x00500000

    @api(3)
    def HeapAlloc(e, heap, flags, size):
        return e.alloc(size)

    @api(3)
    def HeapFree(e, heap, flags, p):
        return 1

    @api(3)
    def HeapSize(e, heap, flags, p):
        return e.heap_sizes.get(p, 0xFFFFFFFF)

    @api(4)
    def HeapReAlloc(e, heap, flags, p, size):
        old = e.heap_sizes.get(p, 0)
        n = e.alloc(size)
        if p and old:
            e.uc.mem_write(n, e.rbytes(p, min(old, size)))
        return n

    # -- TLS
    @api(0)
    def TlsAlloc(e):
        i = e.tls_next
        e.tls_next += 1
        return i

    @api(1)
    def TlsFree(e, i):
        return 1

    @api(1)
    def TlsGetValue(e, i):
        e.cur.last_error = 0
        return e.cur.tls.get(i, 0)

    @api(2)
    def TlsSetValue(e, i, v):
        e.cur.tls[i] = v
        return 1

    # -- 동기화
    @api(2)
    def InitializeCriticalSectionAndSpinCount(e, cs, spin):
        return 1

    @api(1)
    def EnterCriticalSection(e, cs):
        pass

    @api(1)
    def LeaveCriticalSection(e, cs):
        pass

    @api(1)
    def DeleteCriticalSection(e, cs):
        pass

    @api(1)
    def InitializeSListHead(e, p):
        e.uc.mem_write(p, b'\0' * 8)

    @api(1)
    def InterlockedFlushSList(e, p):
        first = e.r32(p)
        e.uc.mem_write(p, b'\0' * 8)
        return first

    def _event(e, manual, initial):
        h = e.new_handle(('event',))
        e.events[h] = {'manual': bool(manual), 'signaled': bool(initial)}
        return h

    @api(4)
    def CreateEventA(e, sa, manual, initial, name):
        return _event(e, manual, initial)

    @api(4)
    def CreateEventW(e, sa, manual, initial, name):
        return _event(e, manual, initial)

    @api(3)
    def CreateMutexW(e, sa, owner, name):
        return _event(e, False, True)   # 협조적 스케줄링이므로 항상 획득 가능

    @api(1)
    def ReleaseMutex(e, h):
        ev = e.events.get(h)
        if ev is not None:
            ev['signaled'] = True
        return 1

    @api(1)
    def SetEvent(e, h):
        ev = e.events.get(h)
        if ev is None:
            raise EmuError(f'SetEvent: 알 수 없는 핸들 {h:#x}')
        ev['signaled'] = True
        return 1

    @api(1)
    def ResetEvent(e, h):
        ev = e.events.get(h)
        if ev is None:
            raise EmuError(f'ResetEvent: 알 수 없는 핸들 {h:#x}')
        ev['signaled'] = False
        return 1

    def _wait(e, h, timeout):
        ev = e.events.get(h)
        if ev is None:
            obj = e.handles.get(h)
            if obj and obj[0] == 'thread':
                th = obj[1]
                if th.done:
                    return WAIT_OBJECT_0
                if timeout == 0:
                    return WAIT_TIMEOUT
            raise EmuError(f'WaitForSingleObject: 지원하지 않는 핸들 {h:#x}')
        if ev['signaled']:
            if not ev['manual']:
                ev['signaled'] = False
            return WAIT_OBJECT_0
        if timeout == 0:
            return WAIT_TIMEOUT
        raise _Block(h)

    @api(2)
    def WaitForSingleObject(e, h, timeout):
        return _wait(e, h, timeout)

    @api(3)
    def WaitForSingleObjectEx(e, h, timeout, alertable):
        return _wait(e, h, timeout)

    @api(6)
    def CreateThread(e, sa, stack, start, param, flags, tid_out):
        th = e.start_thread(start, param)
        if tid_out:
            e.w32(tid_out, th.tid)
        return e.new_handle(('thread', th))

    @api(2)
    def TerminateThread(e, h, code):
        obj = e.handles.get(h)
        if obj and obj[0] == 'thread':
            obj[1].done = True
        return 1

    # -- 파일/장치
    def _open(e, path, access):
        log(f'CreateFile({path!r})')
        p = path.replace('/', '\\')
        if p.upper().rstrip('\\').endswith(f'{e.letter}:') and e.device is not None:
            return e.new_handle(('device',))
        if p.lower() == MODULE_PATH.lower():
            # DLL 자기 무결성 검사용: 원본 파일을 읽기 전용으로 연결
            if access & 0x40000000:   # GENERIC_WRITE
                e.cur.last_error = 5
                return INVALID_HANDLE
            return e.new_handle(('file', open(e.dll_path, 'rb')))
        e.cur.last_error = 2   # ERROR_FILE_NOT_FOUND
        return INVALID_HANDLE

    @api(7)
    def CreateFileA(e, name, access, share, sa, disp, flags, tmpl):
        return _open(e, e.cstr(name), access)

    @api(7)
    def CreateFileW(e, name, access, share, sa, disp, flags, tmpl):
        return _open(e, e.wstr(name), access)

    @api(1)
    def CloseHandle(e, h):
        obj = e.handles.pop(h, None)
        if obj and obj[0] == 'file':
            obj[1].close()
        e.events.pop(h, None)
        return 1

    @api(8)
    def DeviceIoControl(e, h, code, inb, insz, outb, outsz, ret, ov):
        obj = e.handles.get(h)
        if not obj or obj[0] != 'device':
            e.cur.last_error = 6   # ERROR_INVALID_HANDLE
            return 0
        if code == IOCTL_SCSI_PASS_THROUGH_DIRECT:
            return _sptd(e, inb, insz, outb, outsz, ret)
        if code == IOCTL_STORAGE_QUERY_PROPERTY:
            return _query_property(e, inb, insz, outb, outsz, ret)
        raise EmuError(f'DeviceIoControl: 지원하지 않는 코드 {code:#x}')

    def _sptd(e, inb, insz, outb, outsz, ret):
        s = e.rbytes(inb, 0x2C)
        cdb_len, sense_len, data_in = s[6], s[7], s[8]
        xfer_len, timeout_s, data_ptr, sense_off = struct.unpack_from('<IIII', s, 12)
        if not 1 <= cdb_len <= 16:
            raise EmuError(f'SPTD: 비정상 CdbLength {cdb_len}')
        cdb = s[28:28 + cdb_len]
        if data_in == 1:
            direction = SG_DXFER_FROM_DEV
        elif data_in == 0:
            direction = SG_DXFER_TO_DEV
        else:
            direction = SG_DXFER_NONE if xfer_len == 0 else None
        if direction is None:
            raise EmuError(f'SPTD: DataIn={data_in} 미지원')
        out = e.rbytes(data_ptr, xfer_len) if direction == SG_DXFER_TO_DEV else b''
        ok, status, din, sense, got = e.device.command(cdb, direction, xfer_len, out, timeout_s * 1000)
        if not ok:
            e.cur.last_error = 0x1F   # ERROR_GEN_FAILURE
            return 0
        if din:
            e.uc.mem_write(data_ptr, din[:got])
        res = bytearray(e.rbytes(outb, min(outsz, 0x50))) if outb else None
        if res is not None:
            res[2] = status
            struct.pack_into('<I', res, 12, got)
            if sense and sense_off + sense_len <= len(res):
                n = min(len(sense), sense_len)
                res[sense_off:sense_off + n] = sense[:n]
                res[7] = n
            else:
                res[7] = 0
            e.uc.mem_write(outb, bytes(res))
        if ret:
            e.w32(ret, 0x50 if outb else 0)
        return 1

    def _query_property(e, inb, insz, outb, outsz, ret):
        prop_id, query_type = struct.unpack('<II', e.rbytes(inb, 8))
        if prop_id != 0 or query_type != 0:
            raise EmuError(f'STORAGE_QUERY_PROPERTY: 미지원 ({prop_id},{query_type})')
        vendor, product, rev = e.device.inquiry()
        strings = b''
        offs = []
        base = 36 + 4
        for s in (vendor, product, rev):
            offs.append(base + len(strings))
            strings += s + b'\0'
        total = base + len(strings)
        desc = struct.pack('<IIBBBBIIIIII', total, total, 0, 0, 1, 0, offs[0], offs[1], offs[2], 0, 7, 0)
        desc = desc.ljust(base, b'\0') + strings
        n = min(outsz, len(desc))
        e.uc.mem_write(outb, desc[:n])
        if ret:
            e.w32(ret, n)
        return 1

    @api(1)
    def GetStdHandle(e, n):
        return {0xFFFFFFF6: 0x10, 0xFFFFFFF5: 0x11, 0xFFFFFFF4: 0x12}.get(n, INVALID_HANDLE)

    @api(2)
    def SetStdHandle(e, n, h):
        return 1

    @api(1)
    def GetFileType(e, h):
        obj = e.handles.get(h)
        if obj and obj[0] == 'file':
            return 1   # FILE_TYPE_DISK
        return 2 if h in (0x10, 0x11, 0x12) else 0   # FILE_TYPE_CHAR

    @api(5)
    def WriteFile(e, h, buf, n, written, ov):
        if h in (0x11, 0x12):
            log('dll:', e.rbytes(buf, n).decode('latin-1', 'replace').rstrip())
            if written:
                e.w32(written, n)
            return 1
        e.cur.last_error = 6
        return 0

    @api(5)
    def WriteConsoleW(e, h, buf, n, written, res):
        log('dll:', e.rbytes(buf, 2 * n).decode('utf-16le', 'replace').rstrip())
        if written:
            e.w32(written, n)
        return 1

    @api(5)
    def ReadFile(e, h, buf, n, read, ov):
        obj = e.handles.get(h)
        data = obj[1].read(n) if obj and obj[0] == 'file' else b''
        if data:
            e.uc.mem_write(buf, data)
        if read:
            e.w32(read, len(data))
        if not (obj and obj[0] == 'file'):
            e.cur.last_error = 6
            return 0
        return 1

    @api(5)
    def ReadConsoleW(e, *a):
        return 0

    @api(2)
    def GetConsoleMode(e, h, p):
        return 0

    @api(0)
    def GetConsoleCP(e):
        return 65001

    @api(1)
    def FlushFileBuffers(e, h):
        return 1

    @api(5)
    def SetFilePointerEx(e, h, lo, hi, newpos, method):
        obj = e.handles.get(h)
        if not (obj and obj[0] == 'file'):
            e.cur.last_error = 6
            return 0
        dist = struct.unpack('<q', struct.pack('<II', lo, hi))[0]
        pos = obj[1].seek(dist, method)
        if newpos:
            e.uc.mem_write(newpos, struct.pack('<q', pos))
        return 1

    @api(1)
    def SetEndOfFile(e, h):
        return 0

    # -- 환경/로캘 (CRT 초기화용)
    @api(0)
    def GetCommandLineA(e):
        return e.cmdline_a

    @api(0)
    def GetCommandLineW(e):
        return e.cmdline_w

    @api(0)
    def GetEnvironmentStringsW(e):
        return e.env_w

    @api(1)
    def FreeEnvironmentStringsW(e, p):
        return 1

    @api(0)
    def GetACP(e):
        return 1252

    @api(0)
    def GetOEMCP(e):
        return 437

    @api(1)
    def IsValidCodePage(e, cp):
        return 1

    @api(2)
    def GetCPInfo(e, cp, p):
        e.uc.mem_write(p, struct.pack('<I', 1) + b'?\0' + b'\0' * 12)
        return 1

    @api(6)
    def MultiByteToWideChar(e, cp, flags, src, n, dst, dn):
        data = e.rbytes(src, n) if n != 0xFFFFFFFF else (e.cstr(src).encode('latin-1') + b'\0')
        text = data.decode('utf-8' if cp == 65001 else 'latin-1', 'replace')
        if dn == 0:
            return len(text)
        w = text[:dn].encode('utf-16le')
        e.uc.mem_write(dst, w)
        return len(w) // 2

    @api(8)
    def WideCharToMultiByte(e, cp, flags, src, n, dst, dn, defc, used):
        data = e.rbytes(src, 2 * n) if n != 0xFFFFFFFF else (e.wstr(src) + '\0').encode('utf-16le')
        text = data.decode('utf-16le', 'replace')
        out = text.encode('utf-8' if cp == 65001 else 'latin-1', 'replace')
        if used:
            e.w32(used, 0)
        if dn == 0:
            return len(out)
        out = out[:dn]
        e.uc.mem_write(dst, out)
        return len(out)

    @api(4)
    def GetStringTypeW(e, info, src, n, out):
        text = e.rbytes(src, 2 * n).decode('utf-16le', 'replace') if n != 0xFFFFFFFF else e.wstr(src) + '\0'
        vals = []
        for ch in text:
            v = 0
            if ch.isupper():
                v |= 0x1
            if ch.islower():
                v |= 0x2
            if ch.isdigit():
                v |= 0x4
            if ch.isspace():
                v |= 0x8
            if not ch.isalnum() and ch.isprintable() and not ch.isspace():
                v |= 0x10
            if ord(ch) < 32 or ord(ch) == 127:
                v |= 0x20
            if ch == ' ' or ch == '\t':
                v |= 0x40
            if ch.isalpha():
                v |= 0x100
            vals.append(v)
        e.uc.mem_write(out, struct.pack(f'<{len(vals)}H', *vals))
        return 1

    @api(6)
    def LCMapStringW(e, loc, flags, src, n, dst, dn):
        text = e.rbytes(src, 2 * n).decode('utf-16le', 'replace') if n != 0xFFFFFFFF else e.wstr(src) + '\0'
        if flags & 0x100:
            text = text.lower()
        elif flags & 0x200:
            text = text.upper()
        if dn == 0:
            return len(text)
        e.uc.mem_write(dst, text[:dn].encode('utf-16le'))
        return min(len(text), dn)

    # -- 난수 (CRT 쿠키 등)
    @api(2, 'SystemFunction036')
    def RtlGenRandom(e, p, n):
        e.uc.mem_write(p, os.urandom(n))
        return 1

    @api(5)
    def CryptAcquireContextW(e, out, *a):
        e.w32(out, 0x1234)
        return 1

    @api(3)
    def CryptGenRandom(e, prov, n, p):
        e.uc.mem_write(p, os.urandom(n))
        return 1

    @api(2)
    def CryptReleaseContext(e, prov, flags):
        return 1

    return T


# ---------------------------------------------------------------- 명령
def load_ufw(emu, path):
    data = open(path, 'rb').read()
    p = emu.buf(data)
    ret = s32(emu.export('JL_loadUfwData', p, len(data)))
    if ret != 0:
        return ret, None
    pid = emu.buf(16)
    vid = emu.buf(4)
    ret = s32(emu.export('JL_getUfwPidVid', pid, 16, vid, 4))
    if ret != 0:
        return ret, None
    c16 = emu.buf(4)
    c32 = emu.buf(4)
    ret = s32(emu.export('JL_getUfwCrc', c16, c32))
    if ret != 0:
        return ret, None
    info = {
        'pid': emu.rbytes(pid, 16).split(b'\0')[0].decode('latin-1').strip(),
        'vid': emu.rbytes(vid, 4).split(b'\0')[0].decode('latin-1').strip(),
        'crc': f'{struct.unpack("<H", emu.rbytes(c16, 2))[0]:x}-{emu.r32(c32):x}',
    }
    return 0, info


def query_device(emu):
    desc = emu.buf(64)
    dret = s32(emu.export('JL_queryDeviceDesc', ord(emu.letter), desc, 64))
    pid = emu.buf(16)
    vid = emu.buf(4)
    ret = s32(emu.export('JL_queryDevicePidVid', ord(emu.letter), pid, 16, vid, 4))
    return {
        'ret': ret,
        'descRet': dret,
        'deviceDesc': emu.rbytes(desc, 64).split(b'\0')[0].decode('latin-1') if dret == 0 else None,
        'pid': emu.rbytes(pid, 16).split(b'\0')[0].decode('latin-1').strip() if ret == 0 else None,
        'vid': emu.rbytes(vid, 4).split(b'\0')[0].decode('latin-1').strip() if ret == 0 else None,
    }


def s32(v):
    return v - 0x100000000 if v & 0x80000000 else v


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dll', required=True)
    ap.add_argument('--device', help='/dev/sgN (JieLi UBOOT 장치)')
    ap.add_argument('--trace', action='store_true', help='SCSI 명령 로그')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('ufwinfo', help='UFW 파일 정보 (장치 불필요)')
    s.add_argument('ufw')
    s = sub.add_parser('query', help='장치 식별 (읽기 전용)')
    s.add_argument('--ufw', help='먼저 불러올 UFW (칩 KEY 확인용, 앱의 공통 UFW)')
    s = sub.add_parser('upgrade', help='펌웨어 업그레이드 (장치에 씀!)')
    s.add_argument('ufw')
    s.add_argument('--no-pid-check', action='store_true')
    s.add_argument('--dual-backup', action='store_true')
    s.add_argument('--readonly', action='store_true', help='DATA_OUT 명령을 보내지 않는 리허설')
    a = ap.parse_args()

    dev = None
    if a.cmd in ('query', 'upgrade'):
        if not a.device:
            ap.error('--device 필요')
        readonly = a.cmd == 'query' or a.readonly
        dev = ScsiDevice(a.device, readonly=readonly, trace=a.trace)

    try:
        emu = Emulator(a.dll, device=dev)
        emu.run_dllmain()
        if a.cmd == 'ufwinfo':
            ret, info = load_ufw(emu, a.ufw)
            emit(event='ufwinfo', ret=ret, **(info or {}))
        elif a.cmd == 'query':
            if a.ufw:
                ret, info = load_ufw(emu, a.ufw)
                emit(event='ufwinfo', ret=ret, **(info or {}))
            emit(event='query', **query_device(emu))
        elif a.cmd == 'upgrade':
            ret, info = load_ufw(emu, a.ufw)
            emit(event='ufwinfo', ret=ret, **(info or {}))
            if ret != 0:
                emit(event='result', ret=ret, message='UFW load failed')
                return 1
            q = query_device(emu)
            emit(event='query', **q)
            if q['ret'] != 0:
                emit(event='result', ret=q['ret'] if q['ret'] < 0 else -9, message='Device identity query failed')
                return 1
            if not a.no_pid_check and q['pid'] != info['pid']:
                emit(event='result', ret=-23, message=f'UFW PID {info["pid"]!r} != device PID {q["pid"]!r}')
                return 1
            emu.export('JL_setDualBackupBurning', 1 if a.dual_backup else 0)
            emit(event='progress', progress=0)
            ret = s32(emu.export('JL_upgradeDevice', ord(emu.letter), 0, 1, PROGRESS_CB, timeout_s=1800))
            if ret == 0:
                emit(event='progress', progress=100)
            emit(event='result', ret=ret, blocked=dev.blocked)
            return 0 if ret == 0 else 1
    except EmuError as e:
        emit(event='result', ret=-99, message=str(e))
        return 2
    finally:
        if dev:
            dev.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
