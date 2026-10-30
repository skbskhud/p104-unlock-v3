#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""apply_gen2_width.py —— P104-100 (GP104) Gen2 x4 解锁 + LnkCap 宽度声明（单文件 CLI）

================================================================================
★ 研究用工具 · 只对你【自己拥有】的硬件使用 ★
--------------------------------------------------------------------------------
本文具写的是 GPU 的【易失寄存器】（BAR0 MMIO + PCIe 配置空间）：
    · 不刷 VBIOS、不动熔丝、不改驱动策略注册表
    · 掉卡 / 断电后自动恢复；最坏情况需要 AC 断电（拔电源 30 秒）再开机
    · 只应在你能接受"要拔电源"的测试机上运行
★ 默认动作是【只读】（--read-only）：只快照 + 判定，不写任何东西。
  真正写硬件必须显式加 --apply；加 --dry-run 可以先看"将会写什么"。
★ 所有写操作都有保护：
    1) 写前活体检查（读 config 0x00 三次）
    2) 写后回读校验
    3) 非法 0x8C040 值直接拒绝（bit31=0 且 bit29=0 / bit31=1 且 bit29=1 都会掉卡）
    4) 掉卡（config 0x00 连读失败）立即停止并打印 AC 断电指引
★ 写寄存器之前会先拿 hwlock 硬件锁，避免两个进程同时写而掉卡。
================================================================================

用法（推荐顺序）：
    python read_probe.py                                   # 1) 先只读探针
    python apply_gen2_width.py                             # 2) 默认只读快照
    python apply_gen2_width.py --apply --dry-run           # 3) 看写入计划（不写）
    python apply_gen2_width.py --apply                     # 4) 施加 live Gen2 写组 + TLS/retrain
    python apply_gen2_width.py --apply --width on          # 5) Gen2 成功后再写宽度声明 x16
    python apply_gen2_width.py --apply --width off         # 回退宽度声明（整字写安全值）

参数：
    --apply               显式允许写硬件（默认只读，必须显式传）
    --width on|off        宽度声明（写操作，必须同时有 --apply）
    --timeout N           写后观察窗口秒数（默认 20）
    --dry-run             只打印将写入的寄存器序列，不写
    --drivers DIR         BYOVD 驱动目录（含 WinRing0x64.sys / ThrottleStop.sys）
    --device VVVV:DDDD    目标设备（默认 10de:1b87）
    --json-out FILE       结果写 JSON 文件
    --quiet               不打印人类可读报告（只打日志）
    --no-lock             不拿硬件锁（★ 只在你确认没有别的进程在写时用）

退出码：
    0  只读完成 / 已达成目标（PASS）
    1  写完了但没达成（FAIL：声明或链路没上去）
    2  找不到 BYOVD 驱动或设备
    3  掉卡（设备从总线消失）—— 需要 AC 断电
    4  拿不到硬件锁（有别的进程在写硬件）
    5  安全拒绝（非法 0x8C040 值 / 非白名单寄存器）
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _preparse_drivers(argv=None):
    """在 import lib_p104 之前解析 --drivers（驱动路径在模块 import 时就解析了）。"""
    argv = sys.argv if argv is None else argv
    for i, a in enumerate(argv):
        if a == "--drivers" and i + 1 < len(argv):
            os.environ["P104_BYOVD_DIR"] = os.path.abspath(argv[i + 1])
        elif a.startswith("--drivers="):
            os.environ["P104_BYOVD_DIR"] = os.path.abspath(a.split("=", 1)[1])


_preparse_drivers()

import lib_p104 as L
import lib_session
import hwlock
import read_probe as P

# ---------------------------------------------------------------- 常量 --
# ★ 只有这些 BAR0 偏移动允许写（其余一律拒绝，防止手滑写坏别的东西）
WRITE_WHITELIST = {
    0x8872C, 0x2241C, 0x8841C, 0x8C2C0, 0x8C300, 0x8C040, 0x8C1C0, 0x88610,
}

# live Gen2 写组：顺序照 2026-10-28/29 实测生效的 width16_hold.py:291-323，
# 与 C# v1.1 ApplyLiveGroup() 完全一致（★ 顺序不能改）。
#   (偏移, 掩码, 目标值, 说明)
LIVE_GROUP = [
    (0x8872C, 0x0000000F, 0x00000006, "NV_XVE_FUSE_OVERRIDE=6 (CHANGE_SPEED_WIDTH=3)"),
    (0x2241C, 0x00000081, 0x00000081, "NV_PTOP_FS4 bits0,7=1（live 声明开关）"),
    (0x8841C, 0x00006000, 0x00000000, "清 NV_XVE_PRIV_MISC_1 bits14:13 (GEN2_DIS=0)"),
    (0x8C2C0, 0x00000004, 0x00000000, "NV_XP_PL_CYA_0 bit2=0"),
    (0x8C300, 0x00000010, 0x00000000, "NV_XP_PL_CYA_1 bit4=0"),
    (0x8C040, 0x000C0000, 0x00040000, "SPEED[19:18]=1 (5.0GT/s)"),
    (0x8C1C0, 0x000F0000, 0x00020000, "LCTRL_2[19:16]=2 (5.0GT/s)"),
    (0x88610, 0x00001001, 0x00000001, "VSEC_HIER bit0=1, bit12=0"),
]

