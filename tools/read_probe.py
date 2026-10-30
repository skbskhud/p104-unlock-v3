#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""read_probe.py —— P104-100 PCIe 只读探针（★ 绝对不写硬件）

================================================================================
★ 研究用工具 · 只对你【自己拥有】的硬件使用
★ 本脚本只做：
    - PCI 配置空间的【读】（WinRing0 读 IOCTL）
    - BAR0 寄存器的【读】（ThrottleStop 映射物理页后只读）
★ 不做任何写 IOCTL、不调用 _detect_write、不装/卸驱动以外的系统改动
  （驱动未加载时仍会用 sc.exe 加载 BYOVD 驱动 —— 这是必须的通道，README 有说明）
★ 输出人类可读报告 + 可选 JSON（--json / --json-out），方便直接喂给 AI agent
================================================================================

用法：
    python read_probe.py                          # 人类可读报告
    python read_probe.py --json                   # 只输出 JSON（stdout）
    python read_probe.py --json-out probe.json    # 同时写 JSON 文件
    python read_probe.py --drivers ..\\P104-2.0x16-Manual\\bin\\drivers
    python read_probe.py --device 10de:1b87       # 其它矿卡改这里

退出码：0 成功 / 1 平台不支持 / 2 找不到驱动或设备
"""
import argparse
import json
import os
import platform
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _preparse_drivers(argv=None):
    """在 import lib_p104 之前解析 --drivers，设置 P104_BYOVD_DIR（驱动路径在建模块时就定了）。"""
    argv = sys.argv if argv is None else argv
    for i, a in enumerate(argv):
        if a == "--drivers" and i + 1 < len(argv):
            os.environ["P104_BYOVD_DIR"] = os.path.abspath(argv[i + 1])
        elif a.startswith("--drivers="):
            os.environ["P104_BYOVD_DIR"] = os.path.abspath(a.split("=", 1)[1])


_preparse_drivers()

import lib_p104 as L
import lib_session


# ---------------------------------------------------------------- 解码表 --
# PCIe 配置空间速率字段（LNKCAP/LNKSTA/LNKCTL2）
SPD = {1: "2.5GT/s(Gen1)", 2: "5.0GT/s(Gen2)", 3: "8.0GT/s(Gen3)", 4: "16GT/s(Gen4)"}
# Pascal 0x8C040/0x8C1C0 的速率编码（★ 与 PCIe 字段编码不同：0=8GT, 1=5GT, 2=2.5GT）
PCFG_SPD = {0: "8.0GT/s(Gen3)", 1: "5.0GT/s(Gen2)", 2: "2.5GT/s(Gen1)", 3: "保留"}

CAP_NAMES = {
    0x01: "Power Management", 0x02: "AGP", 0x03: "VPD", 0x04: "Slot ID",
    0x05: "MSI", 0x09: "Vendor Specific", 0x0D: "Subsystem ID", 0x0E: "MSI 64bit",
    0x10: "PCI Express", 0x11: "MSI-X", 0x12: "SATA", 0x13: "AF",
    0x14: "Enhanced Allocation", 0x15: "Resizable BAR", 0x19: "Secondary PCIe",
    0x1B: "Process Address Space ID", 0x1C: "LNR", 0x1D: "DPC", 0x1E: "L1 PM",
}

# 关键 BAR0 寄存器（只读快照表）
BAR0_REGS = [
    (0x8C040, "NV_XP_PL_LINK_CONFIG_0", "SPEED[19:18] / 宽度arm=bit31 / bit29"),
    (0x8C080, "NV_XP_PL_LINK_WIDTH", "当前链路宽度（芯片视角）"),
    (0x8C1C0, "NV_XP_PL_LCTRL_2", "目标速率[19:16]（2=5GT）"),
    (0x8872C, "NV_XVE_FUSE_OVERRIDE", "低4位=6 → CHANGE_SPEED_WIDTH=3"),
    (0x2241C, "NV_PTOP_FS4", "bits0/7 = live 声明开关"),
    (0x8841C, "NV_XVE_PRIV_MISC_1", "bits14:13=GEN2_DIS / bit15=复位前置"),
    (0x8C2C0, "NV_XP_PL_CYA_0", "bit2 参与 Gen2 写组"),
    (0x8C300, "NV_XP_PL_CYA_1", "bit4 参与 Gen2 写组"),
    (0x88610, "NV_XVE_VSEC_HIERARCHY", "bit0 参与 Gen2 写组"),
    (0x88140, "NV_LINK_WIDTH(特权)", ""),
    (0x88084, "LNKCAP 镜像", "配置空间 LNKCAP 的芯片内镜像"),
    (0x880A4, "LNKCAP2 镜像", "配置空间 LNKCAP2 的芯片内镜像"),
    (0x880A8, "LNKCTL2 镜像", ""),
    (0x88460, "nv_pci.CONFIG_LINK", ""),
    (0x101000, "PSTRAPS0", "strap 参考值（只读）"),
    (0x101004, "PSTRAPS1", ""),
    (0x101008, "PSTRAPS2", ""),
]

PHYS_ID = 0x1B8710DE       # 10DE:1B87 的 config 0x00 整字
VENDOR_P104 = 0x10DE
DEVICE_P104 = 0x1B87


# ---------------------------------------------------------------- 工具函数 --
def hx(v, width=8):
    return "??" if v is None else ("0x%0*X" % (width, v & ((1 << (4 * width)) - 1)))


def setup_output_encoding():
    """非控制台（管道/重定向/AI agent 捕获）时强制 UTF-8，避免中文日志乱码。"""
    for st in (sys.stdout, sys.stderr):
        try:
            if st is not None and hasattr(st, "reconfigure") and not st.isatty():
                st.reconfigure(encoding="utf-8")
        except Exception:
            pass


def decode_8c040(v):
    """0x8C040 的解码 + 合法性。
    ★ 实测（C# v1.1）：bit31=0 且 bit29=0 掉卡 2/2；bit31=1 且 bit29=1 掉卡 1/1。
      所以合法值 = bit31 与 bit29 恰好一个为 1。"""
    spd = (v >> 18) & 3
    arm = bool(v & 0x80000000)
    b29 = bool(v & 0x20000000)
    legal = arm != b29
    return ("SPEED=%s(%d) 宽度arm=%d bit29=%d %s"
            % (PCFG_SPD.get(spd, "?"), spd, int(arm), int(b29),
               "值合法" if legal else "★非法值（bit31/bit29 同为 0 或同为 1，写下去会掉卡）"))


def is_8c040_legal(v):
    """写 0x8C040 前必须通过：bit31 与 bit29 必须【恰好一个】为 1。
    实测：bit31=0 且 bit29=0 掉卡 2/2；bit31=1 且 bit29=1 掉卡 1/1。"""
    return bool(v & 0x80000000) != bool(v & 0x20000000)


def decode_bar0(off, v):
    if v is None:
        return ""
    if off == 0x8C040:
        return decode_8c040(v)
    if off == 0x8C080:
        return "宽=x%d" % (v & 0x3F)
    if off == 0x8C1C0:
        return "目标速率=%s(%d)" % (PCFG_SPD.get((v >> 16) & 0xF, "?"), (v >> 16) & 0xF)
    if off == 0x8872C:
        return "低4位=%d %s" % (v & 0xF, "(CHANGE_SPEED_WIDTH=3 已置)" if (v & 0xF) == 6 else "")
    if off == 0x2241C:
        return "bit0=%d bit7=%d" % (v & 1, (v >> 7) & 1)
    if off == 0x8841C:
        return "GEN2_DIS=%d 复位前置bit15=%d" % ((v >> 13) & 3, (v >> 15) & 1)
    if off == 0x8C2C0:
        return "bit2=%d" % ((v >> 2) & 1)
    if off == 0x8C300:
        return "bit4=%d" % ((v >> 4) & 1)
    if off == 0x88610:
        return "bit0=%d" % (v & 1)
    if off == 0x88140:
        return "宽=x%d" % (v & 0x3F)
    return ""


# ---------------------------------------------------------------- 只读访问 --
def cfg(s, loc, cap, off):
    """读配置空间（失败返回 0xFFFFFFFF，不抛异常）。"""
    v = s.prd(loc[0], loc[1], loc[2], cap + off)
    return 0xFFFFFFFF if v is None else v


def alive3(s, loc, tries=3, gap=0.1):
    """活体检查：读 config 0x00 最多 3 次（C# v1.1 的 Alive()）。
    ★ 必须先知道"卡还在不在"，再去读 BAR0；掉卡后读 BAR0 会挂死。"""
    for _ in range(tries):
        v = s.prd(loc[0], loc[1], loc[2], 0x00)
        if v == PHYS_ID:
            return True
        time.sleep(gap)
    return False


def discover(s, vendor=VENDOR_P104, device=DEVICE_P104, bus_max=64):
    """枚举 vendor:device，填好 s.loc / s.bar / s.cap / s.root。

    ★ 比 lib_p104.Session.find() 更宽：fn0 之外也扫（部分矿卡/桥会出现在别的 fn）。
    """
    for bus in range(bus_max):
        for dev in range(32):
            v0 = s.prd(bus, dev, 0, 0x00)
            if v0 is None or v0 == 0xFFFFFFFF or (v0 & 0xFFFF) == 0xFFFF:
                continue
            fns = [0]
            hdr = s.prd(bus, dev, 0, 0x0C) or 0
            if hdr & (1 << 23):          # 多功能设备
                fns = list(range(8))
            for fn in fns:
                v = s.prd(bus, dev, fn, 0x00)
                if v is None or v == 0xFFFFFFFF:
                    continue
                if (v & 0xFFFF) == vendor and ((v >> 16) & 0xFFFF) == device:
                    s.loc = (bus, dev, fn)
                    bar = s.prd(bus, dev, fn, 0x10)
                    s.bar = (bar or 0) & 0xFFFFFFF0
                    s.cap = s.pcie_cap(bus, dev, fn)
                    s.root = s.find_root_port(bus, dev, fn)
                    s.root_cap = s.pcie_cap(*s.root) if s.root else None
                    return s.loc
    return None


def cap_chain(s, b, d, f):
    """走完整个能力链（0x34 起）。"""
    caps = []
    ptr = (s.prd(b, d, f, 0x34) or 0) & 0xFC
    seen = set()
    while ptr and ptr < 0x100 and ptr not in seen:
        seen.add(ptr)
        hd = s.prd(b, d, f, ptr)
        if hd is None:
            break
        cid = hd & 0xFF
        nxt = (hd >> 8) & 0xFC
        caps.append({"offset": ptr, "id": cid,
                     "name": CAP_NAMES.get(cid, "未知/厂商"),
                     "next": nxt,
                     "hdr_value": s.prd(b, d, f, ptr + 0x04)})
        ptr = nxt
    return caps


def link_state(s, b, d, f):
    """读 PCIe 能力：LNKCAP/LNKSTA/LNKCTL/LNKCTL2/LNKCAP2/LNKSTA2。"""
    cap = s.pcie_cap(b, d, f)
    if cap is None:
        return None
    lc = cfg(s, (b, d, f), cap, 0x0C)
    ls = cfg(s, (b, d, f), cap, 0x12)
    lt = cfg(s, (b, d, f), cap, 0x10)
    l2 = cfg(s, (b, d, f), cap, 0x2C)
    c2 = cfg(s, (b, d, f), cap, 0x30)
    ls2 = cfg(s, (b, d, f), cap, 0x32)
    return {
        "bdf": "%02X:%02X.%d" % (b, d, f),
        "cap_offset": cap,
        "lnkcap": lc,
        "lnksta": ls,
        "lnkctl": lt,
        "lnkcap2": l2,
        "lnkctl2": c2,
        "lnksta2": ls2,
        "max_speed": lc & 0xF,
        "max_speed_text": SPD.get(lc & 0xF, "?"),
        "max_width": (lc >> 4) & 0x3F,
        "cur_speed": ls & 0xF,
        "cur_speed_text": SPD.get(ls & 0xF, "?"),
        "cur_width": (ls >> 4) & 0x3F,
        "tls": c2 & 0xF,
        "tls_text": SPD.get(c2 & 0xF, "?"),
        "retrain": bool(lt & (1 << 5)),
        "link_disable": bool(lt & (1 << 4)),
        "lcap2_gen2": (l2 & 0x6) == 0x6,
    }


def root_state(s):
    if not s.root:
        return None
    rb, rd, rf = s.root
    ln = link_state(s, rb, rd, rf)
    bus = s.prd(rb, rd, rf, 0x18) or 0
    return {
        "bdf": "%02X:%02X.%d" % (rb, rd, rf),
        "primary": bus & 0xFF,
        "secondary": (bus >> 8) & 0xFF,
        "subordinate": (bus >> 16) & 0xFF,
        "link": ln,
    }


def bar0_snapshot(s, alive_first=True):
    """BAR0 关键寄存器只读快照。掉卡时读 BAR0 会挂死 → 先做活体检查。"""
    snap = {}
    if s.bar is None:
        return snap, False
    if alive_first and s.loc and not alive3(s, s.loc, tries=1, gap=0):
        return snap, False
    for off, name, desc in BAR0_REGS:
        v = s.rd(off)
        snap["0x%06X" % off] = {
            "name": name,
            "desc": desc,
            "value": v,
            "hex": hx(v),
            "decoded": decode_bar0(off, v),
            "ok": v is not None,
        }
    return snap, True


def judge(s):
    """三个判据（与 C# v1.1 相同）+ PASS/FAIL。"""
    if not s.loc or s.cap is None:
        return {"result": "FAIL", "reason": "找不到设备或没有 PCIe 能力结构"}
    ls = link_state(s, *s.loc)
    j1 = ls["lcap2_gen2"]
    j2 = ls["cur_speed"] >= 2
    j3 = ls["max_width"] == 16
    ok = j1 and j2
    return {
        "j1_declared_gen2": j1,      # LNKCAP2 含 Gen2 声明（0x06）
        "j2_link_gen2": j2,          # 实际链路速率 >= 5GT/s
        "j3_width16_declared": j3,   # LNKCAP maxwidth == 16
        "result": "PASS" if ok else "FAIL",
        "cur_speed": ls["cur_speed"],
        "cur_speed_text": ls["cur_speed_text"],
        "cur_width": ls["cur_width"],
        "max_width": ls["max_width"],
    }


