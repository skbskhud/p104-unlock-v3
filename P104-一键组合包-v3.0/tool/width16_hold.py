# -*- coding: utf-8 -*-
"""width16_hold.py —— 让 P104 的 PCIe "声明最大宽度" 持续保持在 x16（零点：0x8C040 bit31=1）

背景（2026-10-28 实测，两次复现）：
  · 基线 LNKCAP = 0x00453C41（maxwidth=x4）
  · 写 0x8872C 低4位=6（NVPermissive 命名 NV_XVE_FUSE_OVERRIDE）
  · 再写 0x8C040 bit31=1（不碰其它位）→ LNKCAP 立刻变 0x00453D01（maxwidth=x16）
  · 整字写回基线 0x8C040=0x20085800 → 恢复 x4，不掉卡（掉卡只发生在"单 bit 清 bit31"的错回退）
  · 声明在 retrain / SBR 后仍保持 x16；实际链路宽度仍是 x4（物理 lane/电容没到位）
本工具：开机自动施加 + 看门狗保持（防止驱动/系统把它改回），并提供状态/恢复/重训命令。

子命令：
  status     只读显示当前 LNKCAP/LNKSTA/关键寄存器
  once       施加一次、验证、退出（不保持）
  run        施加 + 看门狗保持（默认常驻；--seconds N 用于限时测试；--no-retrain 跳过重训）
  restore    整字写回基线（0x8872C=0，0x8C040=开机基线值）
  retrain    触发根端口 retrain（补完电容后用这个让链路重新协商）
  install    注册登录自启计划任务（最高权限、隐藏窗口）
  uninstall  删除计划任务 + restore
"""
import sys, os, time, json, subprocess, ctypes as C
from ctypes import wintypes as W
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path: sys.path.insert(0, HERE)
import lib_p104 as L
import hwlock

# ── 驱动服务管理：不用 sc.exe（SYSTEM 计划任务下 sc.exe 会 0xC0000142），直接用 advapi32 ──
_adv = C.WinDLL("advapi32", use_last_error=True)
_adv.OpenSCManagerW.restype = C.c_void_p
_adv.OpenSCManagerW.argtypes = [W.LPCWSTR, W.LPCWSTR, W.DWORD]
_adv.CreateServiceW.restype = C.c_void_p
_adv.CreateServiceW.argtypes = [C.c_void_p, W.LPCWSTR, W.LPCWSTR, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.LPCWSTR, W.LPCWSTR, C.c_void_p, W.LPCWSTR, W.LPCWSTR, W.LPCWSTR]
_adv.OpenServiceW.restype = C.c_void_p
_adv.OpenServiceW.argtypes = [C.c_void_p, W.LPCWSTR, W.DWORD]
_adv.StartServiceW.argtypes = [C.c_void_p, W.DWORD, C.c_void_p]
_adv.ControlService.argtypes = [C.c_void_p, W.DWORD, C.c_void_p]
_adv.DeleteService.argtypes = [C.c_void_p]
_adv.CloseServiceHandle.argtypes = [C.c_void_p]

SC_MANAGER_ALL_ACCESS = 0xF003F
SERVICE_ALL_ACCESS = 0xF01FF
SERVICE_KERNEL_DRIVER = 1
SERVICE_DEMAND_START = 3
SERVICE_ERROR_NORMAL = 1
SERVICE_CONTROL_STOP = 1

def _scm():
    h = _adv.OpenSCManagerW(None, None, SC_MANAGER_ALL_ACCESS)
    return h

def svc_stop(name):
    scm = _scm()
    if not scm: return
    try:
        svc = _adv.OpenServiceW(scm, name, SERVICE_ALL_ACCESS)
        if svc:
            try:
                st = (W.DWORD * 8)()
                _adv.ControlService(svc, SERVICE_CONTROL_STOP, C.byref(st))
            except Exception:
                pass
            _adv.CloseServiceHandle(svc)
    finally:
        _adv.CloseServiceHandle(scm)

def svc_delete(name):
    scm = _scm()
    if not scm: return
    try:
        svc = _adv.OpenServiceW(scm, name, SERVICE_ALL_ACCESS)
        if svc:
            _adv.DeleteService(svc)
            _adv.CloseServiceHandle(svc)
    finally:
        _adv.CloseServiceHandle(scm)

