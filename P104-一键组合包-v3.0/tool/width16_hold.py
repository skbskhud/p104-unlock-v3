# -*- coding: utf-8 -*-
"""width16_hold.py —— P104 解锁保持器（Gen2 x4 默认 + 宽度声明 x16 实验开关）

2026-10-29 安全版（相比 10-28 版的关键改动）：
  1) **开机默认只做 Gen2**（成熟路径）；宽度声明 x16 改为手动开关（width-on / width-off）
  2) 启动延迟默认 60 秒（等登录后 NVIDIA 驱动完全初始化），可 --delay N 覆盖
  3) 每步写入前记录日志、写后只查 **config space**（不读 BAR0，掉卡后读 BAR0 会挂死）
  4) 一旦发现设备无响应立即中止本次施加，不做任何后续 BAR0 访问
  5) 清理重启后残留的硬件锁（pid 已死）
  6) 单写者：CLI 的 retrain / width-on / width-off 在保持进程运行时改为"命令文件排队"，
     由保持进程统一执行，避免两个进程同时写寄存器
  7) 看门狗只在链路没到 Gen2 时重训，30 秒一次、最多 3 次，避免打搅链路
所有写入都是易失寄存器：不刷 VBIOS、不烧熔丝，断电即恢复。
"""
import sys, os, time, json, subprocess, ctypes as C
from ctypes import wintypes as W
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import lib_p104 as L
import hwlock

# ── 数据目录（可写；ProgramData 不存在时退回脚本目录）──
DATA_DIR = os.path.join(os.environ.get("ProgramData", os.path.expanduser("~")), "P104Unlock")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = HERE
STATE = os.path.join(DATA_DIR, "width16_state.json")
LOGF = os.path.join(DATA_DIR, "width16_hold.log")
CMDDIR = DATA_DIR
TASK = "P104 PCIe Width x16 Hold"
PYW = os.path.join(os.path.dirname(HERE), "python", "pythonw.exe")
if not os.path.exists(PYW):
    PYW = sys.executable
SELF = os.path.abspath(__file__)

# pythonw 下 stdout/stderr 为 None → 重定向
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

# ── advapi32 服务管理（SYSTEM 计划任务下 sc.exe 不可靠）──
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
    return _adv.OpenSCManagerW(None, None, SC_MANAGER_ALL_ACCESS)

def svc_stop(name):
    scm = _scm()
    if not scm: return
    try:
        svc = _adv.OpenServiceW(scm, name, SERVICE_ALL_ACCESS)
        if svc:
            try:
                st = (W.DWORD * 8)()
                _adv.ControlService(svc, SERVICE_CONTROL_STOP, C.byref(st))
            except Exception: pass
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
        log("OpenSCManagerW 失败 err=%d" % C.get_last_error()); return False
    try:
        svc = _adv.CreateServiceW(scm, name, name, SERVICE_ALL_ACCESS,
                                  SERVICE_KERNEL_DRIVER, SERVICE_DEMAND_START,
                                  SERVICE_ERROR_NORMAL, path, None, None, None, None, None)
        if not svc:
            log("CreateServiceW(%s) 失败 err=%d" % (name, C.get_last_error())); return False
        ok = bool(_adv.StartServiceW(svc, 0, None))
        if not ok:
            log("StartServiceW(%s) 失败 err=%d" % (name, C.get_last_error()))
        _adv.CloseServiceHandle(svc)
        return ok
    finally:
        _adv.CloseServiceHandle(scm)

class FastSession(L.Session):
    """优先直接打开 auto-start 驱动的设备；设备不在时才走 SCM 安装。close 默认不删服务。"""
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

def _pid_alive(pid):
    try:
        k32 = C.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = C.c_void_p
        h = k32.OpenProcess(0x1000, False, int(pid))
        if h:
            k32.CloseHandle(C.c_void_p(h)); return True
        return False
    except Exception:
        return False

def clean_stale_lock():
    try:
        p = getattr(hwlock, "LOCK", None)
        if not p or not os.path.exists(p):
            return
        with open(p, "r", encoding="utf-8") as f:
            info = json.load(f)
        owner = info.get("owner"); pid = info.get("pid")
        if owner == "width16-hold" and (not pid or not _pid_alive(pid)):
            os.remove(p)
            log("清理残留硬件锁（owner=%s pid=%s 已不存在，视为重启遗留）" % (owner, pid))
    except Exception:
        pass