def build_report(s, vendor=VENDOR_P104, device=DEVICE_P104):
    loc = s.loc
    gpu_cfg = {}
    if loc:
        b, d, f = loc
        gpu_cfg = {
            "id": s.prd(b, d, f, 0x00),
            "command": s.prd(b, d, f, 0x04),
            "class": s.prd(b, d, f, 0x08),
            "bar0": s.prd(b, d, f, 0x10),
            "bus_numbers": s.prd(b, d, f, 0x18),
            "cap_ptr": s.prd(b, d, f, 0x34),
        }
    snap, snap_ok = bar0_snapshot(s)
    return {
        "tool": "read_probe.py (P104-100, 只读)",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "byovd_dir": L.BYOVD_DIR,
        "write_capable": s.write_ioctl is not None,   # 只读探针里恒为 False
        "device": {
            "bdf": "%02X:%02X.%d" % loc if loc else None,
            "vendor_id": "0x%04X" % vendor,
            "vendor_name": "NVIDIA" if vendor == 0x10DE else "",
            "device_id": "0x%04X" % device,
            "bar0": ("0x%08X" % (s.bar or 0)) if loc else None,
            "config": gpu_cfg,
        },
        "cap_chain": cap_chain(s, *loc) if loc else [],
        "link": link_state(s, *loc) if loc else None,
        "root_port": root_state(s),
        "bar0": snap,
        "bar0_read_ok": snap_ok,
        "judge": judge(s),
    }