def svc_create_start(name, path):
    svc_stop(name); svc_delete(name)
    scm = _scm()
    if not scm:
        try: log("OpenSCManagerW 失败 err=%d" % C.get_last_error())
        except Exception: pass
        return False
    try:
        svc = _adv.CreateServiceW(scm, name, name, SERVICE_ALL_ACCESS,
                                  SERVICE_KERNEL_DRIVER, SERVICE_DEMAND_START,
                                  SERVICE_ERROR_NORMAL, path, None, None, None, None, None)
        if not svc:
            try: log("CreateServiceW(%s) 失败 err=%d" % (name, C.get_last_error()))
            except Exception: pass
            return False
        ok = bool(_adv.StartServiceW(svc, 0, None))
        if not ok:
            try: log("StartServiceW(%s) 失败 err=%d" % (name, C.get_last_error()))
            except Exception: pass
        _adv.CloseServiceHandle(svc)
        return ok
    finally:
        _adv.CloseServiceHandle(scm)

class FastSession(L.Session):
    """优先直接打开已加载驱动的设备（auto-start 服务），只有设备不存在时才用 SCM 装驱动。
    这样：计划任务里没有 SCM 权限也能用；已经常驻的守护进程也不会因为另开工具而把驱动停了。"""
    def _open_devices(self):
        if self.hw is None:
            self.hw = self.k.CreateFileW(r"\\.\WinRing0_1_2_0", 0xC0000000, 0, None, 3, 0, None)
        if self.ht is None:
            self.ht = self.k.CreateFileW("\\\\.\\" + self.svc_ts, 0xC0000000, 0, None, 3, 0, None)

    def open(self, restart=False):
        if restart:
            self.close()
        self._open_devices()
        bad = (None, C.c_void_p(-1).value)
        if self.hw in bad or self.ht in bad:
            try:
                self._stage(self.svc_wr, L.WR)
                self._stage(self.svc_ts, L.TS)
            except Exception:
                pass
            self.hw = self.ht = None
            self._open_devices()
        ok = not (self.hw in bad or self.ht in bad)
        self.write_ioctl = None
        if ok:
            self._detect_write()
        return ok

    def _stage(self, svc, path):
        svc_create_start(svc, path)
        return "ctypes"

    def close(self, unload=False):
        # 注意：默认不删驱动服务 —— 服务由安装时创建为 auto-start，开机自动加载；
        # 计划任务里的进程没有 SCM 权限，但可以直接打开已加载驱动的设备对象。
        for h in (self.hw, self.ht):
            if h not in (None, C.c_void_p(-1).value):
                try: self.k.CloseHandle(h)
                except Exception: pass
        self.hw = self.ht = None
        self.pages.clear()
        if unload:
            for s in (self.svc_ts, self.svc_wr):
                svc_stop(s)
                time.sleep(0.15)
                for _ in range(3):
                    svc_delete(s)
                    time.sleep(0.15)

DATA_DIR = os.path.join(os.environ.get("ProgramData", os.path.expanduser("~")), "P104Unlock")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = HERE
STATE = os.path.join(DATA_DIR, "width16_state.json")
LOGF = os.path.join(DATA_DIR, "width16_hold.log")
TASK = "P104 PCIe Width x16 Hold"
PYW = os.path.join(os.path.dirname(HERE), "python", "pythonw.exe")
if not os.path.exists(PYW):
    PYW = sys.executable
SELF = os.path.abspath(__file__)

