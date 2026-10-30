# -*- coding: utf-8 -*-
"""lib_session.py —— 可靠的 Session 获取（复用已加载的 BYOVD 驱动）

================================================================================
★ 研究用工具 · 只对你【自己拥有】的硬件使用
★ 本文件只负责"拿句柄 + 找卡"，本身不写任何寄存器；
  但返回的 Session 对象带 wr()/pwr()，写之前请读 tools/README.md
★ 所有寄存器写入都是易失的；掉卡请 AC 断电（拔电源 30 秒）
================================================================================

问题（反复困扰历史实验）：
    WinRing0 的设备名 \\\\.\\WinRing0_1_2_0 是【写死在驱动里】的。
    有别的进程（例如常驻按住进程）占着同一个驱动时，
    再 create 一个同名服务会报 183 "设备已存在" → 实验全部失败。

解决：FastSession 先直接 CreateFileW 复用已加载驱动的设备句柄，
      只有在设备确实不存在时才用 sc.exe 装驱动。

    设备名约定：
      WinRing0 系：\\\\.\\WinRing0_1_2_0     （服务名 P104Gen2x4Wr / P104LibWr ...）
      ThrottleStop：\\\\.\\<服务名>          （服务名 P104Gen2x4Ts / P104LibTs ...）

用法：
    from lib_session import open_session
    s = open_session()                    # 只读用：不探测写通道
    s = open_session(detect_write=True)   # 要写寄存器时：先探测写 IOCTL
    from lib_session import get_session   # 旧接口，仍然可用
    s = get_session()
"""
import ctypes as C
import ctypes.wintypes as W
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import lib_p104 as L

IOCTL_MAP_PHYS = 0x8000645C
GENERIC_RW = 0xC0000000
OPEN_EXISTING = 3
INVALID = C.c_void_p(-1).value
BAD = (None, INVALID)


def _open(name):
    k = C.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = C.c_void_p
    k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, C.c_void_p]
    h = k.CreateFileW(name, GENERIC_RW, 0, None, OPEN_EXISTING, 0, None)
    return None if h in (None, INVALID) else h


