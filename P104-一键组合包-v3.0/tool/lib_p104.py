"""lib_p104.py —— P104 PCIe 研究的公共底座（BYOVD 通道 + PCI + BAR0 MMIO）

设计要点（吸取 10-13 事故教训）：
  * MapPage 每次调用都会泄漏内核映射 -> 本库对每个物理页只映射一次并缓存
  * 支持"重新装载驱动"，但只在显式调用时发生
  * 所有写操作都有 restore 记录
用法：import lib_p104 as L;  s = L.Session();  s.open()
"""
import os
import ctypes as C
import ctypes.wintypes as W
import struct
import subprocess
import time
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DRV = os.path.abspath(os.path.join(_HERE, "..", "drivers"))
WR = os.path.join(_DRV, "WinRing0x64.sys")
TS = os.path.join(_DRV, "ThrottleStop.sys")

IOCTL_PCI_READ = 0x9C406144      # in: {u32 addr, u32 reg} -> out u32
OLS_TYPE = 40000                 # 0x9C40 = WinRing0/OpenLibSys 的 DeviceType
# ★ 修正（2026-10-13 接力轮）：写功能号是 WinRing0 的 0x852，
#   而 access 位必须为 2 -> CTL_CODE = 0x9C40<<16 | 2<<14 | 0x852<<2 = 0x9C40610A。
#   上个 AI 全程用的是 0x9C406148（access=1），写请求从未真正下发 —— 所有"只读/写不进"结论都不可信。
IOCTL_PCI_WRITE = 0x9C40610A     # in: {u32 addr, u32 reg, u32 val} -> out u32
IOCTL_PCI_WRITE_CANDIDATES = [(OLS_TYPE << 16) | (acc << 14) | (0x852 << 2) | 0
                              for acc in (2, 1, 0, 3)]
IOCTL_MAP_PHYS = 0x8000645C      # in: {u64 phys, u32 size} -> out u64 va

SPD = {0: "2.5GT/s?", 1: "2.5GT/s(Gen1)", 2: "5.0GT/s(Gen2)", 3: "8.0GT/s(Gen3)",
       4: "16GT/s(Gen4)", 5: "32GT/s(Gen5)"}


def out(*a):
    print(" ".join(str(x) for x in a), flush=True)


def sh(cmd):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, errors="replace")
    return (p.stdout or "") + (p.stderr or "")