# pythonw.exe 下 sys.stdout/stderr 为 None —— 重定向到文件，否则 print 会抛异常
try:
    if sys.stdout is None or not hasattr(sys.stdout, "write"):
        _f = open(os.path.join(DATA_DIR, "width16_stdout.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = _f
        sys.stderr = _f
except Exception:
    pass

def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def cfg_read(s, loc, cap, off):
    v = s.prd(loc[0], loc[1], loc[2], cap + off)
    return 0xFFFFFFFF if v is None else v

def report(s, loc, cap):
    lc = cfg_read(s, loc, cap, 0x0C); l2 = cfg_read(s, loc, cap, 0x2C)
    ls = cfg_read(s, loc, cap, 0x12); lst2 = cfg_read(s, loc, cap, 0x32)
    v040 = s.rd(0x8C040); v8872 = s.rd(0x8872C)
    log("  LNKCAP=0x%08X (maxspeed=%d maxwidth=x%d) LNKCAP2=0x%08X LNKSTA=0x%08X (speed=%d x%d) LNKSTA2=0x%08X" % (
        lc, lc & 0xF, (lc >> 4) & 0x3F, l2, ls, ls & 0xF, (ls >> 4) & 0x3F, lst2))
    log("  BAR0: 0x8C040=0x%08X 0x8872C=0x%08X" % (v040 if v040 is not None else 0xFFFFFFFF,
                                                 v8872 if v8872 is not None else 0xFFFFFFFF))
    return lc, ls, v040, v8872

def load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

def find_gpu(s):
    loc = s.find()
    return loc

def arm(s, loc, st):
    """施加 【Gen2 声明+速率】 与 【宽度声明 x16】（顺序按 2026-10-28 实测有效序列）
       1) 0x8872C=6（XVE_FUSE_OVERRIDE）
       2) 0x2241C |= 0x81（PTOP_FS4 GEN2_PCIE 位）
       3) 0x8841C &= ~0x6000（清 GEN2_DIS）→ LNKCAP2 0x06
       4) 0x8C2C0 &= ~0x4 ; 0x8C300 &= ~0x10（CYA）
       5) 0x8C040 SPEED[19:18]=1（5GT）
       6) 0x8C1C0 [19:16]=2（5GT）
       7) 0x88610 |= 1
       8) 0x8C040 bit31=1（宽度 arm/commit）
       返回 True = Gen2 声明已形成 且 宽度声明=x16"""
    cap = s.cap
    rd = lambda off: s.rd(off)
    need = {0x8872C: None, 0x2241C: None, 0x8841C: None, 0x8C2C0: None, 0x8C300: None,
            0x8C040: None, 0x8C1C0: None, 0x88610: None}
    for off in need:
        need[off] = rd(off)
        if need[off] is None:
            return False
    if not st.get("base040"):
        st.update({"base040": need[0x8C040], "base8872": need[0x8872C],
                   "base8841": need[0x8841C], "base1c0": need[0x8C1C0],
                   "base2241": need[0x2241C], "base88610": need[0x88610]})
        save_state(st)
    # 1) override
    v = need[0x8872C]; nv = (v & ~0xF) | 6
    if v != nv: s.wr(0x8872C, nv, "gen2:override=6")
    # 2) PTOP_FS4
    v = need[0x2241C]; nv = v | 0x81
    if v != nv: s.wr(0x2241C, nv, "gen2:PTOP_FS4=0x81")
    # 3) GEN2_DIS
    v = need[0x8841C]; nv = v & ~0x6000
    if v != nv: s.wr(0x8841C, nv, "gen2:clear GEN2_DIS")
    # 4) CYAs
    v = need[0x8C2C0]
    if v & 0x4: s.wr(0x8C2C0, v & ~0x4, "gen2:CYA_0 bit2=0")
    v = need[0x8C300]
    if v & 0x10: s.wr(0x8C300, v & ~0x10, "gen2:CYA_1 bit4=0")
    # 5) SPEED=1（先不带 bit31）
    v = need[0x8C040]; nv = (v & ~0xC0000) | 0x40000
    if v != nv: s.wr(0x8C040, nv, "gen2:SPEED=5GT")
    # 6) LNK_CAP_SPEED=2
    v = need[0x8C1C0]; nv = (v & ~0xF0000) | 0x20000
    if v != nv: s.wr(0x8C1C0, nv, "gen2:LNK_CAP_SPEED=5GT")
    # 7) VSEC
    v = need[0x88610]; nv = v | 0x1
    if v != nv: s.wr(0x88610, nv, "gen2:VSEC bit0=1")
    time.sleep(0.05)
    # 8) 宽度 arm（单独一步，保持其它位）
    v = rd(0x8C040)
    if v is not None and not (v & 0x80000000):
        s.wr(0x8C040, v | 0x80000000, "width16:arm bit31")
    time.sleep(0.05)
    lc = cfg_read(s, loc, cap, 0x0C)
    l2 = cfg_read(s, loc, cap, 0x2C)
    return ((lc >> 4) & 0x3F) == 16 and (l2 & 0x6) == 0x6

def retrain(s):
    if not s.root:
        return False
    b, d, f = s.root
    rc = s.pcie_cap(b, d, f)
    cur = s.prd(b, d, f, rc + 0x30) or 0
    s.pwr(b, d, f, rc + 0x30, (cur & ~0xF) | 3)   # 目标 8GT：端点声明 Gen2，会协商到 Gen2
    lctl = s.prd(b, d, f, rc + 0x10) or 0
    s.pwr(b, d, f, rc + 0x10, lctl | (1 << 5))
    return True

def restore(s, st):
    ok = True
    for off, key, note in ((0x8C040, "base040", "restore-040"),
                           (0x8C1C0, "base1c0", "restore-1c0"),
                           (0x8841C, "base8841", "restore-8841"),
                           (0x8872C, "base8872", "restore-8872"),
                           (0x2241C, "base2241", "restore-2241"),
                           (0x88610, "base88610", "restore-88610")):
        if st.get(key) is not None:
            s.wr(off, st[key], "width16:" + note); time.sleep(0.03)
            ok &= (s.rd(off) == st[key])
    return ok

def open_session():
    s = FastSession()
    ok = s.open()
    if not ok:
        try: log("open() 失败: hw=%s ht=%s err=%s" % (s.hw, s.ht, C.get_last_error()))
        except Exception: pass
        return None
    loc = s.find()
    if not loc:
        try: log("find() 失败: 驱动已装但枚举不到 P104（prd(0,0,0)=%s）" % (s.prd(0, 0, 0, 0x00),))
        except Exception: pass
        s.close()
        return None
    return s

def cmd_status():
    s = open_session()
    if not s:
        log("!! BYOVD 装载失败或找不到 P104"); return 2
    try:
        log("P104 @ %02X:%02X.%d" % s.loc)
        report(s, s.loc, s.cap)
        return 0
    finally:
        s.close()

def cmd_restore():
    st = load_state()
    s = open_session()
    if not s:
        log("!! 找不到 P104，无法恢复"); return 2
    try:
        ok = restore(s, st)
        log("restore %s" % ("✓" if ok else "✗"))
        report(s, s.loc, s.cap)
        return 0 if ok else 1
    finally:
        s.close()

def cmd_once(do_retrain=True):
    s = open_session()
    if not s:
        log("!! 找不到 P104"); return 2
    try:
        st = load_state()
        ok = arm(s, s.loc, st)
        log("arm x16: %s" % ("✓" if ok else "✗"))
        report(s, s.loc, s.cap)
        if do_retrain:
            retrain(s); time.sleep(1.0); log("retrain 已下发"); report(s, s.loc, s.cap)
        return 0 if ok else 1
    finally:
        s.close()

def cmd_run(seconds=0, do_retrain=True):
    if not hwlock.acquire("width16-hold", "P104 宽度x16声明保持(看门狗)", minutes=1440):
        log("!! 拿不到硬件锁（可能有其它硬件实验在跑），退出")
        return 1
    s = None
    t0 = time.time()
    last_renew = time.time()
    did_retrain = False
    try:
        if seconds == 0:
            time.sleep(2.0)   # 给登录后的驱动初始化留一点时间
        st = load_state()
        last_log = 0
        last_fail_log = 0
        while True:
            if time.time() - last_renew > 600:
                try: hwlock.renew("width16-hold", 1440)
                except Exception: pass
                last_renew = time.time()
            if s is None:
                s = open_session()
                if not s:
                    if time.time() - last_fail_log > 15:
                        log("!! 连不上 P104 或 BYOVD 驱动装载失败（重试中；可能是非管理员上下文）")
                        last_fail_log = time.time()
                    time.sleep(2); continue
                log("已连上 P104 %02X:%02X.%d" % s.loc)
            try:
                ok = arm(s, s.loc, st)
                if not ok and (s.rd(0x8C040) is None or s.rd(0x8872C) is None):
                    log("!! 设备读不到（驱动可能被停/卡被重置）-> 重新连接")
                    try: s.close()
                    except Exception: pass
                    s = None
                    time.sleep(1)
                    continue
                if ok and do_retrain and not did_retrain:
                    retrain(s); time.sleep(1.0)
                    did_retrain = True
                    log("首次施加后已触发 retrain")
                if ok and time.time() - last_log > 30:
                    lc = cfg_read(s, s.loc, s.cap, 0x0C)
                    log("保持中: LNKCAP=0x%08X maxwidth=x%d" % (lc, (lc >> 4) & 0x3F))
                    last_log = time.time()
            except Exception as e:
                log("!! 写入异常(可能是掉卡): %s -> 重新连接" % e)
                try: s.close()
                except Exception: pass
                s = None
            if seconds and time.time() - t0 >= seconds:
                log("到达 --seconds 时限，恢复并退出")
                try:
                    restore(s, st); log("已恢复基线")
                except Exception: pass
                return 0
            time.sleep(0.1)
    finally:
        try:
            if s: s.close()
        except Exception: pass
        hwlock.release("width16-hold")

def cmd_retrain():
    s = open_session()
    if not s:
        log("!! 找不到 P104"); return 2
    try:
        retrain(s); time.sleep(1.5)
        log("retrain 已下发")
        report(s, s.loc, s.cap)
        return 0
    finally:
        s.close()

def cmd_oneclick():
    """一键破解：确保驱动(auto-start)+计划任务，立即施加并打印状态。需要管理员控制台。"""
    log("=" * 60)
    log("P104-100  一键解锁：Gen2 x4  +  声明宽度 x16")
    log("（只写易失寄存器：不刷 VBIOS、不烧熔丝，断电即恢复）")
    log("=" * 60)
    # 1) 驱动：确保存在 + auto-start + 运行
    for name, path in (("P104LibWr", L.WR), ("P104LibTs", L.TS)):
        exists = False
        scm = _scm()
        if scm:
            try:
                svc = _adv.OpenServiceW(scm, name, SERVICE_ALL_ACCESS)
                if svc:
                    exists = True
                    _adv.StartServiceW(svc, 0, None)   # 已在跑则失败，忽略
                    _adv.CloseServiceHandle(svc)
            finally:
                _adv.CloseServiceHandle(scm)
        if not exists:
            svc_create_start(name, path)
        try:
            subprocess.run(["sc.exe", "config", name, "start=", "auto"],
                           capture_output=True, timeout=15)
        except Exception:
            pass
        log("驱动 %s: %s" % (name, "OK" if exists or True else "?"))
    # 2) 计划任务（SYSTEM / 登录触发 / 最高权限）
    tr = '"%s" "%s" run' % (PYW, SELF)
    try:
        r = subprocess.run(["schtasks", "/create", "/tn", TASK, "/tr", tr, "/sc", "onlogon",
                            "/ru", "SYSTEM", "/rl", "highest", "/f"], capture_output=True, timeout=30)
        log("计划任务注册: rc=%d" % r.returncode)
    except Exception as e:
        log("计划任务注册异常: %s" % e)
    # 3) 立即运行
    try:
        subprocess.run(["schtasks", "/run", "/tn", TASK], capture_output=True, timeout=20)
    except Exception as e:
        log("启动任务异常: %s" % e)
    log("等待 8 秒让保持进程施加 x16 ...")
    time.sleep(8)
    # 4) 打印状态
    s = open_session()
    if not s:
        log("!! 状态读取失败（保持进程可能已占用设备，可稍后看 width16_hold.log）")
        return 1
    try:
        log("当前状态：")
        report(s, s.loc, s.cap)
        lc = cfg_read(s, s.loc, s.cap, 0x0C)
        l2 = cfg_read(s, s.loc, s.cap, 0x2C)
        ls = cfg_read(s, s.loc, s.cap, 0x12)
        log("")
        log("判据：")
        log("  声明 Gen2 = %s (LNKCAP2=0x%08X，bit1+bit2)" % ("✔" if (l2 & 0x6) == 0x6 else "✘", l2))
        log("  声明 x16  = %s (LNKCAP maxwidth=%d)" % ("✔" if ((lc >> 4) & 0x3F) == 16 else "✘", (lc >> 4) & 0x3F))
        log("  当前链路  = speed=%d width=%d（补完电容前 width 仍可能是 4）" % (ls & 0xF, (ls >> 4) & 0x3F))
        return 0
    finally:
        s.close()

def cmd_install():
    tr = '"%s" "%s" run' % (PYW, SELF)
    r = subprocess.run(["schtasks", "/create", "/tn", TASK, "/tr", tr, "/sc", "onlogon",
                        "/ru", "SYSTEM", "/rl", "highest", "/f"], capture_output=True, text=True)
    log("schtasks create rc=%d %s%s" % (r.returncode, r.stdout.strip(), r.stderr.strip()))
    subprocess.run(["schtasks", "/run", "/tn", TASK], capture_output=True)
    return 0 if r.returncode == 0 else 1

def cmd_uninstall():
    subprocess.run(["schtasks", "/end", "/tn", TASK], capture_output=True)
    r = subprocess.run(["schtasks", "/delete", "/tn", TASK, "/f"], capture_output=True, text=True)
    log("schtasks delete rc=%d %s%s" % (r.returncode, r.stdout.strip(), r.stderr.strip()))
    rc = cmd_restore()
    for name in ("P104LibWr", "P104LibTs"):
        svc_stop(name)
        time.sleep(0.2)
        svc_delete(name)
        log("驱动服务 %s 已停止并删除" % name)
    return 0 if r.returncode == 0 else 1

def main(argv):
    sub = argv[1] if len(argv) > 1 else "status"
    if sub == "status":   return cmd_status()
    if sub == "once":     return cmd_once("--no-retrain" not in argv)
    if sub == "oneclick": return cmd_oneclick()
    if sub == "retrain":  return cmd_retrain()
    if sub == "restore":  return cmd_restore()
    if sub == "install":  return cmd_install()
    if sub == "uninstall": return cmd_uninstall()
    if sub == "run":
        sec = 0
        if "--seconds" in argv:
            try: sec = int(argv[argv.index("--seconds") + 1])
            except Exception: sec = 0
        return cmd_run(seconds=sec, do_retrain="--no-retrain" not in argv)
    print(__doc__); return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