def lock_holder():
    """返回硬件锁当前持有者 (owner, pid) 或 None"""
    try:
        p = getattr(hwlock, "LOCK", None)
        if not p or not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            info = json.load(f)
        return info.get("owner"), info.get("pid")
    except Exception:
        return None

# ── 默认工厂基线（首次运行若无 state 则用；restore 用）──
FACTORY = {"base040": 0x20085800, "base8872": 0x00000000, "base8841": 0xC0B46500,
           "base1c0": 0x00210036, "base2241": 0x00000000, "base88610": 0x00000000}

def load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    for k, v in FACTORY.items():
        st.setdefault(k, v)
    return st

def save_state(st):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def cfg(s, loc, cap, off):
    v = s.prd(loc[0], loc[1], loc[2], cap + off)
    return 0xFFFFFFFF if v is None else v

def alive(s, loc):
    v = s.prd(loc[0], loc[1], loc[2], 0x00)
    return v == 0x1B8710DE

def open_session(retries=3, wait=1.0):
    for i in range(retries):
        s = FastSession()
        if not s.open():
            s = None
        else:
            loc = s.find()
            if loc:
                return s
            s.close()
        time.sleep(wait)
    return None

def report(s, loc, cap):
    lc = cfg(s, loc, cap, 0x0C); l2 = cfg(s, loc, cap, 0x2C)
    ls = cfg(s, loc, cap, 0x12); lst2 = cfg(s, loc, cap, 0x32)
    log("  LNKCAP=0x%08X (declared speed=%d, maxwidth=x%d) LNKCAP2=0x%08X LNKSTA=0x%08X (speed=%d, width=%d) LNKSTA2=0x%08X" % (
        lc, lc & 0xF, (lc >> 4) & 0x3F, l2, ls, ls & 0xF, (ls >> 4) & 0x3F, lst2))
    return lc, l2, ls, lst2

def retrain(s):
    if not s.root:
        return False
    b, d, f = s.root
    rc = s.pcie_cap(b, d, f)
    cur = s.prd(b, d, f, rc + 0x30) or 0
    s.pwr(b, d, f, rc + 0x30, (cur & ~0xF) | 3)
    lctl = s.prd(b, d, f, rc + 0x10) or 0
    s.pwr(b, d, f, rc + 0x10, lctl | (1 << 5))
    return True

def apply_gen2(s, loc, st, with_width=False):
    """分步施加 Gen2（可选宽度 x16），每步写前先确认设备在线；写后只查 config space。
       返回 (gen2_ok, width_ok, lost)"""
    cap = s.cap
    if not alive(s, loc):
        return False, False, True
    def wr(off, val, note):
        if not alive(s, loc):
            log("   !! 设备在写入前失去响应，中止 [%s]" % note)
            return False
        log("   写 0x%06X = 0x%08X  [%s]" % (off, val, note))
        s.wr(off, val, note)
        time.sleep(0.02)
        if not alive(s, loc):
            log("   !! 写入后设备失去响应 [%s]" % note)
            return False
        return True
    def rd(off):
        v = s.rd(off)
        if v is None:
            log("   !! 读 0x%06X 失败" % off)
        return v
    # 1) Gen2 声明
    v = rd(0x8872C)
    if v is None: return False, False, True
    if not wr(0x8872C, (v & ~0xF) | 6, "XVE_FUSE_OVERRIDE=6"): return False, False, True
    v = rd(0x2241C)
    if v is None: return False, False, True
    if not wr(0x2241C, v | 0x81, "PTOP_FS4 |= 0x81"): return False, False, True
    v = rd(0x8841C)
    if v is None: return False, False, True
    if v & 0x6000:
        if not wr(0x8841C, v & ~0x6000, "清 GEN2_DIS (bits13,14)"): return False, False, True
    v = rd(0x8C2C0)
    if v is None: return False, False, True
    if v & 0x4:
        if not wr(0x8C2C0, v & ~0x4, "CYA_0 bit2=0"): return False, False, True
    v = rd(0x8C300)
    if v is None: return False, False, True
    if v & 0x10:
        if not wr(0x8C300, v & ~0x10, "CYA_1 bit4=0"): return False, False, True
    v = rd(0x8C040)
    if v is None: return False, False, True
    nv = (v & ~0xC0000) | 0x40000
    if v != nv:
        if not wr(0x8C040, nv, "SPEED=5GT"): return False, False, True
    v = rd(0x8C1C0)
    if v is None: return False, False, True
    nv = (v & ~0xF0000) | 0x20000
    if v != nv:
        if not wr(0x8C1C0, nv, "LNK_CAP_SPEED=5GT"): return False, False, True
    v = rd(0x88610)
    if v is None: return False, False, True
    if not (v & 1):
        if not wr(0x88610, v | 1, "VSEC bit0=1"): return False, False, True
    time.sleep(0.05)
    l2 = cfg(s, loc, cap, 0x2C)
    gen2_ok = (l2 & 0x6) == 0x6
    log("   Gen2 声明 = %s (LNKCAP2=0x%08X)" % ("✔" if gen2_ok else "✘", l2))
    width_ok = False
    lc = cfg(s, loc, cap, 0x0C)
    width_ok = ((lc >> 4) & 0x3F) == 16
    if with_width:
        v = rd(0x8C040)
        if v is None: return gen2_ok, False, True
        if not (v & 0x80000000):
            if not wr(0x8C040, v | 0x80000000, "宽度 arm bit31=1"): return gen2_ok, False, True
        lc = cfg(s, loc, cap, 0x0C)
        width_ok = ((lc >> 4) & 0x3F) == 16
        log("   宽度声明 x16 = %s (maxwidth=%d)" % ("✔" if width_ok else "✘", (lc >> 4) & 0x3F))
    return gen2_ok, width_ok, False