REG_NAMES = {
    0x8872C: "NV_XVE_FUSE_OVERRIDE",
    0x2241C: "NV_PTOP_FS4",
    0x8841C: "NV_XVE_PRIV_MISC_1",
    0x8C2C0: "NV_XP_PL_CYA_0",
    0x8C300: "NV_XP_PL_CYA_1",
    0x8C040: "NV_XP_PL_LINK_CONFIG_0",
    0x8C1C0: "NV_XP_PL_LCTRL_2",
    0x88610: "NV_XVE_VSEC_HIERARCHY",
}

LINKCFG0 = 0x8C040
LINKCFG0_SAFE = 0x20085800      # 0x8C040 工厂安全整字（bit31=0, bit29=1, SPEED=2.5GT）
WIDTH_ARM = 0x80000000          # 宽度 arm/commit 位

PHYS_ID = 0x1B8710DE
T_LIVE_ROUNDS = 3               # live 写组最多重试轮数
T_LIVE_LATCH = 2.0              # live 写组后等 LCAP2 的窗口（实测 ≤1s）
T_LIVE_RETRY_GAP = 0.3
T_RETRAIN_ROUNDS = 3            # TLS + retrain 最多轮数
T_TRIGGER_MIN_GAP = 2.0         # 两次 retrain 的最小间隔（C# v1.1 触发限流）
AC_HINT = """
================================================================================
!! 卡已从 PCIe 总线上掉了（掉卡）—— 立即停止一切写入，不要反复重试
   1) 关闭所有 P104 工具（包括常驻按住进程）
   2) 关机，拔掉电源线（笔记本：拔适配器 + 按住电源键 10 秒），等 30 秒以上
   3) 掉卡后内核里的 PCIe 链路状态只有 AC 断电才能彻底复位
   4) 开机后先跑:  python read_probe.py
      确认能看到 10DE:1B87 再考虑下一步
   5) 如果多次掉卡：先用 C# v1.1 (P104Gen2x4.exe -dry-run / -status) 交叉验证，
      并检查散热/供电/PCIe 槽接触
================================================================================
"""


# ---------------------------------------------------------------- 异常 --
class DeviceLost(Exception):
    """config 0x00 连读失败 == 掉卡。必须立即停止一切操作。"""


class UnsafeValue(Exception):
    """要写入的值不合法（例如非法 0x8C040）—— 安全拒绝。"""


# ---------------------------------------------------------------- 日志 --
_LOG = []


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    _LOG.append(line)


def log_plan(msg):
    print("    " + msg, flush=True)