class Session(object):
    def __init__(self, svc_wr="P104LibWr", svc_ts="P104LibTs"):
        self.svc_wr, self.svc_ts = svc_wr, svc_ts
        self.hw = None
        self.ht = None
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.k.CreateFileW.restype = C.c_void_p
        self.k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p,
                                       W.DWORD, W.DWORD, C.c_void_p]
        self.k.DeviceIoControl.restype = W.BOOL
        self.k.DeviceIoControl.argtypes = [C.c_void_p, W.DWORD, C.c_void_p, W.DWORD,
                                           C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
        self.k.CloseHandle.argtypes = [C.c_void_p]
        self.pages = {}          # phys_page -> va (只映射一次)
        self.writes = []         # (kind, target, reg, old, new) 还原记录
        self.bar = None
        self.loc = None          # (bus, dev, fun)
        self.cap = None          # PCIe capability offset
        self.root = None         # 上游根端口 (bus, dev, fun)
        self.root_cap = None

    # ---------------- 驱动装载 ----------------
    def _stage(self, svc, path):
        sh(f'sc.exe stop {svc}')
        sh(f'sc.exe delete {svc}')
        sh(f'sc.exe create {svc} type= kernel start= demand binPath= "{path}"')
        r = sh(f'sc.exe start {svc}')
        return r

    def open(self, restart=False):
        if restart:
            self.close(unload=True)
        if self.hw is None:
            self._stage(self.svc_wr, WR)
            self.hw = self.k.CreateFileW(r"\\.\WinRing0_1_2_0", 0xC0000000, 0, None, 3, 0, None)
        if self.ht is None:
            self._stage(self.svc_ts, TS)
            self.ht = self.k.CreateFileW("\\\\.\\" + self.svc_ts, 0xC0000000, 0, None, 3, 0, None)
        ok = not (self.hw in (None, C.c_void_p(-1).value) or
                  self.ht in (None, C.c_void_p(-1).value))
        self.write_ioctl = None
        if ok:
            self._detect_write()
        return ok

    def _raw_write(self, ioctl, bus, dev, fun, reg, val):
        ib = struct.pack("<III", (bus << 8) | (dev << 3) | fun, reg, val)
        o = C.create_string_buffer(4)
        n = W.DWORD(0)
        return bool(self.k.DeviceIoControl(self.hw, ioctl, ib, 12, o, 4, C.byref(n), None))

    def _detect_write(self):
        """自检写 IOCTL：对 P104 的 0x00（只读的 Vendor/Device）做 no-op 同值写。
        请求成功 == 写通道可用；值不变是硬件只读所致，不是通道问题。"""
        for c in IOCTL_PCI_WRITE_CANDIDATES:
            if self._raw_write(c, 1, 0, 0, 0x00, 0x1B8710DE):
                self.write_ioctl = c
                return c
        return None

    def close(self, unload=True):
        for h in (self.hw, self.ht):
            if h not in (None, C.c_void_p(-1).value):
                try:
                    self.k.CloseHandle(h)
                except Exception:
                    pass
        self.hw = self.ht = None
        self.pages.clear()
        if unload:
            for s in (self.svc_ts, self.svc_wr):
                sh(f'sc.exe stop {s}')
                sh(f'sc.exe delete {s}')

    def remap(self):
        """链路重训后旧映射失效：重装驱动 + 清缓存"""
        self.pages.clear()
        self.open(restart=True)

    # ---------------- PCI 配置空间 ----------------
    def prd(self, bus, dev, fun, reg):
        if self.hw is None:
            return None
        ib = struct.pack("<II", (bus << 8) | (dev << 3) | fun, reg)
        o = C.create_string_buffer(4)
        n = W.DWORD(0)
        if self.k.DeviceIoControl(self.hw, IOCTL_PCI_READ, ib, 8, o, 4, C.byref(n), None):
            return struct.unpack("<I", o.raw[:4])[0]
        return None

    def pwr(self, bus, dev, fun, reg, val):
        if self.hw is None:
            return False
        ioctl = self.write_ioctl or IOCTL_PCI_WRITE
        return self._raw_write(ioctl, bus, dev, fun, reg, val)

    # ---------------- BAR0 MMIO ----------------
    def page(self, off):
        key = self.bar + (off & ~0xFFF)
        if key not in self.pages:
            ib = struct.pack("<QI", key, 0x1000)
            o = C.create_string_buffer(8)
            n = W.DWORD(0)
            va = 0
            if self.k.DeviceIoControl(self.ht, IOCTL_MAP_PHYS, ib, 12, o, 8, C.byref(n), None):
                va = struct.unpack("<Q", o.raw[:8])[0]
            self.pages[key] = va
        return self.pages[key]

    def rd(self, off):
        va = self.page(off)
        if not va:
            return None
        return C.c_uint32.from_address(va + (off & 0xFFF)).value

    def page_at(self, phys):
        """任意物理地址的页映射（只读用途）"""
        key = phys & ~0xFFF
        if key not in self.pages:
            ib = struct.pack("<QI", key, 0x1000)
            o = C.create_string_buffer(8)
            n = W.DWORD(0)
            va = 0
            if self.k.DeviceIoControl(self.ht, IOCTL_MAP_PHYS, ib, 12, o, 8, C.byref(n), None):
                va = struct.unpack("<Q", o.raw[:8])[0]
            self.pages[key] = va
        return self.pages[key]

    def rd_phys(self, phys):
        va = self.page_at(phys)
        if not va:
            return None
        return C.c_uint32.from_address(va + (phys & 0xFFF)).value

    def wr(self, off, val, note=""):
        va = self.page(off)
        if not va:
            return False
        old = C.c_uint32.from_address(va + (off & 0xFFF)).value
        self.writes.append(("bar0", off, old, val, note))
        C.c_uint32.from_address(va + (off & 0xFFF)).value = val & 0xFFFFFFFF
        return True

    # ---------------- 发现 ----------------
    def find(self, bus_max=64):
        for b in range(bus_max):
            for d in range(32):
                if self.prd(b, d, 0, 0x00) == 0x1B8710DE:
                    self.loc = (b, d, 0)
                    break
                # 桥设备：继续扫它的次级总线已在循环里覆盖
            if self.loc:
                break
        if not self.loc:
            return None
        b, d, f = self.loc
        self.bar = (self.prd(b, d, f, 0x10) or 0) & 0xFFFFFFF0
        self.cap = self.pcie_cap(b, d, f)
        self.root = self.find_root_port(b, d, f)
        if self.root:
            self.root_cap = self.pcie_cap(*self.root)
        return self.loc

    def pcie_cap(self, b, d, f):
        ptr = (self.prd(b, d, f, 0x34) or 0) & 0xFC
        seen = set()
        while ptr and ptr not in seen and ptr < 0x100:
            seen.add(ptr)
            hd = self.prd(b, d, f, ptr)
            if hd is None:
                return None
            if (hd & 0xFF) == 0x10:
                return ptr
            ptr = (hd >> 8) & 0xFC
        return None

    def find_root_port(self, b, d, f):
        """暴力找：哪个桥的次级总线区间包含我们的总线（取最深的那层）
        ★ 修正：必须扫全部 function —— PCH 端口常在 fn>0（例如 00:1D.3）"""
        best = None
        for bb in range(64):
            for dd in range(32):
                for ff in range(8):
                    v = self.prd(bb, dd, ff, 0x00)
                    if v is None or v == 0xFFFFFFFF or (v & 0xFFFF) == 0xFFFF:
                        continue
                    if ((self.prd(bb, dd, ff, 0x08) or 0) >> 8) != 0x060400:
                        continue
                    sec = (self.prd(bb, dd, ff, 0x19) or 0) & 0xFF
                    sub = (self.prd(bb, dd, ff, 0x1A) or 0) & 0xFF
                    if sec <= b <= sub:
                        if best is None or sec > best[3]:
                            best = (bb, dd, ff, sec)
        return best[:3] if best else None

    # ---------------- 链路状态 ----------------
    def link(self, b, d, f, cap=None):
        cap = cap if cap is not None else self.pcie_cap(b, d, f)
        if cap is None:
            return None
        lc = self.prd(b, d, f, cap + 0x0C)
        ls = self.prd(b, d, f, cap + 0x12)
        l2 = self.prd(b, d, f, cap + 0x2C)
        c2 = self.prd(b, d, f, cap + 0x30)
        lctl = self.prd(b, d, f, cap + 0x10)
        return dict(cap=cap, lnkcap=lc, lnksta=ls, lcap2=l2, lnkctl2=c2, lnkctl=lctl,
                    maxspd=lc & 0xF, maxw=(lc >> 4) & 0x3F,
                    curspd=ls & 0xF, curw=(ls >> 4) & 0x3F, tls=c2 & 0xF,
                    retrain=bool(lctl & (1 << 5)), linkdis=bool(lctl & (1 << 4)))

    def fmt_link(self, tag, b, d, f):
        L = self.link(b, d, f)
        if not L:
            return f"{tag} {b:02X}:{d:02X}.{f}: <no PCIe cap>"
        return (f"{tag} {b:02X}:{d:02X}.{f} cap@0x{L['cap']:02X} | "
                f"LNKCAP=0x{L['lnkcap']:08X} {SPD.get(L['maxspd'],'?')} x{L['maxw']} | "
                f"LNKSTA=0x{L['lnksta']:08X} {SPD.get(L['curspd'],'?')} x{L['curw']} | "
                f"LCAP2=0x{L['lcap2']:08X} | TLS={L['tls']} | CTL=0x{L['lnkctl']:04X}"
                + (" RETRAIN" if L['retrain'] else "") + (" LINKDIS" if L['linkdis'] else ""))

    # ---------------- 链路重训（安全版：写 TLS + Retrain，轮询） ----------------
    def retrain(self, target_tls=None, wait=3.0, poll=0.1):
        """在端点与根端口两侧设置 TLS 并触发重训；返回 (成功?, 用时)"""
        res = {}
        for tag, ep in (("root", self.root), ("gpu", self.loc)):
            if not ep:
                continue
            cap = self.pcie_cap(*ep)
            if cap is None:
                continue
            if target_tls is not None:
                cur = self.prd(ep[0], ep[1], ep[2], cap + 0x30)
                if (cur & 0xF) != target_tls:
                    self.pwr(ep[0], ep[1], ep[2], cap + 0x30, (cur & ~0xF) | target_tls)
                    self.writes.append(("pci", ep, cap + 0x30, cur, (cur & ~0xF) | target_tls, "TLS"))
            lctl = self.prd(ep[0], ep[1], ep[2], cap + 0x10) or 0
            self.pwr(ep[0], ep[1], ep[2], cap + 0x10, lctl | (1 << 5))
            self.writes.append(("pci", ep, cap + 0x10, lctl, lctl | (1 << 5), "retrain"))
            res[tag] = cap
        t0 = time.time()
        while time.time() - t0 < wait:
            time.sleep(poll)
            L = self.link(*self.loc)
            if L and not L['retrain']:
                break
        return res, time.time() - t0


def dump_pci_tree(s):
    out("=== PCI 拓扑（含 PCIe 链路能力）===")
    for b in range(64):
        for d in range(32):
            v = s.prd(b, d, 0, 0x00)
            if v is None or v in (0xFFFFFFFF, 0x00000000, 0x0000FFFF):
                continue
            vid = v & 0xFFFF
            did = (v >> 16) & 0xFFFF
            cls = ((s.prd(b, d, 0, 0x0B) or 0) >> 16) & 0xFF
            sub = ((s.prd(b, d, 0, 0x0B) or 0) >> 8) & 0xFF
            name = ""
            if vid == 0x10DE:
                name = "NVIDIA"
            elif vid == 0x8086:
                name = "Intel"
            elif vid == 0x1002:
                name = "AMD"
            if cls == 0x06:
                secb = (s.prd(b, d, 0, 0x19) or 0) & 0xFF
                name += f" bridge(sec={secb:02X})"
            out(f"  {b:02X}:{d:02X}.0  {vid:04X}:{did:04X} cls={cls:02X}{sub:02X} {name}")


if __name__ == "__main__":
    s = Session()
    if not s.open():
        out("BYOVD 驱动装载失败")
        sys.exit(1)
    loc = s.find()
    if not loc:
        out("找不到 P104")
        sys.exit(2)
    dump_pci_tree(s)
    out("")
    out("=== 目标卡与上游链 ===")
    out(s.fmt_link("P104  ", *s.loc))
    if s.root:
        out(s.fmt_link("根端口", *s.root))
        # 根端口上游（CPU PEG 端口可能还有一层）
        up = s.find_root_port(*s.root)
        if up:
            out(s.fmt_link("上级  ", *up))
    out("")
    out(f"=== BAR0 = 0x{s.bar:08X} 关键寄存器 ===")
    for off, nm in ((0x8C040, "PPCI_2.CONFIG_LINK(速率)"), (0x8C080, "PPCI_2.WIDTH(当前宽)"),
                    (0x8C1C0, "UNK1C0(LNK_CAP_SPEED)"), (0x88140, "LINK_WIDTH(特权)"),
                    (0x88084, "LNKCAP 镜像"), (0x8802C, "LCAP2 镜像"),
                    (0x880A8, "LNKCTL2 镜像"), (0x88460, "nv_pci.CONFIG_LINK"),
                    (0x8841C, "PRIV_MISC_1"), (0x8872C, "LTSSM"), (0x88610, "VSEC_HIER"),
                    (0x101000, "PSTRAPS0"), (0x101004, "PSTRAPS1"), (0x101008, "PSTRAPS2")):
        v = s.rd(off)
        ex = ""
        if off == 0x8C040 and v is not None:
            ex = f"  SPEED[19:18]={(v >> 18) & 3}"
        out(f"  0x{off:06X} {nm:26s} = " + (f"0x{v:08X}{ex}" if v is not None else "(读失败)"))
    s.close()