def width_set(s, loc, on, force=False):
    cap = s.cap
    if on and not force:
        # 2026-10-29 实测：物理层只有 x4 时写 bit31 → 硬件尝试 x16 提交 → LTSSM 卡死掉卡
        log("!! 拒绝开启宽度 x16：未补电容/物理 lane 未通时此操作会掉卡（2026-10-29 实测）。")
        log("   确认物理层就绪后，用 width-on --force 强制执行。")
        return None
    v = s.rd(0x8C040)
    if v is None:
        return None
    nv = (v | 0x80000000) if on else (v & ~0x80000000)
    if nv != v:
        log("   写 0x8C040 = 0x%08X（宽度%s）" % (nv, "x16 声明开" if on else "声明关"))
        s.wr(0x8C040, nv, "width:%s" % ("on" if on else "off"))
    lc = cfg(s, loc, cap, 0x0C)
    return (lc >> 4) & 0x3F

def restore(s, st):
    ok = True
    for off, key, note in ((0x8C040, "base040", "0x8C040"),
                           (0x8C1C0, "base1c0", "0x8C1C0"),
                           (0x8841C, "base8841", "0x8841C"),
                           (0x8872C, "base8872", "0x8872C"),
                           (0x2241C, "base2241", "0x2241C"),
                           (0x88610, "base88610", "0x88610")):
        if st.get(key) is not None:
            s.wr(off, st[key], "restore"); time.sleep(0.03)
            rb = s.rd(off)
            ok &= (rb == st[key])
            log("   restore %s = 0x%08X -> 0x%08X %s" % (note, st[key], rb if rb is not None else 0xFFFFFFFF,
                                                         "✓" if rb == st[key] else "✗"))
    return ok

# ── 命令文件（单写者队列）──
def cmd_path(name):
    return os.path.join(CMDDIR, "cmd_" + name)