# ---------------------------------------------------------------- Applier --
class Applier(object):
    """所有写操作的唯一入口：写前活体检查 + 白名单 + 0x8C040 合法性 + 写后回读。"""

    def __init__(self, s, loc, dry=False):
        self.s = s
        self.loc = loc
        self.cap = s.cap
        self.dry = dry
        self.journal = []
        self.lost = False

    # ---------- 活体 / 读 ----------
    def alive(self):
        return P.alive3(self.s, self.loc)

    def require_alive(self, when):
        if not self.alive():
            self.lost = True
            raise DeviceLost(when)

    def read_reg(self, off):
        """读 BAR0。读前先活体检查 —— 掉卡后读 BAR0 会挂死（10-29 教训）。"""
        self.require_alive("读 BAR0 0x%06X 前" % off)
        for _ in range(2):
            v = self.s.rd(off)
            if v is not None:
                return v
            time.sleep(0.05)
        self.lost = True
        raise DeviceLost("读 BAR0 0x%06X 失败（映射失败/掉卡）" % off)

    def _prd_retry(self, ep, reg, tries=3, gap=0.05):
        """读配置空间，失败（通道错误）重试几次 —— retrain 瞬间可能短暂读不到。
        ★ 返回 0xFFFFFFFF 说明设备已从总线消失（真·掉卡），由调用方判定。"""
        if ep is None:
            return None
        for _ in range(tries):
            v = self.s.prd(ep[0], ep[1], ep[2], reg)
            if v is not None:
                return v
            time.sleep(gap)
        self.lost = True
        raise DeviceLost("读配置空间 %02X:%02X.%d reg=0x%02X 失败"
                         % (ep[0], ep[1], ep[2], reg))

    def link(self, cap_off):
        v = self._prd_retry(self.loc, self.cap + cap_off)
        if v == 0xFFFFFFFF:
            self.lost = True
            raise DeviceLost("读配置空间 cap+0x%02X 返回全 1（设备可能已掉卡）" % cap_off)
        return v

    def root_link(self, cap_off):
        if not self.s.root or self.s.root_cap is None:
            return None
        return P.cfg(self.s, self.s.root, self.s.root_cap, cap_off)

    def root_tls(self):
        v = self.root_link(0x30)
        return None if v is None or v == 0xFFFFFFFF else (v & 0xF)

    # ---------- 写 ----------
    def write_reg(self, off, val, note):
        if off not in WRITE_WHITELIST:
            raise UnsafeValue("拒绝写非白名单寄存器 0x%06X" % off)
        val &= 0xFFFFFFFF
        if off == LINKCFG0 and not P.is_8c040_legal(val):
            raise UnsafeValue(
                "拒绝写入非法 0x8C040 值 0x%08X（bit31=0 且 bit29=0 实测掉卡 2/2；"
                "bit31=1 且 bit29=1 实测掉卡 1/1）" % val)
        self.require_alive("写 BAR0 0x%06X 前" % off)
        old = self.read_reg(off)
        if old == val:
            log("   0x%06X 已是 0x%08X，跳过 [%s]" % (off, val, note))
            return True
        if self.dry:
            raise RuntimeError("dry-run 模式禁止写硬件（内部错误）")
        log("   写 0x%06X: 0x%08X -> 0x%08X  [%s]" % (off, old, val, note))
        if not self.s.wr(off, val, note):
            self.lost = True
            raise DeviceLost("写 BAR0 0x%06X 失败" % off)
        time.sleep(0.02)
        rb = self.read_reg(off)            # 写后活体 + 回读
        self.journal.append({"kind": "bar0", "offset": "0x%06X" % off,
                             "name": REG_NAMES.get(off, ""),
                             "old": "0x%08X" % old, "new": "0x%08X" % val,
                             "readback": "0x%08X" % rb, "ok": rb == val, "note": note})
        if rb != val:
            log("   !! 回读不一致：期望 0x%08X 实际 0x%08X（可能被硬件忽略，继续观察）" % (val, rb))
            return False
        return True

    def rmw(self, off, mask, value, note):
        """读-改-写：old -> (old & ~mask) | (value & mask)，20ms 后回读校验。"""
        old = self.read_reg(off)
        nv = (old & ~mask & 0xFFFFFFFF) | (value & mask)
        if nv == old:
            log("   0x%06X 已是目标状态（0x%08X），跳过 [%s]" % (off, old, note))
            return True
        return self.write_reg(off, nv, note)

    def pci_write(self, ep, reg, val, note):
        """写 PCIe 配置空间（TLS / LNKCTL.retrain 用）。写前活体、写后回读。"""
        self.require_alive("写配置空间 %02X:%02X.%d reg=0x%02X 前" % (ep[0], ep[1], ep[2], reg))
        old = self._prd_retry(ep, reg)
        if old is None:
            raise DeviceLost("读配置空间 %02X:%02X.%d reg=0x%02X 失败" % (ep[0], ep[1], ep[2], reg))
        if old == val:
            return True
        if self.dry:
            raise RuntimeError("dry-run 模式禁止写硬件（内部错误）")
        log("   写配置 %02X:%02X.%d reg=0x%02X: 0x%08X -> 0x%08X  [%s]"
            % (ep[0], ep[1], ep[2], reg, old, val, note))
        if not self.s.pwr(ep[0], ep[1], ep[2], reg, val):
            self.lost = True
            raise DeviceLost("写配置空间 %02X:%02X.%d reg=0x%02X 失败" % (ep[0], ep[1], ep[2], reg))
        time.sleep(0.02)
        rb = self._prd_retry(ep, reg)
        if rb is None:
            self.lost = True
            raise DeviceLost("写后回读配置空间失败")
        self.journal.append({"kind": "pci", "bdf": "%02X:%02X.%d" % (ep[0], ep[1], ep[2]),
                             "reg": "0x%02X" % reg, "old": "0x%08X" % old,
                             "new": "0x%08X" % val, "readback": "0x%08X" % rb,
                             "ok": (rb & 0xFFFF) == (val & 0xFFFF), "note": note})
        return True

    # ---------- 观察 ----------
    def observe(self, timeout, predicate, what=""):
        """轮询观察（只读）。掉卡立即抛 DeviceLost。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if predicate():
                return True
            time.sleep(0.1)
        if what:
            log("   %s 在 %.1fs 内未达成" % (what, timeout))
        return False

    # ---------- 具体步骤 ----------
    def live_group(self):
        """无复位 live 写组（8 个寄存器，顺序固定）。"""
        ok = True
        for off, mask, val, note in LIVE_GROUP:
            ok &= self.rmw(off, mask, val, note)
        return ok

    def live_latch(self):
        """写组 + 观察 LNKCAP2 是否秒级变成 Gen2（0x06）。最多 3 轮。"""
        for rnd in range(1, T_LIVE_ROUNDS + 1):
            self.require_alive("live 写组第 %d 轮前" % rnd)
            log(" [live 写组] 第 %d/%d 轮" % (rnd, T_LIVE_ROUNDS))
            self.live_group()
            if self.observe(T_LIVE_LATCH,
                            lambda: (self.link(0x2C) & 0x6) == 0x6,
                            "LNKCAP2 Gen2 声明"):
                l2 = self.link(0x2C)
                log("  ★ 声明已锁存为 Gen2（LNKCAP2=0x%08X）" % l2)
                return True
            log("  第 %d 轮未锁存（LNKCAP2=0x%08X），%.1fs 后重试"
                % (rnd, self.link(0x2C), T_LIVE_RETRY_GAP))
            time.sleep(T_LIVE_RETRY_GAP)
        return False

    def set_tls_both(self):
        """两端 LNKCTL2 目标速率 = 2 (5.0GT/s)。"""
        eps = [("GPU", self.loc, self.cap)]
        if self.s.root:
            eps.append(("根端口", self.s.root, self.s.root_cap))
        for tag, ep, cap in eps:
            if cap is None:
                continue
            cur = self._prd_retry(ep, cap + 0x30)
            if (cur & 0xF) != 2:
                self.pci_write(ep, cap + 0x30, (cur & ~0xF) | 2, "%s LNKCTL2 TLS=2" % tag)
            else:
                log_plan("%s LNKCTL2 已经是 TLS=2" % tag)

    def root_retrain(self):
        """根端口 LNKCTL.bit5=1 触发 retrain（★ 与实测有效的 width16_hold.retrain 一致）。"""
        if not self.s.root:
            log("   !! 没找到上游根端口 —— 跳过 retrain（请确认卡是否插在 PCH 槽）")
            return False
        ep = self.s.root
        cap = self.s.root_cap
        if cap is None:
            log("   !! 根端口没有 PCIe 能力结构 —— 跳过 retrain")
            return False
        cur = self._prd_retry(ep, cap + 0x10)
        self.pci_write(ep, cap + 0x10, cur | (1 << 5), "根端口 LNKCTL.retrain=1")
        return True

    def disarm_width_for_speed(self):
        """宽度已 arm（bit31=1）而链路还停在 Gen1 时：先整字回退安全值再提速。
        ★ 绝不能单 bit 清 bit31（会写出 bit31=0 & bit29=0 → 实测掉卡）。"""
        cur = self.read_reg(LINKCFG0)
        if not (cur & WIDTH_ARM):
            return False
        lc = self.link(0x0C)
        if ((lc >> 4) & 0x3F) == 16 and (self.link(0x12) & 0xF) >= 2:
            return False
        log("   宽度已 arm 但链路未到 Gen2 —— 先整字回退 0x8C040=0x%08X 再提速" % LINKCFG0_SAFE)
        self.write_reg(LINKCFG0, LINKCFG0_SAFE, "整字回退宽度 arm（安全值）")
        time.sleep(0.1)
        return True

    def width_arm(self):
        """宽度声明 x16：0x8C040 = (cur & ~0xF8000000) | 0x80000000。
        ★ 只在 Gen2 已达成后调用；失败则整字回退 0x20085800。"""
        cur = self.read_reg(LINKCFG0)
        lc = self.link(0x0C)
        if (cur & WIDTH_ARM) and ((lc >> 4) & 0x3F) == 16:
            log("   宽度声明 x16 已经是生效状态（LNKCAP=0x%08X）" % lc)
            return True
        nv = (cur & ~0xF8000000 & 0xFFFFFFFF) | WIDTH_ARM
        if not P.is_8c040_legal(nv):
            raise UnsafeValue("拒绝写入非法 0x8C040 值 0x%08X" % nv)
        log("   宽度 arm: 0x%08X -> 0x%08X（清 bits31:27 再置 bit31，LNKCAP maxwidth 应变 x16）"
            % (cur, nv))
        self.write_reg(LINKCFG0, nv, "宽度 arm（bit31=1）")
        if self.observe(1.5, lambda: ((self.link(0x0C) >> 4) & 0x3F) == 16,
                        "LNKCAP maxwidth=16"):
            log("   ★ 宽度声明 x16 已生效（LNKCAP=0x%08X）" % self.link(0x0C))
            return True
        log("   !! 宽度声明未生效（LNKCAP=0x%08X）—— 整字回退 0x%08X（不影响 Gen2）"
            % (self.link(0x0C), LINKCFG0_SAFE))
        self.write_reg(LINKCFG0, LINKCFG0_SAFE, "宽度 arm 失败后整字回退")
        return False

    def width_off(self):
        """撤销宽度声明：整字写 0x20085800（★ 绝不用掩码单 bit 清 bit31）。"""
        cur = self.read_reg(LINKCFG0)
        if cur == LINKCFG0_SAFE:
            log("   0x8C040 已是安全值 0x%08X，无需回退" % LINKCFG0_SAFE)
            return True
        log("   宽度回退：整字写 0x8C040 = 0x%08X（当前 0x%08X）" % (LINKCFG0_SAFE, cur))
        self.write_reg(LINKCFG0, LINKCFG0_SAFE, "宽度声明关闭（整字安全值）")
        rb = self.read_reg(LINKCFG0)
        if rb & WIDTH_ARM:
            log("   !! 回读 bit31 仍为 1（0x%08X）—— 可能被驱动立刻改回，稍后再试" % rb)
            return False
        return True


# ---------------------------------------------------------------- 规划 --
def plan_text(app, width_action):
    """dry-run / 只读时打印"将会写什么"（只读，不写硬件）。"""
    log("[计划] 将写入的寄存器（--dry-run：只打印，不写）")
    ok = True
    for off, mask, val, note in LIVE_GROUP:
        try:
            cur = app.read_reg(off)
        except DeviceLost:
            raise
        nv = (cur & ~mask & 0xFFFFFFFF) | (val & mask)
        legal = P.is_8c040_legal(nv) if off == LINKCFG0 else True
        if off == LINKCFG0 and not legal:
            ok = False
            log_plan("0x%06X %-24s mask=0x%08X val=0x%08X 当前=0x%08X -> ★非法值 0x%08X（会被拒绝）"
                     % (off, REG_NAMES.get(off, ""), mask, val, cur, nv))
        else:
            log_plan("0x%06X %-24s mask=0x%08X val=0x%08X 当前=0x%08X -> 0x%08X  %s"
                     % (off, REG_NAMES.get(off, ""), mask, val, cur, nv,
                        "" if nv != cur else "(已是目标值，会跳过)"))

    log_plan("")
    log_plan("配置空间写入：")
    log_plan("  GPU   LNKCTL2[3:0] = 2 (5.0GT/s)   （cap@0x%02X + 0x30）"
             % (app.cap or 0))
    if app.s.root:
        log_plan("  根端口 LNKCTL2[3:0] = 2 (5.0GT/s)   （%02X:%02X.%d cap@0x%02X + 0x30）"
                 % (app.s.root[0], app.s.root[1], app.s.root[2], app.s.root_cap or 0))
        log_plan("  根端口 LNKCTL.bit5 = 1（触发 retrain）")
    else:
        log_plan("  根端口：未找到 —— 将跳过 retrain")

    if width_action == "on":
        try:
            cur = app.read_reg(LINKCFG0)
            nv = (cur & ~0xF8000000 & 0xFFFFFFFF) | WIDTH_ARM
            log_plan("")
            log_plan("宽度 arm: 0x8C040 = (0x%08X & ~0xF8000000) | 0x80000000 = 0x%08X  %s"
                     % (cur, nv, "合法" if P.is_8c040_legal(nv) else "★非法，会被拒绝"))
        except DeviceLost:
            raise
    elif width_action == "off":
        log_plan("")
        log_plan("宽度回退: 0x8C040 = 0x%08X（整字写；★ 不用掩码单 bit 清 bit31）" % LINKCFG0_SAFE)
    return ok


# ---------------------------------------------------------------- 报告 --
def result_payload(rep, mode, ap, width_action, journal, baseline, extra=None):
    out = {
        "tool": "apply_gen2_width.py",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,                       # read-only / dry-run / apply
        "options": {
            "width": width_action,
            "timeout": ap.timeout,
            "device": ap.device,
            "drivers": L.BYOVD_DIR,
            "lock_used": not ap.no_lock,
        },
        "device": rep["device"],
        "link": rep["link"],
        "root_port": rep["root_port"],
        "bar0": rep["bar0"],
        "judge": rep["judge"],
        "baseline": baseline,
        "journal": journal,
        "log": _LOG,
    }
    if extra:
        out.update(extra)
    return out


def print_result(rep, mode):
    if not rep.get("link"):
        log("!! 读不到 PCIe 能力结构（link=None），无法判定")
        return
    j = rep["judge"]
    log("")
    log("================ 结果 ================")
    log("  LNKCAP  = %s  声明: %s x%d" % (P.hx(rep["link"]["lnkcap"]),
                                        rep["link"]["max_speed_text"], rep["link"]["max_width"]))
    log("  LNKSTA  = %s  实际: %s x%d" % (P.hx(rep["link"]["lnksta"]),
                                        rep["link"]["cur_speed_text"], rep["link"]["cur_width"]))
    log("  LNKCAP2 = %s  %s" % (P.hx(rep["link"]["lnkcap2"]),
                               "含 Gen2 声明" if j.get("j1_declared_gen2") else "仅 Gen1 声明"))
    log("  LNKCTL2 TLS = %d (%s)" % (rep["link"]["tls"], rep["link"]["tls_text"]))
    log("  RESULT: %s —— %s" % (j.get("result"),
                                "Gen2 已生效" if j.get("result") == "PASS" else "未达成 Gen2"))
    if mode == "apply":
        log("  （所有写入均为易失寄存器；重启或 AC 断电即恢复出厂状态）")
    log("======================================")


# ---------------------------------------------------------------- main --
def build_parser():
    ap = argparse.ArgumentParser(
        description="P104-100 Gen2 x4 解锁 + 宽度声明（默认只读；写硬件要显式 --apply）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python apply_gen2_width.py                          # 只读快照（默认）\n"
               "  python apply_gen2_width.py --apply --dry-run        # 打印写入计划\n"
               "  python apply_gen2_width.py --apply                  # 施加 Gen2\n"
               "  python apply_gen2_width.py --apply --width on       # Gen2 + 宽度声明 x16\n"
               "  python apply_gen2_width.py --apply --width off      # 撤销宽度声明\n")
    ap.add_argument("--apply", action="store_true",
                    help="★ 允许写硬件（不传就是只读，安全默认）")
    ap.add_argument("--read-only", action="store_true",
                    help="只读快照并退出（默认行为；与 --apply 互斥）")
    ap.add_argument("--width", choices=["on", "off"], default=None,
                    help="宽度声明 on=arm x16 / off=整字回退（写操作，需要 --apply）")
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="写后观察窗口秒数（默认 20）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将写入的寄存器序列，不写硬件")
    ap.add_argument("--drivers", default=None,
                    help="BYOVD 驱动目录（含 WinRing0x64.sys / ThrottleStop.sys）")
    ap.add_argument("--device", default="10de:1b87",
                    help="目标设备 vendor:device（默认 10de:1b87）")
    ap.add_argument("--json-out", default=None, help="把结果 JSON 写到文件")
    ap.add_argument("--quiet", action="store_true", help="不打印人类可读报告")
    ap.add_argument("--no-lock", action="store_true",
                    help="★ 不拿硬件锁（只在你确认没有别的进程写硬件时用）")
    return ap


def print_ac_hint():
    print(AC_HINT, flush=True)


def main(argv=None):
    P.setup_output_encoding()
    ap = build_parser()
    a = ap.parse_args(argv)

    # ---- 参数合法性（安全默认：不写） ----
    if a.read_only and a.apply:
        ap.error("--read-only 与 --apply 互斥")
    if a.width and not a.apply:
        ap.error("--width 是写操作，必须同时加 --apply（默认只读）")
    if a.dry_run and not a.apply:
        # 允许单独 --dry-run：等价于"只读 + 打印写入计划"
        pass
    if a.timeout < 1:
        ap.error("--timeout 至少 1 秒")
    mode = "dry-run" if a.dry_run else ("apply" if a.apply else "read-only")

    if os.name != "nt":
        print("本工具只支持 Windows（BYOVD 驱动 + Win32 API）")
        return 2
    if a.drivers:
        d = os.path.abspath(a.drivers)
        if not os.path.isfile(os.path.join(d, "WinRing0x64.sys")):
            print("警告：%s 里没有 WinRing0x64.sys，驱动装载可能失败" % d, file=sys.stderr)
    try:
        vendor, device = [int(x, 16) for x in a.device.split(":")]
    except Exception:
        ap.error("--device 格式应为 VVVV:DDDD，例如 10de:1b87")

    # ---- 头 ----
    log("=" * 72)
    log("P104-100 Gen2 解锁工具  模式=%s  %s" % (mode, time.strftime("%Y-%m-%d %H:%M:%S")))
    log("研究用工具：只对自己拥有的硬件使用；写的是易失寄存器；掉卡请 AC 断电")
    log("=" * 72)

    # ---- 打开通道 ----
    # 要写才探测写通道；只读/dry-run 不探测（不做任何写 IOCTL）
    detect = a.apply and not a.dry_run
    log("打开 BYOVD 通道（detect_write=%s）…" % detect)
    s = lib_session.open_session(detect_write=detect, verbose=True,
                                 vendor=vendor, device=device)
    if s is None:
        log("!! 打不开 BYOVD 通道或找不到 %04X:%04X。检查：管理员权限 / 卡在不在 / 驱动目录 %s"
            % (vendor, device, L.BYOVD_DIR))
        return 2
    if a.apply and not a.dry_run and s.write_ioctl is None:
        log("!! 探测不到可用的配置空间写通道（write_ioctl=None）—— 只读写，拒绝继续写硬件")
        log("   提示：可能需要管理员权限；或另一个 P104 工具正占着驱动")
        s.close()
        return 2
    if s.loc is None or s.cap is None:
        log("!! 找到设备但读不到 PCIe 能力结构（loc=%s cap=%s）—— 退出" % (s.loc, s.cap))
        s.close()
        return 2

    # dry 标志：read-only / dry-run 时任何写尝试都会抛异常（双保险）
    app = Applier(s, s.loc, dry=(a.dry_run or not a.apply))
    baseline = {}
    rc = 0
    lock_ok = False
    try:
        # ---- 起点快照（只读） ----
        rep0 = P.build_report(s, vendor, device)
        if not a.quiet:
            P.print_human(rep0)
        ls = rep0["link"] or {}
        j = rep0["judge"]
        log("起点：声明Gen2=%s 链路speed=%d width=%d TLS=%d | 宽度max=x%d | %s"
            % ("√" if ls.get("lcap2_gen2") else "×", ls.get("cur_speed", 0),
               ls.get("cur_width", 0), ls.get("tls", 0), ls.get("max_width", 0), j["result"]))

        need_speed = not (j.get("j1_declared_gen2") and j.get("j2_link_gen2"))
        need_width_arm = (a.width == "on") and ls.get("max_width") != 16
        need_width_off = (a.width == "off") and ls.get("max_width") == 16

        if not a.apply:
            if a.dry_run:
                log("dry-run（未加 --apply）：只打印写入计划，绝不写硬件")
                plan_text(app, a.width)
            else:
                log("只读模式：不写任何寄存器。")
                log("（要解锁：先 --apply --dry-run 看计划，再 --apply）")
            payload = result_payload(rep0, mode, a, a.width, [], baseline)
            if a.json_out:
                with open(a.json_out, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                log("JSON 已写入 %s" % a.json_out)
            return 0

        # ---- 已达成目标 → 不写 ----
        if not need_speed and not need_width_arm and not need_width_off:
            log("★ 已是目标状态（Gen2 + 宽度声明符合要求）—— 不写任何寄存器")
            log("  （如需重新施加，先 --apply --width off 或重启后等待驱动复位）")
            payload = result_payload(rep0, mode, a, a.width, [], baseline)
            if a.json_out:
                with open(a.json_out, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            return 0

        # ---- 拿硬件锁 ----
        if not a.no_lock:
            hwlock.clean_stale("apply_gen2_width")
            mins = int(max(5, a.timeout / 60.0 + 5))
            if not hwlock.acquire("apply_gen2_width",
                                  "P104 Gen2 live 写组%s" % (" + 宽度声明" if a.width else ""),
                                  minutes=mins):
                log("!! 拿不到硬件锁（可能有别的进程在写硬件）—— 退出，不写")
                return 4
            lock_ok = True
        else:
            log("!! --no-lock：跳过硬件锁（自己确认没有并发写）")

        for off in (x[0] for x in LIVE_GROUP):
            baseline["0x%06X" % off] = "0x%08X" % app.read_reg(off)
        log("基线（写前）：" + " ".join("%s=0x%08X" % (k, int(v, 16)) for k, v in baseline.items()))
        if app.s.root:
            log("根端口 TLS 基线 = %s" % app.root_tls())

        # ---- dry-run ----
        if a.dry_run:
            plan_text(app, a.width)
            log("dry-run：未写任何硬件。确认无误后去掉 --dry-run 再执行。")
            payload = result_payload(rep0, mode, a, a.width, [], baseline)
            if a.json_out:
                with open(a.json_out, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                log("JSON 已写入 %s" % a.json_out)
            return 0

        # ================= 真正写入 =================
        app.require_alive("施加前")

        if need_speed:
            # 先解除可能存在的宽度 arm（armed 状态下 retrain 升不了速 — v1.1 实测）
            app.disarm_width_for_speed()

            # S2 无复位 live 写组
            locked = app.live_latch()
            if not locked:
                log("!! live 写组 3 轮都没把 LNKCAP2 翻到 Gen2 —— 仍继续尝试 TLS/retrain 后判定")
                log("   （本工具不做整卡复位；复位路径请用 C# v1.1 P104Gen2x4.exe）")

            # S4 两端 TLS=2 + 根端口 retrain（最多 3 轮，轮间隔 >=2s 触发限流）
            per_round = max(1.0, a.timeout / float(T_RETRAIN_ROUNDS))
            last_trigger = 0.0
            link_ok = False
            for rnd in range(1, T_RETRAIN_ROUNDS + 1):
                gap = time.time() - last_trigger
                if gap < T_TRIGGER_MIN_GAP:
                    time.sleep(T_TRIGGER_MIN_GAP - gap)
                log(" [TLS/retrain] 第 %d/%d 轮" % (rnd, T_RETRAIN_ROUNDS))
                app.set_tls_both()
                app.root_retrain()
                last_trigger = time.time()
                if app.observe(per_round, lambda: (app.link(0x12) & 0xF) >= 2,
                               "链路 >= 5.0GT/s"):
                    link_ok = True
                    break
                log("  第 %d 轮后 LNKSTA=0x%08X（speed=%d）"
                    % (rnd, app.link(0x12), app.link(0x12) & 0xF))
                time.sleep(1.0)

            # 链路没上去 → 补一轮 live 写组 + TLS/retrain（C# v1.1 的"补刀"，不复位）
            if not link_ok:
                log("!! 声明有了但链路没上 Gen2 —— 补一轮 live 写组 + retrain")
                if app.live_latch():
                    app.disarm_width_for_speed()
                    app.set_tls_both()
                    app.root_retrain()
                    app.observe(max(2.0, a.timeout / 2.0),
                                lambda: (app.link(0x12) & 0xF) >= 2, "链路 >= 5.0GT/s")

        # S5 宽度声明 x16（只在 Gen2 达成且稳定后）
        if a.width == "on":
            if not ((app.link(0x2C) & 0x6) == 0x6 and (app.link(0x12) & 0xF) >= 2):
                log("!! 链路还没到 Gen2 —— 拒绝写宽度声明（v1.1 规矩：先 Gen2 再 arm 宽度）")
                rc = 1
            else:
                stable = True
                for _ in range(4):
                    time.sleep(0.5)
                    if not ((app.link(0x2C) & 0x6) == 0x6 and (app.link(0x12) & 0xF) >= 2):
                        stable = False
                        break
                if not stable:
                    log("!! 声明/链路不稳定 —— 本次不做宽度声明（先解决 Gen2）")
                    rc = 1
                else:
                    if not app.width_arm():
                        rc = 1
        elif a.width == "off":
            if not app.width_off():
                rc = 1

        # ---- 写后判定 ----
        rep = P.build_report(s, vendor, device)
        if not a.quiet:
            print_result(rep, mode)
        if rep["judge"]["result"] != "PASS":
            rc = 1
            log("!! 未达成 Gen2（RESULT=FAIL）。诊断建议：")
            log("   - 再跑一次 --apply（live 写组可重试；NVIDIA 驱动会在 CUDA 初始化时改回 SPEED）")
            log("   - 用 C# v1.1 P104Gen2x4.exe -status / -dry-run 交叉验证")
            log("   - 若反复失败：确认卡插在 CPU 直连 x16 槽、驱动策略 RMPcieLinkSpeed=1")
        else:
            log("√ PASS —— Gen2 已生效（本次写入全部为易失寄存器，重启即恢复）")
        if a.width == "on":
            log("  宽度声明 maxwidth = x%d（x16 只是声明层；实际物理宽度仍是 %d）"
                % (rep["link"]["max_width"], rep["link"]["cur_width"]))

        extra = {"lost": False, "recovery": "重启或 AC 断电即恢复出厂寄存器状态"}
        payload = result_payload(rep, mode, a, a.width, app.journal, baseline, extra)
        if a.json_out:
            with open(a.json_out, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            log("JSON 已写入 %s" % a.json_out)
        return rc

    except DeviceLost as e:
        log("!! 掉卡：%s" % e)
        log("!! 已立即停止所有写入。")
        print_ac_hint()
        payload = {
            "tool": "apply_gen2_width.py", "mode": mode,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "lost": True, "reason": str(e),
            "journal": app.journal, "baseline": baseline, "log": _LOG,
        }
        if a.json_out:
            try:
                with open(a.json_out, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        return 3
    except UnsafeValue as e:
        log("!! 安全拒绝：%s" % e)
        log("   已停止，未写入非法值。请把这条日志原样反馈。")
        return 5
    except KeyboardInterrupt:
        log("!! 用户中断（Ctrl+C）。已停止写入。")
        print_ac_hint()
        return 3
    except Exception as e:
        log("!! 异常：%s: %s" % (type(e).__name__, e))
        log("   已停止。若链路异常，请先跑 read_probe.py，必要时 AC 断电。")
        return 1
    finally:
        try:
            s.close(unload=False)      # 不删服务，避免把别的进程的驱动弄断
        except Exception:
            pass
        if lock_ok:
            try:
                hwlock.release("apply_gen2_width")
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