def print_human(rep):
    p = print
    p("=" * 78)
    p("P104-100 PCIe 只读探针   %s   ★ 未执行任何写操作" % rep["timestamp"])
    p("=" * 78)
    p("[环境] %s  Python %s" % (rep["os"], rep["python"]))
    p("[驱动] BYOVD 目录 = %s" % rep["byovd_dir"])
    dev = rep["device"]
    if not dev["bdf"]:
        p("[设备] × 没找到 %s:%s" % (dev["vendor"], dev["device_id"]))
        return
    c = dev["config"]
    p("[设备] %s  %s:%s  class=%s  BAR0=%s  PCIe cap=%s" % (
        dev["bdf"], dev["vendor_id"].replace("0x", ""), dev["device_id"].replace("0x", ""),
        ("0x%06X" % ((c.get("class") or 0) >> 8)), dev["bar0"],
        hx(rep["link"]["cap_offset"], 2) if rep["link"] else "无"))
    p("[能力链] " + (" → ".join("%s@0x%02X" % (x["name"], x["offset"]) for x in rep["cap_chain"])
                     or "（空）"))
    p("")
    ln = rep["link"]
    if ln:
        p("[目标卡链路]")
        p("  LNKCAP  = %s  声明: %s x%d" % (hx(ln["lnkcap"]), ln["max_speed_text"], ln["max_width"]))
        p("  LNKSTA  = %s  实际: %s x%d" % (hx(ln["lnksta"]), ln["cur_speed_text"], ln["cur_width"]))
        p("  LNKCAP2 = %s  %s" % (hx(ln["lnkcap2"]), "√ 含 Gen2 声明 (0x06)" if ln["lcap2_gen2"] else "× 只有 Gen1 声明"))
        p("  LNKCTL  = %s  %s" % (hx(ln["lnkctl"], 4),
                                  ("RETRAIN " if ln["retrain"] else "") + ("LINKDIS" if ln["link_disable"] else "")))
        p("  LNKCTL2 = %s  TLS=%d (%s)" % (hx(ln["lnkctl2"]), ln["tls"], ln["tls_text"]))
        p("  LNKSTA2 = %s" % hx(ln["lnksta2"]))
    rp = rep["root_port"]
    if rp:
        rl = rp["link"]
        p("[上游根端口] %s  bus=%d sec=%d sub=%d" % (rp["bdf"], rp["primary"], rp["secondary"], rp["subordinate"]))
        if rl:
            p("  LNKCAP  = %s  声明: %s x%d" % (hx(rl["lnkcap"]), rl["max_speed_text"], rl["max_width"]))
            p("  LNKSTA  = %s  实际: %s x%d" % (hx(rl["lnksta"]), rl["cur_speed_text"], rl["cur_width"]))
            p("  LNKCTL2 = %s  TLS=%d (%s)" % (hx(rl["lnkctl2"]), rl["tls"], rl["tls_text"]))
    else:
        p("[上游根端口] 未找到（可能是根复合体直连）")
    p("")
    p("[BAR0 关键寄存器]")
    if not rep["bar0"]:
        p("  × 读不到（掉卡？驱动？）")
    for key, item in rep["bar0"].items():
        val = item["hex"] if item["ok"] else "（读失败）"
        extra = ("  " + item["decoded"]) if item["decoded"] else ""
        p("  %s %-28s = %s%s" % (key, item["name"], val, extra))
    p("")
    j = rep["judge"]
    p("[判定]")
    p("  J1 声明 Gen2 (LNKCAP2&0x6==0x6)      : %s" % ("√" if j.get("j1_declared_gen2") else "×"))
    p("  J2 实际链路 >= Gen2 (LNKSTA>=5GT/s)  : %s" % ("√" if j.get("j2_link_gen2") else "×"))
    p("  J3 声明最大宽度 x16 (LNKCAP==16)     : %s" % ("√" if j.get("j3_width16_declared") else "×"))
    p("  RESULT: %s   （当前 %s x%d）" % (
        j.get("result"), j.get("cur_speed_text", "?"), j.get("cur_width", 0)))
    p("")
    p("[提示]")
    if j.get("result") == "PASS":
        p("  已经是 Gen2（+ 宽度声明）。不需要再写任何寄存器。")
    else:
        p("  要解锁请用 apply_gen2_width.py：先 --apply --dry-run 看计划，再 --apply。")
        p("  ★ 看懂 README 的安全规矩再动手；掉卡就 AC 断电（拔电源 30 秒）。")
    p("=" * 78)