def queue_cmd(name):
    try:
        with open(cmd_path(name), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return False

def take_cmd():
    for name in ("retrain", "width_on", "width_off"):
        p = cmd_path(name)
        if os.path.exists(p):
            try: os.remove(p)
            except Exception: pass
            return name
    return None

def run_cmd_in_session(s, loc, st, name):
    if name == "retrain":
        retrain(s); time.sleep(1.0)
        lc, l2, ls, _ = report(s, loc, s.cap)
        log("命令执行: retrain -> LNKSTA speed=%d" % (ls & 0xF))
    elif name == "width_on":
        w = width_set(s, loc, True, force=True)
        log("命令执行: 宽度 x16 声明开 -> maxwidth=%s" % (w,))
    elif name == "width_off":
        w = width_set(s, loc, False)
        log("命令执行: 宽度声明关 -> maxwidth=%s" % (w,))

# ── 启动/看门狗主循环 ──
def cmd_run(seconds=0, delay=60, with_width=False):
    clean_stale_lock()
    if not hwlock.acquire("width16-hold", "P104 解锁保持(Gen2 默认; 宽度 x16 手动)", minutes=1440):
        log("!! 拿不到硬件锁（可能有其它硬件实验在跑），退出")
        return 1
    s = None
    t0 = time.time()
    last_renew = time.time()
    last_retrain = 0.0
    retrain_count = 0
    st = load_state()
    last_log = 0
    last_fail_log = 0
    last_slow = 0
    try:
        if seconds == 0 and delay > 0:
            log("启动延迟 %d 秒（等登录后驱动初始化完成）..." % delay)
            end = time.time() + delay
            while time.time() < end and not (seconds and time.time() - t0 >= seconds):
                time.sleep(1.0)
        log("保持器启动（Gen2 默认; 宽度手动; 单写者模式）")
        while True:
            if time.time() - last_renew > 600:
                try: hwlock.renew("width16-hold", 1440)
                except Exception: pass
                last_renew = time.time()
            if s is None:
                s = open_session(retries=1)
                if not s:
                    if time.time() - last_fail_log > 15:
                        log("!! 连不上 P104 或驱动装载失败（重试中）")
                        last_fail_log = time.time()
                    time.sleep(2); continue
                log("已连上 P104 %02X:%02X.%d" % s.loc)
                # 首次施加：默认只 Gen2；宽度 x16 必须显式 --with-width（未补电容会掉卡）
                g2, w16, lost = apply_gen2(s, s.loc, st, with_width=with_width)
                if lost:
                    log("!! 施加过程中设备失去响应（可能掉卡）→ 关闭句柄、等待恢复；请冷启动")
                    try: s.close()
                    except Exception: pass
                    s = None
                    time.sleep(3)
                    continue
                lc, l2, ls, _ = report(s, s.loc, s.cap)
                if (ls & 0xF) < 2:
                    retrain(s); time.sleep(1.0); last_retrain = time.time()
                    ls = cfg(s, s.loc, s.cap, 0x12)
                    log("启动 retrain -> LNKSTA speed=%d" % (ls & 0xF))
            try:
                if not alive(s, s.loc):
                    log("!! 设备失去响应（掉卡/被复位）→ 关闭句柄，等待手动恢复（冷启动）")
                    try: s.close()
                    except Exception: pass
                    s = None
                    time.sleep(3)
                    continue
                # 1) 处理排队命令（单写者）
                c = take_cmd()
                if c:
                    run_cmd_in_session(s, s.loc, st, c)
                # 2) 声明保持：Gen2 声明被改回则重写（频率限制）
                if time.time() - last_slow > 5:
                    l2 = cfg(s, s.loc, s.cap, 0x2C)
                    if (l2 & 0x6) != 0x6 and time.time() - last_retrain > 5:
                        log("检测到 Gen2 声明丢失（LNKCAP2=0x%08X），重新施加" % l2)
                        g2, w16, lost = apply_gen2(s, s.loc, st, with_width=False)
                        if lost:
                            try: s.close()
                            except Exception: pass
                            s = None
                            continue
                    last_slow = time.time()
                # 3) 链路速率检查：没到 Gen2 时重训（30s 一次、最多 3 次）
                if (time.time() - last_retrain > 30) and retrain_count < 3:
                    ls = cfg(s, s.loc, s.cap, 0x12)
                    if (ls & 0xF) < 2:
                        retrain(s); time.sleep(0.8)
                        last_retrain = time.time(); retrain_count += 1
                        ls2 = cfg(s, s.loc, s.cap, 0x12)
                        log("链路未在 Gen2（speed=%d）→ retrain（第 %d 次）→ speed=%d" % (ls & 0xF, retrain_count, ls2 & 0xF))
                # 4) 周期日志
                if time.time() - last_log > 30:
                    lc, l2, ls, lst2 = report(s, s.loc, s.cap)
                    log("状态: 声明Gen2=%s 声明x16=%s 链路speed=%d width=%d" % (
                        "✔" if (l2 & 0x6) == 0x6 else "✘",
                        "✔" if ((lc >> 4) & 0x3F) == 16 else "✘",
                        ls & 0xF, (ls >> 4) & 0x3F))
                    last_log = time.time()
            except Exception as e:
                log("!! 异常: %s -> 重新连接" % e)
                try: s.close()
                except Exception: pass
                s = None
            if seconds and time.time() - t0 >= seconds:
                log("到达 --seconds 时限，恢复并退出")
                try:
                    restore(s, st)
                except Exception: pass
                return 0
            time.sleep(0.2)
    finally:
        try:
            if s: s.close()
        except Exception: pass
        hwlock.release("width16-hold")

# ── CLI ──
def cmd_status():
    s = open_session()
    if not s:
        log("!! 找不到 P104（可能驱动未装/卡不在/掉卡）"); return 2
    try:
        log("P104 @ %02X:%02X.%d" % s.loc)
        report(s, s.loc, s.cap)
        return 0
    finally:
        s.close()

def cmd_once(with_width=False, do_retrain=True):
    s = open_session()
    if not s:
        log("!! 找不到 P104"); return 2
    try:
        st = load_state()
        g2, w16, lost = apply_gen2(s, s.loc, st, with_width=with_width)
        if lost:
            log("!! 施加过程中设备失去响应 —— 停止（可能需要冷启动）")
            return 3
        lc, l2, ls, _ = report(s, s.loc, s.cap)
        if do_retrain:
            retrain(s); time.sleep(1.0)
            lc, l2, ls, _ = report(s, s.loc, s.cap)
            log("retrain 已下发")
        log("结果: 声明Gen2=%s 声明x16=%s 链路speed=%d width=%d" % (
            "✔" if (l2 & 0x6) == 0x6 else "✘",
            "✔" if ((lc >> 4) & 0x3F) == 16 else "✘",
            ls & 0xF, (ls >> 4) & 0x3F))
        return 0 if (l2 & 0x6) == 0x6 else 1
    finally:
        s.close()

def _queue_or_direct(name, force=False):
    """若保持进程在跑 → 写命令文件；否则自己开 session 执行"""
    if name == "width_on" and not force:
        log("!! 未加 --force：拒绝开启宽度 x16（未补电容/物理 lane 未通时会掉卡，2026-10-29 实测）。")
        log("   确认物理层就绪后再执行: width-on --force")
        return 1
    h = lock_holder()
    if h and h[0] == "width16-hold" and h[1] and _pid_alive(h[1]):
        if queue_cmd(name):
            log("已排队给保持进程执行: %s（结果见日志 %s）" % (name, LOGF))
            t0 = time.time()
            while time.time() - t0 < 8:
                time.sleep(0.3)
                if not os.path.exists(cmd_path(name)):
                    log("保持进程已执行: %s" % name)
                    return 0
            log("保持进程暂未取走命令，稍后自动执行（结果见日志）")
            return 0
        log("!! 无法写命令文件"); return 1
    # 直接执行
    s = open_session()
    if not s:
        log("!! 找不到 P104"); return 2
    try:
        st = load_state()
        if name == "retrain":
            retrain(s); time.sleep(1.0); log("retrain 已下发")
        elif name == "width_on":
            w = width_set(s, s.loc, True, force=True); log("宽度 x16 声明开 -> maxwidth=%s" % (w,))
            st["want_width"] = True; save_state(st)
        elif name == "width_off":
            w = width_set(s, s.loc, False); log("宽度声明关 -> maxwidth=%s" % (w,))
            st["want_width"] = False; save_state(st)
        report(s, s.loc, s.cap)
        return 0
    finally:
        s.close()

def cmd_decl16(on=True):
    """只改「声明最大宽度」——2026-10-28 实测的安全配方（不碰 Gen2/不触发链路重训）：
       0x8872C 低4位=6；0x8C040 = bit31=1 + SPEED 固定 Gen1(2) + 清 bits27-31
       → LnkCap maxwidth 由 x4 变 x16（maxspeed 仍是 Gen1），实测可逆、不掉卡。
       注意：不要在这个状态后再去跑 Gen2 的 retrain（会把 SPEED 改到 1，可能触发 x16 提交导致掉卡）。"""
    s = open_session()
    if not s:
        log("!! 找不到 P104"); return 2
    try:
        v = s.rd(0x8872C)
        if v is None:
            log("!! 读 0x8872C 失败"); return 2
        s.wr(0x8872C, (v & ~0xF) | 6, "decl16:override=6"); time.sleep(0.03)
        if on:
            cur = s.rd(0x8C040)
            if cur is None:
                log("!! 读 0x8C040 失败"); return 2
            new = (cur & ~0xF8000000 & ~0xC0000) | (2 << 18) | 0x80000000
            log("   写 0x8C040 = 0x%08X（bit31=1, SPEED=2_5GT 固定）" % new)
            s.wr(0x8C040, new, "decl16:on"); time.sleep(0.05)
        else:
            st = load_state()
            base = st.get("base040", 0x20085800)
            log("   写 0x8C040 = 0x%08X（恢复工厂基线）" % base)
            s.wr(0x8C040, base, "decl16:off"); time.sleep(0.05)
        lc = cfg(s, s.loc, s.cap, 0x0C); l2 = cfg(s, s.loc, s.cap, 0x2C)
        ls = cfg(s, s.loc, s.cap, 0x12)
        log("   LnkCap=0x%08X (maxspeed=%d, maxwidth=x%d)  LnkCap2=0x%08X  LnkSta=0x%08X (speed=%d, width=%d)" % (
            lc, lc & 0xF, (lc >> 4) & 0x3F, l2, ls, ls & 0xF, (ls >> 4) & 0x3F))
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
        st["want_width"] = False; save_state(st)
        report(s, s.loc, s.cap)
        log("restore %s" % ("✓" if ok else "✗"))
        return 0 if ok else 1
    finally:
        s.close()

def ensure_drivers():
    for name, path in (("P104LibWr", L.WR), ("P104LibTs", L.TS)):
        exists = False
        scm = _scm()
        if scm:
            try:
                svc = _adv.OpenServiceW(scm, name, SERVICE_ALL_ACCESS)
                if svc:
                    exists = True
                    _adv.StartServiceW(svc, 0, None)
                    _adv.CloseServiceHandle(svc)
            finally:
                _adv.CloseServiceHandle(scm)
        if not exists:
            svc_create_start(name, path)
        try:
            subprocess.run(["sc.exe", "config", name, "start=", "auto"], capture_output=True, timeout=15)
        except Exception:
            pass
        log("驱动 %s: OK" % name)

def register_task(delay=60):
    tr = '"%s" "%s" run --delay %d' % (PYW, SELF, delay)
    r = subprocess.run(["schtasks", "/create", "/tn", TASK, "/tr", tr, "/sc", "onlogon",
                        "/ru", "SYSTEM", "/rl", "highest", "/f"], capture_output=True, text=True)
    log("计划任务注册: rc=%d %s%s" % (r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()))
    return r.returncode == 0

def cmd_install(delay=60):
    ensure_drivers()
    if not register_task(delay):
        return 1
    log("任务已注册（登录后延迟 %d 秒启动；默认只做 Gen2，宽度用 width-on 手动开）" % delay)
    return 0

def cmd_oneclick(delay=60):
    log("=" * 64)
    log("P104-100  一键解锁（Gen2 x4 默认；宽度 x16 手动开关）")
    log("=" * 64)
    ensure_drivers()
    register_task(delay)
    log("立即施加 Gen2（不碰宽度声明）...")
    rc = cmd_once(with_width=False, do_retrain=True)
    if rc == 3:
        log("!! 施加过程中掉卡，请冷启动；任务已注册但会在开机后延迟再试")
        return 3
    log("完成。若要实验宽度 x16：运行 width-on；撤销：width-off；完全卸载：uninstall")
    return rc

def cmd_uninstall():
    subprocess.run(["schtasks", "/end", "/tn", TASK], capture_output=True)
    r = subprocess.run(["schtasks", "/delete", "/tn", TASK, "/f"], capture_output=True, text=True)
    log("schtasks delete rc=%d %s%s" % (r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()))
    rc = cmd_restore()
    for name in ("P104LibWr", "P104LibTs"):
        svc_stop(name); time.sleep(0.2); svc_delete(name)
        log("驱动服务 %s 已停止并删除" % name)
    return 0 if r.returncode == 0 else 1

def main(argv):
    sub = argv[1] if len(argv) > 1 else "status"
    def opt(name, default=0):
        if name in argv:
            try: return int(argv[argv.index(name) + 1])
            except Exception: return default
        return default
    if sub == "status":   return cmd_status()
    if sub == "once":     return cmd_once(with_width="--with-width" in argv,
                                          do_retrain="--no-retrain" not in argv)
    if sub == "oneclick": return cmd_oneclick(delay=opt("--delay", 60))
    if sub == "retrain":  return _queue_or_direct("retrain")
    if sub == "width-on": return _queue_or_direct("width_on", force="--force" in argv)
    if sub == "width-off":return _queue_or_direct("width_off")
    if sub == "decl16":   return cmd_decl16(True)
    if sub == "decl16-off": return cmd_decl16(False)
    if sub == "restore":  return cmd_restore()
    if sub == "install":  return cmd_install(delay=opt("--delay", 60))
    if sub == "uninstall": return cmd_uninstall()
    if sub == "run":
        return cmd_run(seconds=opt("--seconds", 0), delay=opt("--delay", 60),
                       with_width="--with-width" in argv)
    print(__doc__); return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