def _map_ok(h):
    """验证映射驱动真的能用：映射物理 0 页，看 VA 是否非零"""
    if h is None:
        return False
    try:
        k = C.WinDLL("kernel32", use_last_error=True)
        k.DeviceIoControl.restype = W.BOOL
        k.DeviceIoControl.argtypes = [C.c_void_p, W.DWORD, C.c_void_p, W.DWORD,
                                      C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
        ib = struct.pack("<QI", 0, 0x1000)
        o = C.create_string_buffer(8)
        n = W.DWORD(0)
        if not k.DeviceIoControl(h, IOCTL_MAP_PHYS, ib, 12, o, 8, C.byref(n), None):
            return False
        return struct.unpack("<Q", o.raw[:8])[0] != 0
    except Exception:
        return False


def _pci_ok(s):
    """验证 PCI 读能用：扫一遍找任意真实设备"""
    for bus in range(0, 4):
        for dev in range(0, 32):
            v = s.prd(bus, dev, 0, 0x00)
            if v is not None and v not in (0xFFFFFFFF, 0):
                return True
    return False


def _find_gpu(s, vendor=0x10DE, device=0x1B87, bus_max=64):
    """枚举 vendor:device（默认 10DE:1B87 = P104-100），并填好 loc/bar/cap/root。
    ★ 不依赖 lib_p104.find()（那个只扫 fn0 且缓存状态）。"""
    for bus in range(0, bus_max):
        for dev in range(0, 32):
            id0 = s.prd(bus, dev, 0, 0x00)
            if id0 is None or id0 == 0xFFFFFFFF:
                continue
            if (id0 & 0xFFFF) != vendor:
                continue
            for fn in range(0, 8):
                i2 = s.prd(bus, dev, fn, 0x00)
                if i2 is None or i2 == 0xFFFFFFFF:
                    continue
                if (i2 & 0xFFFF) == vendor and ((i2 >> 16) & 0xFFFF) == device:
                    s.loc = (bus, dev, fn)
                    bar = s.prd(bus, dev, fn, 0x10)
                    if bar is None:
                        return False
                    s.bar = bar & 0xFFFFFFF0
                    s.cap = s.pcie_cap(bus, dev, fn)
                    s.root = s.find_root_port(bus, dev, fn)
                    if s.root:
                        s.root_cap = s.pcie_cap(*s.root)
                    return True
    return False


class FastSession(L.Session):
    """优先直接打开【已加载】驱动的设备；设备不在时才用 sc.exe 安装。

    * close() 默认【不】停止/删除服务（避免把别人的按住进程弄断）
    * open(detect_write=False) 默认不探测写通道 → 纯只读工具用这个
    """

    def _open_devices(self):
        if self.hw is None:
            self.hw = _open(r"\\.\WinRing0_1_2_0")
            if self.hw is None:
                self.hw = _open("\\\\.\\" + self.svc_wr)
        if self.ht is None:
            self.ht = _open("\\\\.\\" + self.svc_ts)

    def open(self, restart=False, detect_write=False):
        if restart:
            self.close()
        self._open_devices()
        if self.hw in BAD or self.ht in BAD:
            # 设备不在 → 才装驱动（★ 需要管理员；会 stop/delete/create/start 服务）
            try:
                self._stage(self.svc_wr, L.WR)
                self._stage(self.svc_ts, L.TS)
            except Exception:
                pass
            self.hw = self.ht = None
            self._open_devices()
        ok = not (self.hw in BAD or self.ht in BAD)
        self.write_ioctl = None
        if ok and detect_write:
            self._detect_write()
        return ok

    def close(self, unload=False):
        for h in (self.hw, self.ht):
            if h not in (None, INVALID):
                try:
                    self.k.CloseHandle(h)
                except Exception:
                    pass
        self.hw = self.ht = None
        self.pages.clear()


def get_session(verbose=True, detect_write=True, vendor=0x10DE, device=0x1B87,
                bus_max=64):
    """返回一个可用且【已验证】的 Session，失败返回 None。

    detect_write=False 时不做任何写探测（纯只读）。
    """
    # 候选服务名（别人正在跑的按住进程优先）
    for svc_wr, svc_ts in (("P104Gen2x4Wr", "P104Gen2x4Ts"),
                           ("P104LibWr", "P104LibTs"),
                           ("G3TestWr", "G3TestTs")):
        s = L.Session(svc_wr=svc_wr, svc_ts=svc_ts)
        # 直接开设备，不 stage
        s.hw = _open(r"\\.\WinRing0_1_2_0")
        if s.hw is None:
            s.hw = _open("\\\\.\\" + svc_wr)
        s.ht = _open("\\\\.\\" + svc_ts)
        if s.hw is None or s.ht is None:
            if verbose:
                print("    [%s] 句柄: WinRing0=%s  TS=%s" %
                      (svc_ts, s.hw is not None, s.ht is not None))
            continue
        if not _map_ok(s.ht):
            if verbose:
                print("    [%s] 映射驱动不可用" % svc_ts)
            continue
        # 设置写入功能号（否则 pwr() 会落到默认 IOCTL）
        s.write_ioctl = None
        if detect_write:
            try:
                s._detect_write()
            except Exception as e:
                if verbose:
                    print("    [%s] _detect_write 失败: %s" % (svc_ts, e))
        if not _pci_ok(s):
            if verbose:
                print("    [%s] PCI 读不可用" % svc_ts)
            continue
        if not _find_gpu(s, vendor, device):
            if verbose:
                print("    [%s] 找不到 %04X:%04X" % (svc_ts, vendor, device))
            continue
        if verbose:
            print("    √ 复用驱动 [%s]  设备 %02X:%02X.%d  BAR0=0x%08X  write_ioctl=%s" %
                  (svc_ts, s.loc[0], s.loc[1], s.loc[2],
                   s.bar if isinstance(s.bar, int) else int(s.bar[0]),
                   ("0x%08X" % s.write_ioctl) if s.write_ioctl else "None"))
        return s

    # 全失败 → 尝试自己 stage（只在没有别的按住进程时可行）
    if verbose:
        print("    尝试自己装载驱动…（需要管理员）")
    s = FastSession()
    if s.open(detect_write=detect_write) and _find_gpu(s, vendor, device, bus_max):
        return s
    return None


def open_session(detect_write=False, retries=3, wait=1.0, verbose=True,
                 vendor=0x10DE, device=0x1B87, bus_max=64):
    """推荐的入口：FastSession（复用已加载驱动）优先，失败再退回 get_session。

    detect_write 默认 False —— 只读工具不要打开写探测。
    """
    for _ in range(max(1, retries)):
        s = FastSession()
        if s.open(detect_write=detect_write):
            if _find_gpu(s, vendor, device, bus_max):
                if verbose:
                    print("    √ 设备 %02X:%02X.%d  BAR0=0x%08X  write_ioctl=%s" %
                          (s.loc[0], s.loc[1], s.loc[2],
                           s.bar if isinstance(s.bar, int) else int(s.bar[0]),
                           ("0x%08X" % s.write_ioctl) if s.write_ioctl else "None(只读)"))
                return s
            s.close()
        time.sleep(wait)
    return get_session(verbose=verbose, detect_write=detect_write,
                       vendor=vendor, device=device, bus_max=bus_max)


if __name__ == "__main__":
    for _st in (sys.stdout, sys.stderr):          # 管道/重定向时用 UTF-8，避免中文乱码
        try:
            if _st is not None and hasattr(_st, "reconfigure") and not _st.isatty():
                _st.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if os.name != "nt":
        print("本工具只支持 Windows")
        sys.exit(1)
    print("=== 测试 open_session（只读）===")
    s = open_session()
    if s is None:
        print("× 失败（驱动没装 / 卡不在 / 无管理员权限）")
        sys.exit(1)
    g = s.loc
    cap = s.pcie_cap(*g)
    print("  LNKCAP  = 0x%08X" % s.prd(g[0], g[1], g[2], cap + 0x0C))
    print("  LNKSTA  = 0x%08X" % s.prd(g[0], g[1], g[2], cap + 0x12))
    print("  LNKCAP2 = 0x%08X" % s.prd(g[0], g[1], g[2], cap + 0x2C))
    print("  0x8C040 = 0x%08X" % s.rd(0x8C040))
    s.close()
    print("  √ 可用")