# ---------------------------------------------------------------- main --
def main(argv=None):
    setup_output_encoding()
    ap = argparse.ArgumentParser(
        description="P104-100 PCIe 只读探针（不写硬件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python read_probe.py\n"
               "  python read_probe.py --json\n"
               "  python read_probe.py --device 10de:1b87 --drivers ../P104-2.0x16-Manual/bin/drivers\n")
    ap.add_argument("--device", default="10de:1b87",
                    help="目标设备 vendor:device（默认 10de:1b87 = P104-100）")
    ap.add_argument("--drivers", default=None,
                    help="BYOVD 驱动目录（含 WinRing0x64.sys / ThrottleStop.sys）")
    ap.add_argument("--bus-max", type=int, default=64, help="PCI 总线扫描上限（默认 64）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 到 stdout")
    ap.add_argument("--json-out", default=None, help="把 JSON 写到文件")
    a = ap.parse_args(argv)

    if os.name != "nt":
        print("本工具只支持 Windows（BYOVD 驱动 + Win32 API）")
        return 1
    if a.drivers:
        d = os.path.abspath(a.drivers)
        if not os.path.isfile(os.path.join(d, "WinRing0x64.sys")):
            print("警告：%s 里没有 WinRing0x64.sys，驱动装载可能失败" % d, file=sys.stderr)
    try:
        vendor, device = [int(x, 16) for x in a.device.split(":")]
    except Exception:
        print("--device 格式应为 VVVV:DDDD，例如 10de:1b87")
        return 1

    print("", file=sys.stderr)
    print("打开 BYOVD 通道（只读模式，不探测写通道）…", file=sys.stderr)
    s = lib_session.open_session(detect_write=False, verbose=True,
                                 vendor=vendor, device=device, bus_max=a.bus_max)
    if s is None:
        print("× 打不开 BYOVD 通道或找不到设备。检查：管理员权限 / 卡在不在 / 驱动目录 %s"
              % L.BYOVD_DIR, file=sys.stderr)
        return 2
    try:
        loc = s.loc or discover(s, vendor, device, a.bus_max)
        if not loc:
            print("× 找不到 %04X:%04X（用 --device 指定其它卡）" % (vendor, device), file=sys.stderr)
            return 2
        s.loc = loc
        rep = build_report(s, vendor, device)
    finally:
        s.close()

    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print("JSON 已写入 %s" % a.json_out, file=sys.stderr)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print_human(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
