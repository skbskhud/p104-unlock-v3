// ============================================================================
//  P104Gen2x4 —— NVIDIA P104-100 (GP104) PCIe 2.0 x4 解锁工具  ·  v1.1
//  Gen2 x4 真实链路 + LnkCap 声明最大宽度 x16
// ============================================================================
//  成果：CPU 直连 x16 槽上，Gen1 x4 → Gen2 x4，带宽 0.78 → 1.55 GB/s（约 2 倍）
//
//  用法：
//    P104Gen2x4.exe                 完整解锁（bringup），含常驻按住循环
//    P104Gen2x4.exe -once           只解锁一次，不常驻；打印 PASS/FAIL
//    P104Gen2x4.exe -dry-run        只读：快照 + 打印"将会写什么"，不写
//    P104Gen2x4.exe -status         只读诊断（= -check）
//    P104Gen2x4.exe -check          只读：从状态文件/日志给结论（不需要管理员）
//    P104Gen2x4.exe -diag           只读：寄存器级诊断（需要管理员）
//    P104Gen2x4.exe -off            停止按住、按快照还原寄存器
//    P104Gen2x4.exe -install        安装（写驱动策略 + 部署 + 注册计划任务）
//    P104Gen2x4.exe -uninstall      卸载（删任务 + 停服务；保留驱动策略键）
//    P104Gen2x4.exe -bw             只读：CUDA 带宽实测（无需 Python）
//    P104Gen2x4.exe -vbios [-save x.rom]   读 VBIOS 并算哈希（只读）
//    P104Gen2x4.exe -quiet          日志只写文件不打印
//    P104Gen2x4.exe -noreset        只做无复位（live）路径，不做整卡复位
//    P104Gen2x4.exe -retry N        本次运行最多允许 N 次整卡复位（1..2，默认 1）
//
//  ★★ 关键机理（v1.1 重写，2026-10-30）★★
//    1) 【主路径 · 无复位】P104 出厂把 PCIe 声明钉在 2.5GT/s（LNKCAP2=0x02）。
//       两条"live 声明开关"写下去后，PCIe 核会实时把声明改成 Gen2（0x06）：
//         · BAR0 0x8841C 的 bits[14:13] 清 0（NV_XVE_PRIV_MISC_1，GEN2_DIS）
//         · BAR0 0x2241C 的 bits[0],[7] 置 1（NV_PTOP_FS4）
//       再配 0x8C040 SPEED=5GT + 0x8C1C0[19:16]=2 + 0x88610 bit0=1，
//       两端 LNKCTL2 目标速率=2 + 根端口 retrain，链路即上 Gen2 x4。
//       实测（2026-10-28/29 两次独立复现）：秒级生效，无需复位，保持 29 分钟。
//       ★ v1.0 的写组把 0x8841C bit13 写反了（置 1）、且从不写 0x2241C，
//         所以它永远锁不上，只能靠"复位 + 赌 NVIDIA 驱动重新初始化"碰运气
//         （历史统计：61 次复位仅 9 次锁存成功 = 14.8%）。这是"时好时坏"的根因。
//    2) 【兜底 · 整卡复位】仅当 live 路径连续 3 轮（约 6 秒）都失败时才做一次：
//       用 SetupAPI DIF_PROPERTYCHANGE 禁用→保持 6 秒→启用，让驱动重新初始化。
//       复位前置必须显式置 0x8841C bit15=1（实测 7/7 无反例），否则复位白做。
//       复位后无论是否锁存，都再补一遍 live 写组（"补刀"），把不确定性补掉。
//    3) 【宽度声明 x16】只能在 Gen2 已达成后写：
//         0x8C040 = (cur & ~0xF8000000) | 0x80000000
//       ★ 禁止写 bit31=0 且 bit29=0 的值（实测 2/2 掉卡）；
//       ★ 禁止写 bit31=1 且 bit29=1 的值（实测 1/1 掉卡）；
//       ★ 回退永远整字写 0x20085800，绝不用掩码单 bit 清。
//    4) 【常驻保持】150ms 看门狗，只在四个判据偏离时补写，触发类写 ≥2 秒限流。
//       （v1.0 是每 20ms 全量重写，把 CHANGE_SPEED 触发位打成 50Hz，无必要。）
//    5) 【复位预算】一次运行最多 1 次，同一开机周期最多 2 次，两次复位间隔 ≥10 分钟。
//       （v1.0 的 -retry 3 会让计划任务每分钟连做 3 次复位 == 反复黑屏。）
//
//  驱动策略 RMPcieLinkSpeed 必须 = 1；= 2 会把 TLS 钳死在 Gen1。
// ============================================================================
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

class P104Gen2x4
{
    // ---------- 常量 ----------
    const ushort VENDOR_NV = 0x10DE;
    const ushort DEVICE_P104 = 0x1B87;
    const uint PAGE_SIZE = 0x1000;

    const uint IOCTL_PCI_READ = 0x9C406144;   // WinRing0: 读 PCI 配置空间
    const uint IOCTL_PCI_WRITE = 0x9C40A148;  // WinRing0: 写 PCI 配置空间（★ 不是 0x9C406148）
    const uint IOCTL_MAP_PHYS = 0x8000645C;   // ThrottleStop: 映射物理页

    const string SVC_WR = "P104Gen2x4Wr";
    const string SVC_TS = "P104Gen2x4Ts";

    // BAR0 寄存器
    const uint OFF_PRIV_MISC1 = 0x8841C;      // 写使能位：置 bit11/13/15，清 bit12/14（复位前置）；live 路径另见 ApplyLiveGroup
    const uint OFF_PTOP_FS4 = 0x2241C;        // NV_PTOP_FS4：live 声明开关，bits0/7=1
    const uint OFF_CYA0 = 0x8C2C0;            // NV_XP_PL_CYA_0：bit2=0
    const uint OFF_CYA1 = 0x8C300;            // NV_XP_PL_CYA_1：bit4=0
    const uint OFF_VSEC_HIER = 0x88610;       // 清 bit12，置 bit0
    const uint OFF_LTSSM = 0x8872C;           // 写 6
    const uint OFF_LINKCFG0 = 0x8C040;        // SPEED[19:18]: Pascal 0=8GT 1=5GT 2=2.5GT；bit31 宽度 arm/commit
    const uint OFF_LCTRL2 = 0x8C1C0;          // NV_XP_PL_LCTRL_2：[19:16] 目标速率（2=5GT）
    const uint OFF_XP3G_OVR0 = 0x8E110;
    const uint OFF_XP3G_OVR3 = 0x8E11C;
    const uint OFF_XP3G_VAL0 = 0x8E120;
    const uint OFF_XP3G_VAL3 = 0x8E12C;
    const uint OFF_LCAP2_MIRROR = 0x880A4;    // 声明镜像（只读，用来看是否解锁）

    const uint SPEED_GEN2 = 1;                // Pascal 编码：1 = 5.0GT/s
    const uint LINKCFG0_SAFE = 0x20085800;    // 0x8C040 工厂安全整字（bit31=0, bit29=1, SPEED=2.5GT）
    const uint LINKCFG_WIDTH_ARM = 0x80000000;// 宽度 arm：bit31=1（且必须确保 bit29=0）

    // ---------- v1.1 状态机参数 ----------
    const int  T_READY_MS          = 120000;  // 开机后等"设备 + 驱动"就绪上限
    const int  T_LIVE_LATCH_MS     = 2000;    // live 写组后观察 LCAP2 的窗口（实测 ≤1s）
    const int  T_LIVE_ROUNDS       = 3;       // live 写组重试轮数
    const int  T_RETRAIN_ROUNDS    = 3;       // TLS+retrain 重试轮数
    const int  T_RETRAIN_OBSERVE_MS= 3000;    // retrain 后等 LNKSTA 的窗口
    const int  T_TRIGGER_MIN_GAP_MS= 2000;    // 两次 CHANGE_SPEED/Retrain 的最小间隔
    const int  T_RESET_LATCH_MS    = 8000;    // 复位后观察 LCAP2 的窗口（实测 4s 就够，20s 纯浪费）
    const int  T_DEVICE_BACK_MS    = 30000;   // 复位后等设备回来
    const int  T_HOLD_PERIOD_MS    = 150;     // 常驻看门狗周期（v1.0 是 20ms，无必要）
    const int  T_STATUS_PERIOD_MS  = 1000;    // 状态文件刷新周期
    const int  T_WIDTH_RETRY_MS    = 3000;    // 宽度声明丢失后的补写限流（恢复窗口越短越好）
    const int  MAX_RESETS_PER_RUN  = 1;       // 一次运行最多复位 1 次
    const int  MAX_RESETS_PER_BOOT  = 2;      // 同一开机周期最多 2 次
    const long RESET_MIN_GAP_MS    = 600000;  // 两次复位最小间隔 10 分钟

    const uint GENERIC_READ = 0x80000000, GENERIC_WRITE = 0x40000000, OPEN_EXISTING = 3;
    static readonly IntPtr INVALID = new IntPtr(-1);

    // ---------- SetupAPI ----------
    [StructLayout(LayoutKind.Sequential)]
    struct SP_DEVINFO_DATA
    {
        public uint cbSize;
        public Guid ClassGuid;
        public uint DevInst;
        public IntPtr Reserved;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct SP_CLASSINSTALL_HEADER
    {
        public uint cbSize;
        public uint InstallFunction;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct SP_PROPCHANGE_PARAMS
    {
        public SP_CLASSINSTALL_HEADER ClassInstallHeader;
        public uint StateChange;
        public uint Scope;
        public uint HwProfile;
    }

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr SetupDiGetClassDevs(IntPtr ClassGuid, string Enumerator, IntPtr hwndParent, uint Flags);
    [DllImport("setupapi.dll", SetLastError = true)]
    static extern bool SetupDiEnumDeviceInfo(IntPtr DeviceInfoSet, uint MemberIndex, ref SP_DEVINFO_DATA DeviceInfoData);
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool SetupDiGetDeviceInstanceId(IntPtr DeviceInfoSet, ref SP_DEVINFO_DATA DeviceInfoData,
        StringBuilder DeviceInstanceId, uint DeviceInstanceIdSize, out uint RequiredSize);
    [DllImport("setupapi.dll", SetLastError = true)]
    static extern bool SetupDiSetClassInstallParams(IntPtr DeviceInfoSet, ref SP_DEVINFO_DATA DeviceInfoData,
        ref SP_PROPCHANGE_PARAMS ClassInstallParams, uint ClassInstallParamsSize);
    [DllImport("setupapi.dll", SetLastError = true)]
    static extern bool SetupDiCallClassInstaller(uint InstallFunction, IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData);
    [DllImport("setupapi.dll", SetLastError = true)]
    static extern bool SetupDiDestroyDeviceInfoList(IntPtr DeviceInfoSet);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool SetupDiGetDeviceRegistryProperty(IntPtr DeviceInfoSet, ref SP_DEVINFO_DATA DeviceInfoData,
        uint Property, out uint PropertyRegDataType, byte[] PropertyBuffer, uint PropertyBufferSize, out uint RequiredSize);

    const uint DIGCF_PRESENT = 0x02;
    const uint DIGCF_ALLCLASSES = 0x04;
    const uint DIF_PROPERTYCHANGE = 0x12;
    const uint DICS_ENABLE = 0x01;
    const uint DICS_DISABLE = 0x02;
    const uint DICS_FLAG_GLOBAL = 0x01;
    const uint SPDRP_HARDWAREID = 0x01;

    [DllImport("cfgmgr32.dll")]
    static extern int CM_Get_DevNode_Status(out uint status, out uint problemNumber, uint devInst, uint flags);
    const uint DN_HAS_PROBLEM = 0x00000400;
    const uint CM_PROB_DISABLED = 22;

    // ---------- kernel32 ----------
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateFileW(string name, uint access, uint share, IntPtr sa, uint disp, uint flags, IntPtr tmpl);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool DeviceIoControl(IntPtr h, uint code, byte[] inBuf, uint inLen,
                                       byte[] outBuf, uint outLen, out uint ret, IntPtr ov);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr h);

    // ---------- 状态 ----------
    static IntPtr hWr = INVALID, hTs = INVALID;
    static ulong _vaLink, _vaPpci, _vaXp3g, _vaFuse0, _vaFuse1, _vaFuse2;
    static uint _bar;
    static byte _bus, _dev, _fun;
    static bool Quiet = false;
    /// <summary>整卡复位最多做几次。★ 默认 1 —— 每次复位都会让屏幕黑一下，
    /// 多次复位会造成"反复黑屏"，所以必须显式 -retry N 才允许多次。</summary>
    static int ResetRetries = 1;
    static string ExeDir { get { return Path.GetDirectoryName(Process.GetCurrentProcess().MainModule.FileName); } }
    static string StatusFile = @"C:\ProgramData\P104Gen2x4\status.txt";
    /// <summary>★ 给人看的实时状态文件（不需要管理员就能读）</summary>
    static string StatusOkFile = @"C:\ProgramData\P104Gen2x4\当前状态.txt";
    static long _rewrites = 0, _samples = 0;

    // ---------- v1.1 运行状态 ----------
    static bool WidthEnabled = true;          // 是否做"声明宽度 x16"
    static bool AllowReset = true;            // -noreset 时置 false
    static int  _resetUsedThisRun = 0;
    static string StateFile  = @"C:\ProgramData\P104Gen2x4\snapshot.txt";     // 只读基线快照（-off 用）
    static string BudgetFile = @"C:\ProgramData\P104Gen2x4\reset_budget.txt"; // 复位预算（跨进程/跨开机）
    static long _bootTick = 0;                // 本次开机周期指纹（Environment.TickCount）
    static long _lastResetTick = -100000000;  // 上次复位时的 TickCount
    static int  _resetsThisBoot = 0;
    static long _lastTriggerTick = -100000;
    static long _lastWidthFixTick = -100000;
    static long _lastFixTick = -100000;

    /// <summary>
    /// ★ 写一个"人看的"实时状态文件。按住进程每 ~1 秒更新一次。
    /// 这样 status.cmd 不需要管理员权限、也不需要读寄存器，就能一句话告诉你 Gen1/Gen2。
    ///
    /// ★ 2026-10-16 改进：状态文件必须【自带证据】，不能只给结论。
    ///   起因：用户看到 status.cmd 说"Gen2 已解锁"，但 GPU-Z 显示"当前 1.1 x4"，
    ///   无法判断谁对。根因是旧版只打印解码后的结论、没有原始寄存器值可核实；
    ///   而且没有解释 GPU-Z / nvidia-smi 读的是【驱动上报值】、在这张矿卡上不可靠。
    /// </summary>
    static void WriteStatusFile(byte rb, byte rd, byte rf, uint rcap, uint ecap, uint eLnkSta, uint samples)
    {
        try
        {
            uint curSpeed = eLnkSta & 0xF;
            uint curWidth = (eLnkSta >> 4) & 0x3F;
            string gen = curSpeed >= 3 ? "Gen3" : curSpeed == 2 ? "Gen2" : "Gen1";
            string spd = curSpeed == 3 ? "8.0GT/s" : curSpeed == 2 ? "5.0GT/s" : "2.5GT/s";
            bool ok = curSpeed >= 2;

            // 端点：能力声明类寄存器（不是当前速率，只作对照）
            uint eLnkap = PciRead(_bus, _dev, _fun, ecap + 0x0C);
            uint eLnkap2 = PciRead(_bus, _dev, _fun, ecap + 0x2C);
            uint eLnkctl2 = PciRead(_bus, _dev, _fun, ecap + 0x30);
            // 上游根端口：它的 LNKSTA 也反映同一条链路的当前速率（交叉验证）
            uint rLnkSta = PciRead(rb, rd, rf, rcap + 0x12);

            string eg = (eLnkSta & 0xF) >= 3 ? "8.0GT/s(Gen3)" : (eLnkSta & 0xF) == 2 ? "5.0GT/s(Gen2)"
                      : (eLnkSta & 0xF) == 1 ? "2.5GT/s(Gen1)" : "?";
            string rg = (rLnkSta & 0xF) >= 3 ? "8.0GT/s(Gen3)" : (rLnkSta & 0xF) == 2 ? "5.0GT/s(Gen2)"
                      : (rLnkSta & 0xF) == 1 ? "2.5GT/s(Gen1)" : "?";
            string tg = (eLnkctl2 & 0xF) >= 3 ? "8.0GT/s(Gen3)" : (eLnkctl2 & 0xF) == 2 ? "5.0GT/s(Gen2)"
                      : (eLnkctl2 & 0xF) == 1 ? "2.5GT/s(Gen1)" : "?";
            string mg = (eLnkap & 0xF) >= 3 ? "8.0GT/s(Gen3)" : (eLnkap & 0xF) == 2 ? "5.0GT/s(Gen2)"
                      : (eLnkap & 0xF) == 1 ? "2.5GT/s(Gen1)" : "?";
            uint vec = (eLnkap2 >> 1) & 0x7F;

            var sb = new StringBuilder();
            sb.AppendLine("============================================================");
            sb.AppendLine("P104-100 PCIe 链路状态（由按住进程实时写入）");
            sb.AppendLine("更新时间 : " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
            sb.AppendLine("============================================================");
            sb.AppendLine();
            sb.AppendLine("★ 当前链路速率 : " + gen + "  (" + spd + ")      <- 判据 = LNKSTA[3:0]");
            sb.AppendLine("  当前链路宽度 : x" + curWidth + "                 <- 判据 = LNKSTA[9:4]");
            sb.AppendLine();
            sb.AppendLine("------------------------------------------------------------");
            sb.AppendLine("【判据原始数据】—— 你可以自己核实这两行");
            sb.AppendLine("------------------------------------------------------------");
            sb.AppendLine("  端点   " + _bus.ToString("X2") + ":" + _dev.ToString("X2") + "." + _fun
                          + "  LNKSTA = 0x" + eLnkSta.ToString("X8")
                          + "   SPD[3:0]=" + curSpeed + " (" + eg + ")"
                          + "   WIDTH[9:4]=" + curWidth);
            sb.AppendLine("  根端口 " + rb.ToString("X2") + ":" + rd.ToString("X2") + "." + rf
                          + "  LNKSTA = 0x" + rLnkSta.ToString("X8")
                          + "   SPD[3:0]=" + (rLnkSta & 0xF) + " (" + rg + ")"
                          + "   WIDTH[9:4]=" + ((rLnkSta >> 4) & 0x3F));
            sb.AppendLine("  => 两端必须一致；上面两行的 SPD 都是 2 才是真的 Gen2。");
            sb.AppendLine();
            sb.AppendLine("------------------------------------------------------------");
            sb.AppendLine("【以下不是当前速率，只是\"宣称能力\"，别混淆】");
            sb.AppendLine("------------------------------------------------------------");
            sb.AppendLine("  端点 LNKCAP  (cap+0x0C) = 0x" + eLnkap.ToString("X8")
                          + "   MAXSPD=" + (eLnkap & 0xF) + " (" + mg + ")"
                          + "   MAXWIDTH=" + ((eLnkap >> 4) & 0x3F));
            sb.AppendLine("  端点 LNKCAP2 (cap+0x2C) = 0x" + eLnkap2.ToString("X8")
                          + "   支持速率向量 = 0x" + vec.ToString("X2")
                          + (vec == 3 ? " (Gen1+Gen2)" : vec == 1 ? " (仅 Gen1)" : ""));
            sb.AppendLine("  端点 LNKCTL2 (cap+0x30) = 0x" + eLnkctl2.ToString("X8")
                          + "   TLS[3:0]=" + (eLnkctl2 & 0xF) + " (" + tg + ")");
            sb.AppendLine();
            sb.AppendLine("------------------------------------------------------------");
            sb.AppendLine("【★ 为什么 GPU-Z / nvidia-smi 可能显示 Gen1？】");
            sb.AppendLine("------------------------------------------------------------");
            sb.AppendLine("  它们读的是【NVIDIA 驱动上报的值】，不是 PCIe 寄存器。");
            sb.AppendLine("  在这张【无显示输出的矿卡】上，驱动上报值不可靠 ——");
            sb.AppendLine("  nvidia-smi 的 pcie.link.gen.current / gpucurrent 在此卡上");
            sb.AppendLine("  始终报 1，即使寄存器 LNKSTA 和实测带宽都证明是 Gen2。");
            sb.AppendLine("  => 以本文件的 LNKSTA 原始值 + 带宽实测为准。");
            sb.AppendLine();
            sb.AppendLine("------------------------------------------------------------");
            sb.AppendLine("【带宽自检（唯一不会骗人的判据）】");
            sb.AppendLine("------------------------------------------------------------");
            sb.AppendLine("  Gen1 x4 = H2D 约 0.78 GB/s");
            sb.AppendLine("  Gen2 x4 = H2D 约 1.55 GB/s      <- 目标");
            sb.AppendLine("  Gen3 x4 = H2D 约 3.10 GB/s");
            sb.AppendLine("  跑 check-bw.cmd 可实测（需要 Python + torch）");
            sb.AppendLine();
            sb.AppendLine("============================================================");
            sb.AppendLine("结论     : " + (ok ? "【正常】" + gen + " x" + curWidth + " 已解锁并在保持"
                                            : "【异常】只跑到 " + gen + " —— 目标 Gen2"));
            sb.AppendLine("按住进程 : 运行中（PID " + Process.GetCurrentProcess().Id + "）");
            sb.AppendLine("累计采样 : " + samples + " 次");
            sb.AppendLine("============================================================");
            File.WriteAllText(StatusOkFile, sb.ToString(), Encoding.UTF8);
        }
        catch { }
    }

    static void Log(string s)
    {
        string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + s;
        if (!Quiet) { try { Console.WriteLine(line); } catch { } }
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(StatusFile));
            // AppendAllText 每次关闭文件即落盘 —— 相当于逐行 flush，
            // 掉卡/崩溃时能保留最后一步（2026-10-29 12:49 事故日志截断的教训）。
            File.AppendAllText(StatusFile, line + Environment.NewLine, Encoding.UTF8);
        }
        catch { }
    }

    /// <summary>只往控制台写（不写日志文件）——用于 -check / -bw 等只读输出。</summary>
    static void Say(string s) { try { Console.WriteLine(s); } catch { } }

    static string Run(string exe, string args) { return Run(exe, args, 60000); }

    /// <summary>
    /// 跑外部命令并捕获输出。
    /// ★ v1.1 修乱码：sc.exe / schtasks.exe 在重定向下按系统 ANSI 代码页（中文系统 = GBK 936）输出，
    ///   必须用同一代码页解码；v1.0 在主线程强制 Console.OutputEncoding=UTF8 后按 UTF-8 解码，
    ///   得到的 `�ɹ�` 会永久写进日志。另外这里加了硬超时，避免某个子进程把工具卡死。
    /// </summary>
    static string Run(string exe, string args, int timeoutMs)
    {
        try
        {
            var psi = new ProcessStartInfo(exe, args);
            psi.UseShellExecute = false;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.CreateNoWindow = true;
            try { psi.StandardOutputEncoding = Encoding.Default; psi.StandardErrorEncoding = Encoding.Default; }
            catch { }
            var p = Process.Start(psi);
            var outSb = new StringBuilder();
            var errSb = new StringBuilder();
            var t1 = new Thread(() => { try { outSb.Append(p.StandardOutput.ReadToEnd()); } catch { } });
            var t2 = new Thread(() => { try { errSb.Append(p.StandardError.ReadToEnd()); } catch { } });
            t1.IsBackground = true; t2.IsBackground = true;
            t1.Start(); t2.Start();
            if (!p.WaitForExit(timeoutMs)) { try { p.Kill(); } catch { } }
            t1.Join(3000); t2.Join(3000);
            return (outSb.ToString() + errSb.ToString()).Trim();
        }
        catch (Exception e) { return "ERR " + e.Message; }
    }

    // ================= 驱动 =================
    static void StageService(string svc, string sysPath)
    {
        Run("sc.exe", "stop " + svc);
        Run("sc.exe", "delete " + svc);
        Run("sc.exe", "create " + svc + " type= kernel start= demand binPath= \"" + sysPath + "\"");
        Run("sc.exe", "start " + svc);
    }

    static IntPtr TryOpen(string name)
    { return CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, IntPtr.Zero, OPEN_EXISTING, 0, IntPtr.Zero); }

    static bool EnsureDevices()
    {
        hWr = TryOpen(@"\\.\WinRing0_1_2_0");
        hTs = TryOpen(@"\\.\" + SVC_TS);
        if (hWr != INVALID && hTs != INVALID) return true;
        string drvDir = Path.Combine(ExeDir, "drivers");
        string wr = Path.Combine(drvDir, "WinRing0x64.sys"), ts = Path.Combine(drvDir, "ThrottleStop.sys");
        if (!File.Exists(wr) || !File.Exists(ts))
        {
            Log("找不到驱动文件: " + drvDir);
            return false;
        }
        StageService(SVC_WR, wr);
        StageService(SVC_TS, ts);
        if (hWr == INVALID) hWr = TryOpen(@"\\.\WinRing0_1_2_0");
        if (hTs == INVALID) hTs = TryOpen(@"\\.\" + SVC_TS);
        if (hWr == INVALID || hTs == INVALID)
        {
            Log("驱动加载失败。常见原因：");
            Log("  · 未以管理员运行");
            Log("  · 易受攻击驱动阻止列表开启 → 关闭后重启");
            Log("  · 反作弊预启动驱动拦截（错误 31）→ 停掉它");
            return false;
        }
        return true;
    }

    static void UnloadServices()
    {
        Run("sc.exe", "stop " + SVC_WR); Run("sc.exe", "delete " + SVC_WR);
        Run("sc.exe", "stop " + SVC_TS); Run("sc.exe", "delete " + SVC_TS);
    }

    // ================= PCI 配置空间 =================
    static uint PciRead(byte bus, byte dev, byte fun, uint reg)
    {
        if (hWr == INVALID) return 0xFFFFFFFF;
        byte[] inb = new byte[8], outb = new byte[4];
        BitConverter.GetBytes((uint)((bus << 8) | (dev << 3) | fun)).CopyTo(inb, 0);
        BitConverter.GetBytes(reg).CopyTo(inb, 4);
        uint ret;
        if (!DeviceIoControl(hWr, IOCTL_PCI_READ, inb, 8, outb, 4, out ret, IntPtr.Zero)) return 0xFFFFFFFF;
        return BitConverter.ToUInt32(outb, 0);
    }

    static bool PciWrite(byte bus, byte dev, byte fun, uint reg, uint val)
    {
        if (hWr == INVALID) return false;
        byte[] inb = new byte[12], outb = new byte[4];
        BitConverter.GetBytes((uint)((bus << 8) | (dev << 3) | fun)).CopyTo(inb, 0);
        BitConverter.GetBytes(reg).CopyTo(inb, 4);
        BitConverter.GetBytes(val).CopyTo(inb, 8);
        uint ret;
        return DeviceIoControl(hWr, IOCTL_PCI_WRITE, inb, 12, outb, 4, out ret, IntPtr.Zero);
    }

    /// <summary>写通道自检：对只读的 0x00 做 no-op 同值写，请求成功即通道可用。</summary>
    static bool SelfTestWrite(byte bus, byte dev, byte fun)
    {
        return PciWrite(bus, dev, fun, 0x00, 0x1B8710DE);
    }

    static uint PciCap(byte bus, byte dev, byte fun)
    {
        uint ptr = PciRead(bus, dev, fun, 0x34) & 0xFC;
        int guard = 0;
        while (ptr != 0 && ptr < 0x100 && guard++ < 48)
        {
            uint head = PciRead(bus, dev, fun, ptr);
            if ((head & 0xFF) == 0x10) return ptr;
            ptr = (head >> 8) & 0xFC;
        }
        return 0;
    }

    /// <summary>找 P104。只扫 function 0（P104 是单功能设备）。</summary>
    static bool FindP104(out byte bus, out byte dev, out byte fun)
    {
        for (int b = 0; b < 64; b++)
            for (int d = 0; d < 32; d++)
            {
                uint id = PciRead((byte)b, (byte)d, 0, 0x00);
                if (id == 0xFFFFFFFF) continue;
                if ((id & 0xFFFF) == VENDOR_NV && ((id >> 16) & 0xFFFF) == DEVICE_P104)
                { bus = (byte)b; dev = (byte)d; fun = 0; return true; }
            }
        bus = dev = fun = 0; return false;
    }

    /// <summary>找 P104 的上游桥（必须扫全部 function —— PCH 端口常在 fn>0）</summary>
    static bool FindRootPort(byte tgt, out byte bus, out byte dev, out byte fun)
    {
        bus = dev = fun = 0;
        int bestSec = -1;
        for (int b = 0; b < 64; b++)
            for (int d = 0; d < 32; d++)
                for (int f = 0; f < 8; f++)
                {
                    uint id = PciRead((byte)b, (byte)d, (byte)f, 0x00);
                    if (id == 0xFFFFFFFF || (id & 0xFFFF) == 0xFFFF) continue;
                    if ((PciRead((byte)b, (byte)d, (byte)f, 0x08) >> 8) != 0x060400) continue;
                    // 0x18: Primary[7:0] Secondary[15:8] Subordinate[23:16] SecLatency[31:24]
                    uint sec = (PciRead((byte)b, (byte)d, (byte)f, 0x18) >> 8) & 0xFF;
                    uint sub = (PciRead((byte)b, (byte)d, (byte)f, 0x18) >> 16) & 0xFF;
                    if (sec <= tgt && tgt <= sub && (int)sec > bestSec)
                    {
                        bestSec = (int)sec; bus = (byte)b; dev = (byte)d; fun = (byte)f;
                    }
                }
        return bestSec >= 0;
    }

    // ================= BAR0 MMIO =================
    static ulong MapPage(uint phys)
    {
        if (hTs == INVALID) return 0;
        byte[] inb = new byte[12], outb = new byte[8];
        BitConverter.GetBytes((ulong)phys).CopyTo(inb, 0);
        BitConverter.GetBytes(PAGE_SIZE).CopyTo(inb, 8);
        uint ret;
        if (!DeviceIoControl(hTs, IOCTL_MAP_PHYS, inb, 12, outb, 8, out ret, IntPtr.Zero)) return 0;
        return BitConverter.ToUInt64(outb, 0);
    }

    /// <summary>每页只映射一次并缓存（MapPage 每次调用都会泄漏内核映射）</summary>
    static bool RemapAll()
    {
        _vaLink = MapPage(_bar + 0x8C000);
        _vaPpci = MapPage(_bar + 0x88000);
        _vaXp3g = MapPage(_bar + 0x8E000);
        _vaFuse0 = MapPage(_bar + 0x020000);
        _vaFuse1 = MapPage(_bar + 0x021000);
        _vaFuse2 = MapPage(_bar + 0x022000);
        return _vaLink != 0 && _vaPpci != 0 && _vaXp3g != 0;
    }

    static uint Mrd(ulong va, uint off)
    { return (uint)Marshal.ReadInt32((IntPtr)(long)(va + (off & 0xFFFu))); }
    static void Mwr(ulong va, uint off, uint val)
    { Marshal.WriteInt32((IntPtr)(long)(va + (off & 0xFFFu)), unchecked((int)val)); }

    // ================= 寄存器操作 =================
    static void WriteEnables()
    {
        Mwr(_vaXp3g, OFF_XP3G_OVR0, 0x00000001);
        Mwr(_vaXp3g, OFF_XP3G_OVR3, 0x00000004);
        Mwr(_vaXp3g, OFF_XP3G_VAL0, 0x00000000);
        Mwr(_vaXp3g, OFF_XP3G_VAL3, 0x00200000);

        // ★★★ 2026-10-16 修复：这是一个真 BUG 的根因 ★★★
        //
        //  resetpre 用 7/7 无反例（4 次复位 / 2 种复位机制）证明：
        //    复位【之前】PRIV_MISC_1 的 **bit15** 决定 L 能否建立：
        //        bit15 = 1  →  复位后声明锁存 0x06 (Gen2)  ✓
        //        bit15 = 0  →  复位后声明锁存 0x02 (Gen1)  ✗
        //
        //  而旧代码只「置 bit11/13、清 bit12/14」，【从不显式置 bit15】，
        //  仅靠"读旧值再 OR"【碰巧保留】它 —— 所以表现为"时好时坏"：
        //       上一次成功过      → bit15 残留 1 → 这次也成功
        //       刚开机/驱动刚重置 → bit15 = 0    → 这次失败（要再跑一次才行）
        //  实测复现：2026-10-16 18:08 重启后首次失败（初始 TLS=0），
        //            18:10 第二次成功（初始 TLS=1）。
        //
        //  ⇒ 显式置 bit15，并按"写通道必须先自检"的纪律【回读验证】。
        uint c1 = Mrd(_vaPpci, OFF_PRIV_MISC1);
        uint want = (c1 | (1u << 11) | (1u << 13) | (1u << 15)) & ~((1u << 12) | (1u << 14));
        Mwr(_vaPpci, OFF_PRIV_MISC1, want);
        uint got = Mrd(_vaPpci, OFF_PRIV_MISC1);
        if ((got & (1u << 15)) == 0)
            Log("!! PRIV_MISC_1 bit15 写入未落地（读回 0x" + got.ToString("X8")
                + "）—— 声明很可能锁存失败");

        uint c2 = Mrd(_vaPpci, OFF_VSEC_HIER);
        Mwr(_vaPpci, OFF_VSEC_HIER, (c2 & ~(1u << 12)) | 1u);
        Mwr(_vaPpci, OFF_LTSSM, 6);
    }

    /// <summary>
    /// Gen2 速率请求：只改 SPEED[19:18]=1(5GT)，★ 不带 bit0 触发位
    /// （v1.0 每 20ms 连 bit0 一起写 = 50Hz CHANGE_SPEED，实测无必要且可能是 LTSSM 不稳来源之一）。
    /// </summary>
    static void WriteSpeedGen2()
    {
        uint cur = Mrd(_vaLink, OFF_LINKCFG0);
        if (cur == 0xFFFFFFFF) return;
        uint nv = (cur & ~0x000C0000u) | (SPEED_GEN2 << 18);
        if (!Is8C040Legal(nv)) return;
        Mwr(_vaLink, OFF_LINKCFG0, nv);
    }

    // ★ 宽度声明 x16：v1.1 统一走 WidthArm()（含合法性断言 + 回读校验 + 失败整字回退）。
    static void WriteWidthX16() { WidthArm(); }

    static void WriteTlsBoth(byte rb, byte rd, byte rf, uint rcap)
    {
        uint gcap = PciCap(_bus, _dev, _fun);
        if (gcap != 0)
        {
            uint cur = PciRead(_bus, _dev, _fun, gcap + 0x30);
            if ((cur & 0xF) != 2) PciWrite(_bus, _dev, _fun, gcap + 0x30, (cur & ~0xFu) | 2u);
        }
        if (rcap != 0)
        {
            uint cur = PciRead(rb, rd, rf, rcap + 0x30);
            if ((cur & 0xF) != 2) PciWrite(rb, rd, rf, rcap + 0x30, (cur & ~0xFu) | 2u);
        }
    }

    static void RetrainBoth(byte rb, byte rd, byte rf, uint rcap)
    {
        uint gcap = PciCap(_bus, _dev, _fun);
        if (rcap != 0)
        {
            uint lc = PciRead(rb, rd, rf, rcap + 0x10);
            if (lc != 0xFFFFFFFF) PciWrite(rb, rd, rf, rcap + 0x10, lc | (1u << 5));
        }
        if (gcap != 0)
        {
            uint lc = PciRead(_bus, _dev, _fun, gcap + 0x10);
            if (lc != 0xFFFFFFFF) PciWrite(_bus, _dev, _fun, gcap + 0x10, lc | (1u << 5));
        }
    }

    // ==========================================================================
    //  v1.1 新增：判据 / 活体 / 安全写 / live 快路径 / 宽度 / 就绪门控 / 复位预算
    // ==========================================================================
    static byte _rb, _rd, _rf;
    static uint _rcap;

    static uint Lcap2() { uint g = PciCap(_bus, _dev, _fun); return g == 0 ? 0xFFFFFFFFu : PciRead(_bus, _dev, _fun, g + 0x2C); }
    static uint LnkCap() { uint g = PciCap(_bus, _dev, _fun); return g == 0 ? 0xFFFFFFFFu : PciRead(_bus, _dev, _fun, g + 0x0C); }
    static uint LnkSta() { uint g = PciCap(_bus, _dev, _fun); return g == 0 ? 0xFFFFFFFFu : PciRead(_bus, _dev, _fun, g + 0x12); }
    static uint LnkCtl2() { uint g = PciCap(_bus, _dev, _fun); return g == 0 ? 0xFFFFFFFFu : PciRead(_bus, _dev, _fun, g + 0x30); }
    static bool J1() { return (Lcap2() & 0x6) == 0x6; }
    static bool J2() { return (LnkSta() & 0xF) >= 2; }
    static bool J3() { return ((LnkCap() >> 4) & 0x3F) == 16; }
    static long NowTick() { return Environment.TickCount & 0x7FFFFFFF; }

    /// <summary>活体检查：读 config 0x00（正确偏移，不是 cap+0）。3 连读失败才算掉卡。</summary>
    static bool Alive()
    {
        for (int i = 0; i < 3; i++)
        {
            if (PciRead(_bus, _dev, _fun, 0x00) == 0x1B8710DE) return true;
            Thread.Sleep(100);
        }
        return false;
    }

    /// <summary>0x8C040 写值合法性：实测 bit31=0&amp;bit29=0 掉卡 2/2，bit31=1&amp;bit29=1 掉卡 1/1。</summary>
    static bool Is8C040Legal(uint v)
    {
        bool arm31 = (v & 0x80000000u) != 0;
        bool bit29 = (v & 0x20000000u) != 0;
        return arm31 || bit29;
    }

    /// <summary>读-改-写 + 20ms 后回读校验。已是目标值则不重复写（减少触发类写）。</summary>
    static bool RmwVerify(ulong va, uint off, uint mask, uint value, string name)
    {
        uint old = Mrd(va, off);
        if (old == 0xFFFFFFFF || (old >> 16) == 0xBADF)
        {
            Log("!! 读 " + name + " 失败（0x" + old.ToString("X8") + "，链路可能已断）—— 中止写入");
            return false;
        }
        uint nv = (old & ~mask) | (value & mask);
        if (nv == old) return true;
        Mwr(va, off, nv);
        Thread.Sleep(20);
        uint rb = Mrd(va, off);
        bool ok = (rb & mask) == (value & mask);
        Log("  " + name + ": 0x" + old.ToString("X8") + " -> 0x" + nv.ToString("X8")
            + " 回读 0x" + rb.ToString("X8") + (ok ? "" : "  !! 未落地"));
        return ok;
    }

    /// <summary>
    /// ★ v1.1 主路径：无复位 live 写组。
    ///   顺序照 2026-10-28/29 实测生效的那一版（width16_hold.py:291-323）：
    ///   0x8872C→0x2241C→0x8841C→0x8C2C0→0x8C300→0x8C040→0x8C1C0→0x88610
    ///   实测：写完后 LCAP2 秒级 0x02→0x06，随后两端 TLS+retrain 即上 Gen2，保持 29 分钟。
    ///   注意：这里【不】写 XP3G(0x8E11x)，也【不】动 bit15/bit11（那是复位路径的写法）。
    /// </summary>
    static bool ApplyLiveGroup(string why)
    {
        Log("[PLAN] live 写组（" + why + "）");
        bool ok = true;
        ok &= RmwVerify(_vaPpci, OFF_LTSSM, 0xFu, 6u, "0x8872C");
        ok &= RmwVerify(_vaFuse2, OFF_PTOP_FS4, 0x81u, 0x81u, "0x2241C");
        ok &= RmwVerify(_vaPpci, OFF_PRIV_MISC1, 0x6000u, 0u, "0x8841C");
        ok &= RmwVerify(_vaLink, OFF_CYA0, 0x4u, 0u, "0x8C2C0");
        ok &= RmwVerify(_vaLink, OFF_CYA1, 0x10u, 0u, "0x8C300");
        ok &= RmwVerify(_vaLink, OFF_LINKCFG0, 0xC0000u, 0x40000u, "0x8C040(speed)");
        ok &= RmwVerify(_vaLink, OFF_LCTRL2, 0xF0000u, 0x20000u, "0x8C1C0");
        ok &= RmwVerify(_vaPpci, OFF_VSEC_HIER, 0x1001u, 0x1u, "0x88610");
        return ok;
    }

    /// <summary>live 写组 + 2 秒内轮询 LCAP2；成功即返回 true（无需复位）。</summary>
    static bool LiveLatch(string phase)
    {
        for (int round = 1; round <= T_LIVE_ROUNDS; round++)
        {
            if (!Alive()) { Log("!! live 写组前活体检查失败"); return false; }
            ApplyLiveGroup(phase + " 第" + round + "轮");
            int waited = 0;
            while (waited < T_LIVE_LATCH_MS)
            {
                uint l2 = Lcap2();
                if ((l2 & 0x6) == 0x6)
                {
                    Log("★ live 声明已锁存为 Gen2（" + phase + " 第" + round + "轮 / " + waited + "ms，LCAP2=0x" + l2.ToString("X8") + "）");
                    return true;
                }
                Thread.Sleep(100); waited += 100;
            }
            Log("  live 第" + round + "轮未锁存（LCAP2=0x" + Lcap2().ToString("X8") + "），300ms 后重试");
            Thread.Sleep(300);
        }
        return false;
    }

    /// <summary>
    /// 宽度声明 x16（只在 Gen2 已达成后调用）。
    ///   安全配方：new = (cur &amp; ~0xF8000000) | 0x80000000（清 bits27-31 再置 bit31）
    ///   ★ 绝不用掩码单 bit 清 bit31（会写出 bit31=0&amp;bit29=0 → 实测掉卡）。
    /// </summary>
    static bool WidthArm()
    {
        uint cur = Mrd(_vaLink, OFF_LINKCFG0);
        if (cur == 0xFFFFFFFF) { Log("!! 宽度写前读 0x8C040 失败"); return false; }
        if ((cur & 0x80000000u) != 0 && J3()) return true;     // 已 arm 且已生效
        uint nv = (cur & ~0xF8000000u) | 0x80000000u;
        if (!Is8C040Legal(nv)) { Log("!! 拒绝写入非法 0x8C040 值 0x" + nv.ToString("X8")); return false; }
        Mwr(_vaLink, OFF_LINKCFG0, nv);
        Thread.Sleep(50);
        uint rb = Mrd(_vaLink, OFF_LINKCFG0);
        if ((rb & 0x80000000u) == 0) { Log("!! 宽度 arm 未落地（回读 0x" + rb.ToString("X8") + "）"); return false; }
        int t = 0;
        while (t < 1000)
        {
            uint lc = LnkCap();
            if (((lc >> 4) & 0x3F) == 16)
            {
                Log("★ 宽度声明 x16 已生效（LNKCAP=0x" + lc.ToString("X8") + "）");
                return true;
            }
            Thread.Sleep(100); t += 100;
        }
        Mwr(_vaLink, OFF_LINKCFG0, LINKCFG0_SAFE);            // 整字安全回退
        Log("宽度声明未生效，已整字回退 0x8C040=0x" + LINKCFG0_SAFE.ToString("X8") + "（不影响 Gen2）");
        return false;
    }

    /// <summary>
    /// ★ 实测教训（2026-10-30 23:23）：宽度已 arm（0x8C040 bit31=1）而链路停在 Gen1 时，
    ///   直接 retrain 升不了速（armed 状态会干扰速率协商）。必须先"整字回退"到工厂安全值
    ///   0x20085800（bit31=0, bit29=1，实测安全），再提速，成功后再重新 arm 宽度。
    ///   绝不能单 bit 清 bit31（会写出 bit31=0&amp;bit29=0 → 掉卡）。
    /// </summary>
    static void DisarmWidthForSpeed()
    {
        uint cur = Mrd(_vaLink, OFF_LINKCFG0);
        if (cur == 0xFFFFFFFF) return;
        if ((cur & 0x80000000u) == 0) return;
        Log("宽度已 arm 但链路在 Gen1 —— 先整字回退 0x8C040=0x" + LINKCFG0_SAFE.ToString("X8") + " 再提速");
        Mwr(_vaLink, OFF_LINKCFG0, LINKCFG0_SAFE);
        Thread.Sleep(100);
    }

    /// <summary>就绪门控：设备 + NVIDIA 驱动（用 nvidia-smi 子进程，★ 不做 cuInit）。</summary>
    static bool NvidiaSmiReady()
    {
        string r = Run("nvidia-smi.exe", "-L", 10000);
        return r.IndexOf("P104", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    static bool WaitReady(int timeoutMs)
    {
        int waited = 0;
        while (waited < timeoutMs)
        {
            if (EnsureDevices() && FindP104(out _bus, out _dev, out _fun)) break;
            if (waited == 0) Log("设备/驱动尚未就绪，开始等待（最多 " + (timeoutMs / 1000) + " 秒）…");
            Thread.Sleep(2000); waited += 2000;
        }
        if (waited >= timeoutMs) { Log("等待 " + (timeoutMs / 1000) + " 秒后仍找不到 P104-100 (10DE:1B87)"); return false; }
        if (waited > 0) Log("设备已就绪（等待 " + (waited / 1000) + " 秒）");
        int w2 = 0;
        while (w2 < timeoutMs)
        {
            if (NvidiaSmiReady()) { if (w2 > 0) Log("NVIDIA 驱动就绪（等待 " + (w2 / 1000) + " 秒）"); return true; }
            Thread.Sleep(3000); w2 += 3000;
        }
        Log("nvidia-smi 在 " + (timeoutMs / 1000) + " 秒内未就绪 —— 继续尝试，但失败风险升高");
        return true;
    }

    /// <summary>掉卡判定：config 0x00 三连读失败 + nvidia-smi 交叉确认（修掉历史误报）。</summary>
    static bool IsLost()
    {
        if (Alive()) return false;
        return !NvidiaSmiReady();
    }

    /// <summary>
    /// 等保持进程把链路恢复到 "Gen2 + 宽度声明 x16"（只读状态文件，不需要驱动）。
    /// 用于 -bw 测试结束后：CUDA 上下文会让链路临时降速，等它恢复再退出，避免用户误以为解锁失效。
    /// </summary>
    static bool WaitHolderRestored(int timeoutMs)
    {
        int waited = 0;
        while (waited < timeoutMs)
        {
            try
            {
                if (File.Exists(StatusOkFile))
                {
                    string all = File.ReadAllText(StatusOkFile, Encoding.UTF8);
                    bool gen2 = all.IndexOf("当前链路速率 : Gen2", StringComparison.Ordinal) >= 0;
                    bool w16 = all.IndexOf("MAXWIDTH=16", StringComparison.Ordinal) >= 0;
                    if (gen2 && w16) return true;
                }
            }
            catch { }
            Thread.Sleep(1000); waited += 1000;
        }
        return false;
    }

    static void SaveSnapshot()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(StateFile));
            var sb = new StringBuilder();
            sb.AppendLine("# P104Gen2x4 v1.1 只读基线快照 " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
            sb.AppendLine("lnkcap=0x" + LnkCap().ToString("X8"));
            sb.AppendLine("lnksta=0x" + LnkSta().ToString("X8"));
            sb.AppendLine("lcap2=0x" + Lcap2().ToString("X8"));
            sb.AppendLine("lnkctl2=0x" + LnkCtl2().ToString("X8"));
            sb.AppendLine("8841C=0x" + Mrd(_vaPpci, OFF_PRIV_MISC1).ToString("X8"));
            sb.AppendLine("2241C=0x" + Mrd(_vaFuse2, OFF_PTOP_FS4).ToString("X8"));
            sb.AppendLine("8872C=0x" + Mrd(_vaPpci, OFF_LTSSM).ToString("X8"));
            sb.AppendLine("8C040=0x" + Mrd(_vaLink, OFF_LINKCFG0).ToString("X8"));
            sb.AppendLine("8C1C0=0x" + Mrd(_vaLink, OFF_LCTRL2).ToString("X8"));
            sb.AppendLine("8C2C0=0x" + Mrd(_vaLink, OFF_CYA0).ToString("X8"));
            sb.AppendLine("8C300=0x" + Mrd(_vaLink, OFF_CYA1).ToString("X8"));
            sb.AppendLine("88610=0x" + Mrd(_vaPpci, OFF_VSEC_HIER).ToString("X8"));
            File.WriteAllText(StateFile, sb.ToString(), Encoding.UTF8);
        }
        catch { }
    }

    // ---------- 复位预算（跨进程 / 跨开机持久化，防止"反复黑屏"）----------
    static void BudgetLoad()
    {
        long now = NowTick();
        _bootTick = now;
        try
        {
            if (File.Exists(BudgetFile))
            {
                foreach (string ln in File.ReadAllLines(BudgetFile))
                {
                    string[] p = ln.Split('=');
                    if (p.Length != 2) continue;
                    long v; if (!long.TryParse(p[1], out v)) continue;
                    if (p[0] == "boot") _bootTick = v;
                    else if (p[0] == "lastReset") _lastResetTick = v;
                    else if (p[0] == "resetsThisBoot") _resetsThisBoot = (int)v;
                }
            }
        }
        catch { }
        if (now < _bootTick) { _bootTick = now; _resetsThisBoot = 0; _lastResetTick = -100000000; }
    }

    static void BudgetSave()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(BudgetFile));
            File.WriteAllText(BudgetFile,
                "boot=" + _bootTick + Environment.NewLine +
                "lastReset=" + _lastResetTick + Environment.NewLine +
                "resetsThisBoot=" + _resetsThisBoot + Environment.NewLine,
                Encoding.UTF8);
        }
        catch { }
    }

    static bool CanResetNow()
    {
        if (!AllowReset) { Log("复位已禁用（-noreset）"); return false; }
        if (_resetUsedThisRun >= Math.Min(ResetRetries, MAX_RESETS_PER_RUN))
        { Log("本次运行复位预算已用尽（" + _resetUsedThisRun + " 次）"); return false; }
        if (_resetsThisBoot >= MAX_RESETS_PER_BOOT)
        { Log("本开机周期复位次数已达上限 " + MAX_RESETS_PER_BOOT + " 次 —— 不再复位（避免反复黑屏/掉卡）"); return false; }
        long now = NowTick();
        if (_lastResetTick >= 0 && (now - _lastResetTick) < RESET_MIN_GAP_MS)
        { Log("距上次复位不足 " + (RESET_MIN_GAP_MS / 60000) + " 分钟 —— 本次不再复位"); return false; }
        return true;
    }

    static bool PnpDisable(int holdMs)
    {
        var list = ListPresentP104();
        if (list.Count == 0) { Log("找不到在线的 P104 设备实例"); return false; }
        Log("设备实例 = " + list[0].Key);
        if (!PropertyChange(list[0].Value, DICS_DISABLE)) { Log("禁用失败（错误 " + Marshal.GetLastWin32Error() + "）"); return false; }
        Thread.Sleep(holdMs);
        uint prob;
        GetDevNodeStatus(list[0].Value, out prob);
        Log("已禁用并保持 " + holdMs + "ms（problem=" + prob + "）");
        return true;
    }

    static bool PnpEnable()
    {
        var list = ListPresentP104();
        if (list.Count == 0) { Log("启用前找不到 P104 实例（可能已从总线消失）"); return false; }
        if (!PropertyChange(list[0].Value, DICS_ENABLE)) { Log("启用失败（错误 " + Marshal.GetLastWin32Error() + "）"); return false; }
        Log("已启用，等设备回到总线…");
        return true;
    }

    static bool WaitBack()
    {
        Thread.Sleep(2000);
        for (int i = 0; i < 30; i++)
        {
            if (hWr == INVALID || hTs == INVALID) EnsureDevices();
            if (hWr != INVALID && FindP104(out _bus, out _dev, out _fun) &&
                PciRead(_bus, _dev, _fun, 0x00) == 0x1B8710DE)
            {
                Log("设备已回到总线（约 " + (2 + i) + " 秒）");
                return true;
            }
            Thread.Sleep(1000);
        }
        return false;
    }

    /// <summary>刷新全部引用：P104 BDF / BAR0 / BAR0 映射 / 上游根端口。</summary>
    static bool RefreshRefs()
    {
        _rb = _rd = _rf = 0; _rcap = 0;
        if (!FindP104(out _bus, out _dev, out _fun)) return false;
        _bar = PciRead(_bus, _dev, _fun, 0x10) & 0xFFFFFFF0;
        if (!RemapAll()) return false;
        if (FindRootPort(_bus, out _rb, out _rd, out _rf)) _rcap = PciCap(_rb, _rd, _rf);
        return true;
    }

    /// <summary>整卡复位（v1.1）：先写前置、再关 MMIO、再禁用→保持 6s→启用→等回来→重映射。</summary>
    static bool DoCardReset()
    {
        Log("=== 开始整卡复位（本次运行第 " + (_resetUsedThisRun + 1) + " 次 / 本开机周期第 " + (_resetsThisBoot + 1) + " 次）===");
        WriteDriverPolicy();
        WriteEnables();                       // 复位前置：0x8841C bit15=1（实测 7/7 必要条件）
        Thread.Sleep(300);
        CloseAll();                           // ★ 先关 MMIO/句柄再禁用设备（v1.0 是禁用后才关）
        _resetUsedThisRun++; _resetsThisBoot++; _lastResetTick = NowTick();
        BudgetSave();
        if (!PnpDisable(6000)) return false;
        if (!PnpEnable()) return false;
        if (!WaitBack()) { Log("!! 设备 30 秒内没有回到总线 —— 需要 AC 断电"); return false; }
        if (!RefreshRefs()) { Log("!! 复位后 MMIO 重映射失败"); return false; }
        Log("复位后: " + StateLine(_rb, _rd, _rf, _rcap));
        int t = 0;
        while (t < T_RESET_LATCH_MS)
        {
            uint l2 = Lcap2();
            if ((l2 & 0x6) == 0x6) { Log("★ 复位后声明已锁存（" + t + "ms，LCAP2=0x" + l2.ToString("X8") + "）"); return true; }
            Thread.Sleep(500); t += 500;
        }
        Log("复位后 8 秒未锁存（LCAP2=0x" + Lcap2().ToString("X8") + "）—— 交给 live 写组补刀");
        return true;
    }

    /// <summary>还原到出厂寄存器值（-off 用；整字写，绝不用掩码单 bit 清）。</summary>
    static void RestoreAll()
    {
        if (hWr == INVALID || hTs == INVALID) { Log("驱动未就绪，无法还原寄存器"); return; }
        Mwr(_vaLink, OFF_LINKCFG0, LINKCFG0_SAFE);
        Mwr(_vaPpci, OFF_PRIV_MISC1, 0xC0B46500);
        Mwr(_vaPpci, OFF_VSEC_HIER, 0x00000000);
        Mwr(_vaPpci, OFF_LTSSM, 0x00000000);
        Mwr(_vaFuse2, OFF_PTOP_FS4, 0x00000000);
        Mwr(_vaLink, OFF_LCTRL2, 0x00210036);
        Mwr(_vaLink, OFF_CYA0, 0x068711B3);
        Mwr(_vaLink, OFF_CYA1, 0x040A00C0);
        Log("已还原寄存器（0x8841C/0x2241C/0x8872C/0x88610/0x8C040/0x8C1C0/0x8C2C0/0x8C300）");
    }

    /// <summary>自检：四个判据 + 一句话 PASS/FAIL（写进日志，-once 直接可见）。</summary>
    static bool SelfCheck()
    {
        uint lc = LnkCap(), ls = LnkSta(), l2 = Lcap2(), c2 = LnkCtl2();
        uint maxspd = lc & 0xF, maxwid = (lc >> 4) & 0x3F;
        uint curspd = ls & 0xF, curwid = (ls >> 4) & 0x3F;
        Log("================ 自检 ================");
        Log("  LNKCAP = 0x" + lc.ToString("X8") + "  maxspd=" + SpeedName(maxspd) + "  maxwidth=x" + maxwid);
        Log("  LNKSTA = 0x" + ls.ToString("X8") + "  cur=" + SpeedName(curspd) + "  width=x" + curwid);
        Log("  LCAP2  = 0x" + l2.ToString("X8") + "  " + ((l2 & 0x6) == 0x6 ? "含 Gen2 声明" : "仅 Gen1 声明"));
        Log("  TLS    = " + (c2 & 0xF));
        bool ok = (l2 & 0x6) == 0x6 && curspd >= 2;
        bool w16 = maxwid == 16;
        Log(ok
            ? ("RESULT: PASS —— Gen2 已生效（实际链路 " + SpeedName(curspd) + " x" + curwid + "）"
               + (w16 ? " + 宽度声明 x16" : "（宽度声明未生效）"))
            : ("RESULT: FAIL —— 未达成 Gen2（当前 " + SpeedName(curspd) + " x" + curwid + "）"));
        Log("====================================");
        return ok;
    }

    static string StateLine(byte rb, byte rd, byte rf, uint rcap)
    {
        uint gcap = PciCap(_bus, _dev, _fun);
        uint lc = PciRead(_bus, _dev, _fun, gcap + 0x0C);
        uint ls = PciRead(_bus, _dev, _fun, gcap + 0x12);
        uint l2 = PciRead(_bus, _dev, _fun, gcap + 0x2C);
        uint c2 = PciRead(_bus, _dev, _fun, gcap + 0x30);
        string s = string.Format("LNKCAP=0x{0:X8}({1} x{2}) LNKSTA=0x{3:X8}({4} x{5}) LCAP2=0x{6:X8} TLS={7}",
            lc, SpeedName(lc & 0xF), (lc >> 4) & 0x3F, ls, SpeedName(ls & 0xF), (ls >> 4) & 0x3F, l2, c2 & 0xF);
        return s;
    }

    static string SpeedName(uint f)
    {
        return f == 1 ? "2.5GT/s" : f == 2 ? "5.0GT/s" : f == 3 ? "8.0GT/s" : f == 4 ? "16GT/s" : "?";
    }

    // ================= ★★ PnP 禁用/启用 =================
    static bool GetDevNodeStatus(uint devInst, out uint problem)
    {
        uint status;
        return CM_Get_DevNode_Status(out status, out problem, devInst, 0) == 0 && (status & DN_HAS_PROBLEM) != 0;
    }

    /// <summary>列出所有 Present 的 P104 设备实例。</summary>
    static List<KeyValuePair<string, uint>> ListPresentP104()
    {
        var res = new List<KeyValuePair<string, uint>>();
        IntPtr set = SetupDiGetClassDevs(IntPtr.Zero, null, IntPtr.Zero, DIGCF_PRESENT | DIGCF_ALLCLASSES);
        if (set == INVALID) return res;
        try
        {
            var did = new SP_DEVINFO_DATA();
            did.cbSize = (uint)Marshal.SizeOf(did);
            for (uint i = 0; SetupDiEnumDeviceInfo(set, i, ref did); i++)
            {
                var sb = new StringBuilder(512);
                uint need;
                if (!SetupDiGetDeviceInstanceId(set, ref did, sb, 512, out need)) continue;
                string id = sb.ToString().ToUpperInvariant();
                if (id.Contains("VEN_10DE") && id.Contains("DEV_1B87"))
                    res.Add(new KeyValuePair<string, uint>(sb.ToString(), did.DevInst));
            }
        }
        finally { SetupDiDestroyDeviceInfoList(set); }
        return res;
    }

    static bool PropertyChange(uint devInst, uint stateChange)
    {
        IntPtr set = SetupDiGetClassDevs(IntPtr.Zero, null, IntPtr.Zero, DIGCF_PRESENT | DIGCF_ALLCLASSES);
        if (set == INVALID) return false;
        try
        {
            var did = new SP_DEVINFO_DATA();
            did.cbSize = (uint)Marshal.SizeOf(did);
            for (uint i = 0; SetupDiEnumDeviceInfo(set, i, ref did); i++)
            {
                if (did.DevInst != devInst) continue;
                var pp = new SP_PROPCHANGE_PARAMS();
                pp.ClassInstallHeader.cbSize = (uint)Marshal.SizeOf(pp.ClassInstallHeader);
                pp.ClassInstallHeader.InstallFunction = DIF_PROPERTYCHANGE;
                pp.StateChange = stateChange;
                pp.Scope = DICS_FLAG_GLOBAL;
                pp.HwProfile = 0;
                if (!SetupDiSetClassInstallParams(set, ref did, ref pp, (uint)Marshal.SizeOf(pp))) return false;
                return SetupDiCallClassInstaller(DIF_PROPERTYCHANGE, set, ref did);
            }
        }
        finally { SetupDiDestroyDeviceInfoList(set); }
        return false;
    }

    /// <summary>
    /// ★★★ 本工具的核心：真正的整卡复位。
    /// 设备会从总线上消失，然后再回来 —— 只有这样才能让 PCIe 核
    /// 重新锁存「支持速率声明」为 Gen2。
    /// </summary>
    static bool PnPReset()
    {
        var list = ListPresentP104();
        if (list.Count == 0) { Log("找不到在线的 P104 设备实例"); return false; }
        var me = list[0];
        Log("设备实例 = " + me.Key);

        // 1) 禁用
        if (!PropertyChange(me.Value, DICS_DISABLE))
        {
            Log("禁用失败（错误 " + Marshal.GetLastWin32Error() + "）");
            return false;
        }
        bool gone = false;
        for (int i = 0; i < 30; i++)
        {
            Thread.Sleep(200);
            uint id = PciRead(_bus, _dev, _fun, 0x00);
            if (id != 0x1B8710DE) { gone = true; break; }
        }
        uint prob;
        GetDevNodeStatus(me.Value, out prob);
        Log("已禁用（设备离开总线=" + gone + "，problem=" + prob + "）");

        // 2) 启用
        var list2 = ListPresentP104();
        uint inst = me.Value;
        if (list2.Count > 0) inst = list2[0].Value;
        if (!PropertyChange(inst, DICS_ENABLE))
        {
            Log("启用失败（错误 " + Marshal.GetLastWin32Error() + "）");
            return false;
        }

        // 3) 等它回来
        bool back = false;
        for (int i = 0; i < 60; i++)
        {
            Thread.Sleep(500);
            uint id = PciRead(_bus, _dev, _fun, 0x00);
            if (id == 0x1B8710DE)
            {
                Log(string.Format("设备已回到总线（{0:F1}s）", (i + 1) * 0.5));
                back = true;
                break;
            }
        }
        if (!back)
        {
            Log("!! 设备 30 秒内没有回来 —— 需要重启机器");
            return false;
        }

        // 4) ★★★ 关键：等声明锁存 ★★★
        //   实测（2026-10-15 反复验证，共 4 次对照）：
        //     复位后只等 2-3 秒读 → LCAP2 仍是 0x02（Gen1）→ 判定失败
        //     复位后等 25 秒读    → LCAP2 变成 0x06（Gen2）→ 成功
        //
        //   原因：需要 NVIDIA 驱动重新绑定到设备、读取 RMPcieLinkSpeed 策略，
        //         然后才会把声明锁存成 Gen2。这个过程要十几到二十几秒。
        //
        //   ⇒ 这是"时好时坏"的真正原因。以前偶尔成功，是因为那时碰巧等够了。
        Log("等待声明锁存（最多 20 秒，每 4 秒检查一次）…");
        uint gcap2 = PciCap(_bus, _dev, _fun);
        for (int i = 1; i <= 5; i++)
        {
            Thread.Sleep(4000);
            uint t = PciRead(_bus, _dev, _fun, gcap2 + 0x2C);
            if ((t & 0x6) == 0x6)
            {
                Log(string.Format("★ 声明已锁存为 Gen2（等待 {0} 秒后 LCAP2=0x{1:X8}）", i * 4, t));
                return true;
            }
        }
        Log("声明锁存超时（20 秒后仍是 Gen1）");
        return true;
    }

    static void WriteInitStatus()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(StatusOkFile));
            var sb = new StringBuilder();
            sb.AppendLine("============================================================");
            sb.AppendLine("P104-100 PCIe 链路状态（按住进程实时写入）");
            sb.AppendLine("更新时间 : " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
            sb.AppendLine("============================================================");
            sb.AppendLine();
            sb.AppendLine("★ 当前链路速率 : 未确认（正在解锁/重试中）");
            sb.AppendLine("  当前链路宽度 : 未知");
            sb.AppendLine();
            sb.AppendLine("  说明：开机/重启后先看到这个 = 正常。等 1-2 分钟再查。");
            File.WriteAllText(StatusOkFile, sb.ToString(), Encoding.UTF8);
        }
        catch { }
    }
    // ================= 解锁流程 =================
    // ================= 解锁流程（v1.1 状态机：live 快路径优先 → 复位兜底）=================
    static bool UnlockOnce()
    {
        WriteInitStatus();
        Log("=== 解锁流程开始（live 快路径优先；整卡复位只在 live 连续失败后兜底 1 次）===");
        Log("当前: " + StateLine(_rb, _rd, _rf, _rcap));

        bool j1 = J1(), j2 = J2(), j3 = J3();
        if (j1 && j2 && j3)
        {
            Log("★ 已经是 Gen2 x4 + 宽度声明 x16 —— 无需任何写入，直接进入保持");
            return SelfCheck();
        }

        if (!j1)
        {
            // S2 无复位快路径
            j1 = LiveLatch("无复位快路径");
            if (!j1)
            {
                // S3 复位兜底（受预算/限流约束）
                if (CanResetNow())
                {
                    if (DoCardReset()) j1 = J1() || LiveLatch("复位后补刀");
                }
                else Log("live 路径失败，且复位预算不足 —— 放弃本次解锁（不再复位）");
            }
            if (!j1)
            {
                Log("!! live + 复位都没能把声明翻到 Gen2（LCAP2=0x" + Lcap2().ToString("X8") + "）");
                return SelfCheck();
            }
        }
        else Log("声明已是 Gen2（LCAP2=0x" + Lcap2().ToString("X8") + "），直接做链路提速");

        // S4 两端 TLS=2 + 根端口 retrain
        // ★ 若宽度已 arm 而链路停在 Gen1：必须先整字回退（armed 状态下 retrain 升不了速）。
        if (!J2()) { DisarmWidthForSpeed(); _lastTriggerTick = -100000; }
        for (int round = 1; round <= T_RETRAIN_ROUNDS; round++)
        {
            WriteTlsBoth(_rb, _rd, _rf, _rcap);
            RetrainBoth(_rb, _rd, _rf, _rcap);          // 每轮间隔 ≥3 秒，天然满足触发限流
            _lastTriggerTick = NowTick();
            int t = 0;
            while (t < T_RETRAIN_OBSERVE_MS) { if (J2()) break; Thread.Sleep(50); t += 50; }
            Log("  TLS/retrain 第" + round + "轮: " + StateLine(_rb, _rd, _rf, _rcap));
            if (J2()) break;
            Thread.Sleep(1000);
        }

        if (!J2())
        {
            Log("!! 声明有了但链路没上 Gen2 —— 再补一轮 live 写 + retrain（不复位）");
            if (LiveLatch("链路未升速补写"))
            {
                DisarmWidthForSpeed();
                WriteTlsBoth(_rb, _rd, _rf, _rcap);
                RetrainBoth(_rb, _rd, _rf, _rcap);
                _lastTriggerTick = NowTick();
                int t = 0; while (t < 4000) { if (J2()) break; Thread.Sleep(50); t += 50; }
            }
        }
        if (!J2()) { Log("!! 链路仍未升速（" + StateLine(_rb, _rd, _rf, _rcap) + "）"); return SelfCheck(); }
        Log("★ 链路已上 Gen2（LNKSTA=0x" + LnkSta().ToString("X8") + "）");

        // S5 宽度声明 x16（Gen2 稳定 2 秒后）
        if (WidthEnabled)
        {
            bool stable = true;
            for (int i = 0; i < 4; i++)
            {
                Thread.Sleep(500);
                if (!J1() || !J2()) { stable = false; break; }
            }
            if (stable) WidthArm();
            else Log("链路/声明不稳定，本次暂不做宽度声明（常驻看门狗会继续补）");
        }
        else Log("宽度声明已关闭（-nowidth）");

        // S6 自检
        return SelfCheck();
    }

    // ================= 驱动策略 =================
    static string FindClassKey()
    {
        string baseKey = @"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}";
        try
        {
            var root = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(baseKey);
            if (root == null) return null;
            foreach (string sub in root.GetSubKeyNames())
            {
                var k = root.OpenSubKey(sub);
                if (k == null) continue;
                object m = k.GetValue("MatchingDeviceId");
                if (m != null && m.ToString().ToLower().Contains("1b87"))
                    return baseKey + "\\" + sub;
            }
        }
        catch { }
        return null;
    }

    static void WriteDriverPolicy()
    {
        string key = FindClassKey();
        if (key == null) { Log("找不到 P104 的显卡类键（跳过驱动策略）"); return; }
        try
        {
            using (var k = Microsoft.Win32.Registry.LocalMachine.CreateSubKey(key))
            {
                k.SetValue("RMPcieLinkSpeed", 1, Microsoft.Win32.RegistryValueKind.DWord);
                k.SetValue("RmForceEnableGen2", 1, Microsoft.Win32.RegistryValueKind.DWord);
                k.SetValue("RmEnableGen2", 1, Microsoft.Win32.RegistryValueKind.DWord);
            }
            Log("驱动策略已写入 " + key + "  (RMPcieLinkSpeed=1)");
        }
        catch (Exception e) { Log("写驱动策略失败: " + e.Message); }
    }

    static void ClearDriverPolicy()
    {
        string key = FindClassKey();
        if (key == null) return;
        try
        {
            using (var k = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(key, true))
            {
                if (k == null) return;
                foreach (string n in new[] { "RMPcieLinkSpeed", "RmForceEnableGen2", "RmEnableGen2" })
                    try { k.DeleteValue(n, false); } catch { }
            }
            Log("驱动策略已清除");
        }
        catch { }
    }

    static bool IsAdmin()
    {
        try
        {
            var id = System.Security.Principal.WindowsIdentity.GetCurrent();
            return new System.Security.Principal.WindowsPrincipal(id)
                .IsInRole(System.Security.Principal.WindowsBuiltInRole.Administrator);
        }
        catch { return false; }
    }

    /// <summary>
    /// 状态查询。★ 设计要点：
    ///   1) 优先用【不需要管理员】的方式判断（进程 / 计划任务 / 工具自己的日志）
    ///   2) 失败信息只在真的失败时出现，不会因为"没管理员权限"就吓人
    ///   3) 只有需要读寄存器时才提权限；此时给出明确的一句提示
    /// </summary>
    static int DoCheck()
    {
        // v1.1：不强制 UTF-8，让控制台用自己的代码页（zh-CN 默认 936）—— 中文/子进程输出都不乱码。
        bool admin = IsAdmin();
        // ★ 必须排除自身：本进程也叫 P104Gen2x4，否则 status 永远误报"按住进程运行中"。
        int myPid = Process.GetCurrentProcess().Id;
        var procs = new List<Process>();
        foreach (var p in Process.GetProcessesByName("P104Gen2x4"))
            if (p.Id != myPid) procs.Add(p);
        bool holder = procs.Count > 0;

        // ★ 计划任务查询分三种结果（修 BUG 2：非管理员曾误报"未注册"）：
        //     taskOk=true                  → 已注册
        //     taskOk=false && admin        → 确实未注册（管理员有权查询）
        //     taskOk=false && !admin       → **无法查询**（不能说成"未注册"）
        string t = Run("schtasks.exe", "/query /tn \"" + TaskName + "\" /fo LIST");
        bool taskOk = t.IndexOf(TaskName, StringComparison.OrdinalIgnoreCase) >= 0;
        bool taskUnknown = !taskOk && !admin;

        // ---------- ① 实时状态文件（按住进程每秒刷新，不需要管理员）----------
        string realGen = null;
        uint realSpd = 0, realWidth = 0, realMaxWidth = 0;
        string realTime = null;
        DateTime realStamp = DateTime.MinValue;
        bool hasStamp = false;
        bool pending = false;
        try
        {
            if (File.Exists(StatusOkFile))
            {
                foreach (string ln in File.ReadAllLines(StatusOkFile, Encoding.UTF8))
                {
                    if (ln.Contains("更新时间")) { realTime = ln.Substring(ln.IndexOf(':') + 1).Trim(); hasStamp = DateTime.TryParse(realTime, out realStamp); }
                    else if (ln.Contains("当前链路速率"))
                    {
                        if (ln.Contains("Gen3")) realGen = "Gen3";
                        else if (ln.Contains("Gen2")) realGen = "Gen2";
                        else if (ln.Contains("Gen1")) realGen = "Gen1";
                        if (ln.Contains("8.0GT/s")) realSpd = 3;
                        else if (ln.Contains("5.0GT/s")) realSpd = 2;
                        else if (ln.Contains("2.5GT/s")) realSpd = 1;
                        if (realGen == null) pending = true;
                    }
                    else if (ln.Contains("当前链路宽度"))
                    {
                        var mw1 = Regex.Match(ln, @"x\s*(\d+)");
                        if (mw1.Success) uint.TryParse(mw1.Groups[1].Value, out realWidth);
                    }
                    else if (ln.Contains("MAXWIDTH="))
                    {
                        var mw2 = Regex.Match(ln, @"MAXWIDTH=(\d+)");
                        if (mw2.Success) uint.TryParse(mw2.Groups[1].Value, out realMaxWidth);
                    }
                }
            }
        }
        catch { }

        bool stale = hasStamp && (DateTime.Now - realStamp).TotalSeconds > 15;
        string staleMsg = null;
        if (stale) { staleMsg = "状态数据已过期（上次更新 " + realTime + "，超过 15 秒未刷新），当前状态未知"; realGen = null; pending = false; }

        // ---------- ② 回退：从日志读最近一次实测 ----------
        var st = ReadLastStateFromLog();

        string resGen = null; uint resSpd = 0, resWid = 0;
        if (realGen != null) { resGen = realGen; resSpd = realSpd; resWid = realWidth; }
        else if (st != null) { resGen = SpeedName(st.Item2); resSpd = st.Item2; resWid = st.Item3; }

        string resultLine;
        if (!holder) resultLine = "UNKNOWN   (hold process NOT running - link will fall back to Gen1)";
        else if (stale) resultLine = "UNKNOWN   (status data is stale - re-run in a few seconds)";
        else if (resGen == null) resultLine = "UNKNOWN   (initializing / retrying - wait 1-2 minutes)";
        else if (resSpd >= 3) resultLine = string.Format("Gen3 x{0}  (8.0 GT/s)   <-- UNLOCKED", resWid);
        else if (resSpd >= 2) resultLine = string.Format("Gen2 x{0}  (5.0 GT/s)   <-- UNLOCKED", resWid);
        else resultLine = string.Format("Gen1 x{0}  (2.5 GT/s)   <-- LOCKED (target: Gen2)", resWid);

        resultLine = resultLine + (realMaxWidth >= 16 ? "   [声明宽度 x16]" : "");

        // ============ 顶部：醒目的 RESULT 块（修 BUG 3）============
        Console.WriteLine("============================================================");
        Console.WriteLine("  P104-100  PCIe Link Status");
        Console.WriteLine("============================================================");
        Console.WriteLine();
        Console.WriteLine("============================================================");
        Console.WriteLine("   RESULT:  " + resultLine);
        Console.WriteLine("============================================================");
        Console.WriteLine();

        if (realGen != null)
        {
            if (staleMsg != null) Console.WriteLine("  [注意] " + staleMsg);
            if (pending && realGen == null) Console.WriteLine("  状态文件：正在解锁/重试（还没出结果）");
            Console.WriteLine("  当前链路宽度：x" + realWidth + "   （判据 LNKSTA）");
            Console.WriteLine("  声明最大宽度：x" + realMaxWidth + "   （判据 LnkCap MAXWIDTH）"
                + (realMaxWidth >= 16 ? "   <-- 宽度声明 x16 已生效" : ""));
            Console.WriteLine("  数据时间：" + realTime);
            // ★ 恢复期提示：带宽测试/CUDA 初始化会让驱动把链路临时降回 Gen1，看门狗 10-20 秒内自动恢复。
            if (holder && (realSpd < 2 || realMaxWidth < 16))
            {
                Console.WriteLine();
                Console.WriteLine("  [正在自动恢复] 链路刚被外部程序（常见于 CUDA / 带宽测试）临时降速，");
                Console.WriteLine("                 保持进程正在重新提速 + 重新写宽度声明，通常 10-20 秒恢复。");
                Console.WriteLine("                 这不是解锁失效 —— 等 20 秒再跑一次本命令即可看到 x16。");
            }
            Console.WriteLine();
        }
        else if (st != null)
        {
            Console.WriteLine("  最近一次实测（{0}）：", st.Item1);
            Console.WriteLine("     速率 = {0}    宽度 = x{1}    目标速率 = {2}",
                SpeedName(st.Item2), st.Item3, SpeedName(st.Item4));
            Console.WriteLine();
        }

        // ---------- ③ 按住进程 / 计划任务 ----------
        Console.WriteLine("  按住进程 ....... {0}", holder
            ? string.Format("运行中（PID {0}）", procs[0].Id) : "未运行");
        if (taskOk) Console.WriteLine("  开机自启任务 ... 已注册（开机+90 秒 / 登录+120 秒 / 每 5 分钟自愈）");
        else if (taskUnknown) Console.WriteLine("  开机自启任务 ... 无法查询（需要管理员权限才能查询计划任务）");
        else Console.WriteLine("  开机自启任务 ... 未注册");
        Console.WriteLine();

        // ---------- ④ 结论 ----------
        Console.WriteLine("============================================================");
        if (holder && (taskOk || taskUnknown) && resGen != null && resSpd >= 2)
        {
            Console.WriteLine("  结论：【正常】Gen2 已解锁，且正在保持");
            Console.WriteLine("        带宽参考：H2D 约 1.55 GB/s   D2H 约 1.69 GB/s");
            Console.WriteLine("        （Gen1 x4 时只有 0.78 / 0.85 GB/s）");
        }
        else if (holder && (taskOk || taskUnknown) && st != null && st.Item4 == 2)
        {
            Console.WriteLine("  结论：【正常】Gen2 已解锁");
            Console.WriteLine("        日志里那一行显示 Gen1，通常是采样时刚好空闲降速，");
            Console.WriteLine("        空闲降速是省电行为，有负载会自动升回 Gen2。");
            Console.WriteLine("        判据看「目标速率 = 5.0GT/s」即可。");
        }
        else if (holder && (taskOk || taskUnknown) && resGen == null)
        {
            Console.WriteLine("  结论：【看起来正常】但还没有可用的实测记录");
            Console.WriteLine("        按住进程在跑。等 1 分钟后重跑本命令看速率。");
        }
        else if (pending && realGen == null && resGen == null)
        {
            Console.WriteLine("  结论：【正在解锁/重试】保持进程刚启动或正在反复复位，等 1-2 分钟再查。");
            Console.WriteLine("        如果超过 5 分钟还是这样，重跑 install.cmd（管理员）。");
        }
        else if (!holder)
        {
            Console.WriteLine("  结论：【未在保持】按住进程没有运行");
            Console.WriteLine("        → 链路会掉回 Gen1（这是正常的，不是故障）");
            Console.WriteLine("        → 双击 C:\\Program Files\\P104Gen2x4\\P104Gen2x4.exe");
            Console.WriteLine("          或重新运行 install.cmd（需要管理员）");
        }
        else
        {
            Console.WriteLine("  结论：【需要检查】");
            if (!taskOk && !taskUnknown)
            {
                Console.WriteLine("        - 开机自启任务未注册");
                Console.WriteLine("          → 以管理员运行 install.cmd");
            }
            if (resGen == null)
                Console.WriteLine("        - 没有可用的实测记录");
        }
        Console.WriteLine("============================================================");
        Console.WriteLine();

        // ---------- ⑤ 提示如何做寄存器级精确判定 ----------
        // ★ 故意【不】在这里自动读寄存器：那会临时加载驱动、抢 MMIO，
        //   可能干扰正在运行的按住进程。要看寄存器请显式用 -diag。
        Console.WriteLine("  ------------------------------------------------------------");
        Console.WriteLine("  想要寄存器级精确判定？（可选，需要管理员）");
        Console.WriteLine("     右键 status.cmd → 以管理员身份运行");
        Console.WriteLine("     或运行：  bin\\P104Gen2x4.exe -diag");
        Console.WriteLine();
        Console.WriteLine("  不这样做也能判断 —— 上面的结论就是读工具日志得出的，同样可靠。");
        Console.WriteLine("  本命令不需要管理员，也不会干扰正在运行的按住进程。");
        Console.WriteLine("  ------------------------------------------------------------");

        // ============ 底部：再重复一次 RESULT（修 BUG 3）============
        //   终端会停在最后一行，所以结论必须在【最后】再出现一次。
        Console.WriteLine();
        Console.WriteLine("============================================================");
        Console.WriteLine("   RESULT:  " + resultLine);
        Console.WriteLine("============================================================");
        Console.WriteLine("   >>> If you only read one line, read the RESULT above. <<<");
        return 0;
    }

    /// <summary>从状态日志里取最近一条含链路速率的记录</summary>
    static Tuple<string, uint, uint, uint> ReadLastStateFromLog()
    {
        try
        {
            if (!File.Exists(StatusFile)) return null;
            string[] ls = File.ReadAllLines(StatusFile);
            for (int i = ls.Length - 1; i >= 0 && i > ls.Length - 200; i--)
            {
                string ln = ls[i];
                if (ln.IndexOf("LNKSTA=", StringComparison.Ordinal) < 0) continue;
                var m = Regex.Match(ln, @"LNKSTA=0x([0-9A-Fa-f]{8})");
                if (!m.Success) continue;
                uint ls2 = Convert.ToUInt32(m.Groups[1].Value, 16);
                uint spd = ls2 & 0xF, wid = (ls2 >> 4) & 0x3F;

                uint tls = 0;
                var m2 = Regex.Match(ln, @"TLS=(\d)");
                if (m2.Success) tls = uint.Parse(m2.Groups[1].Value);

                string ts = ln.Length >= 19 ? ln.Substring(0, 19) : "?";
                return Tuple.Create(ts, spd, wid, tls);
            }
        }
        catch { }
        return null;
    }

    /// <summary>深度诊断（需要管理员 + 驱动）</summary>
    static int DoDiag()
    {
        // v1.1：不强制 UTF-8，让控制台用自己的代码页（zh-CN 默认 936）—— 中文/子进程输出都不乱码。
        Console.WriteLine("============================================================");
        Console.WriteLine("  P104-100  PCIe Gen2x4  深度诊断");
        Console.WriteLine("============================================================");
        Console.WriteLine();

        if (!IsAdmin())
        {
            Console.WriteLine("  深度诊断需要管理员权限（要加载内核驱动读 GPU 寄存器）。");
            Console.WriteLine();
            Console.WriteLine("  · 只是想看速率 → 直接运行 status.cmd，不需要管理员");
            Console.WriteLine("  · 要跑深度诊断   → 右键本程序 → 以管理员身份运行 -diag");
            return 1;
        }

        bool devOk = EnsureDevices();
        if (!devOk)
        {
            Console.WriteLine("  [X] 内核驱动加载失败");
            Console.WriteLine();
            Console.WriteLine("      常见原因与处理：");
            Console.WriteLine("        1. 易受攻击的驱动程序阻止列表开着");
            Console.WriteLine("           → Windows 安全中心 / 设备安全性 / 内核隔离 / 关掉它 / 重启");
            Console.WriteLine("        2. 反作弊的预启动驱动拦截（错误 31，常见腾讯 ACE-BOOT）");
            Console.WriteLine("           → 先停掉它，或换一台机器");
            Console.WriteLine("        3. 杀毒软件删掉了 bin\\drivers 下的 .sys 文件");
            Console.WriteLine("           → 加白名单后重新解压");
            return 2;
        }
        Console.WriteLine("  [OK] 内核驱动加载成功");
        Console.WriteLine();
        return DoDiagBody();
    }

    /// <summary>已确认有驱动时的寄存器级诊断</summary>
    static int DoDiagBody()
    {
        byte b, d, f;
        if (!FindP104(out b, out d, out f))
        {
            Console.WriteLine("  [X] 总线上找不到 P104-100（10DE:1B87）");
            return 3;
        }
        _bar = PciRead(b, d, f, 0x10) & 0xFFFFFFF0;
        if (!RemapAll())
        {
            Console.WriteLine("  [X] BAR0 寄存器读不到（MMIO 映射失败）");
            return 4;
        }

        uint gcap = PciCap(b, d, f);
        uint lc = PciRead(b, d, f, gcap + 0x0C);
        uint ls = PciRead(b, d, f, gcap + 0x12);
        uint l2 = PciRead(b, d, f, gcap + 0x2C);
        uint c2 = PciRead(b, d, f, gcap + 0x30);
        uint spd = ls & 0xF, wid = (ls >> 4) & 0x3F;
        bool declared = (l2 & 0x6) == 0x6;

        Console.WriteLine("  寄存器实测（{0:X2}:{1:X2}.{2}）", b, d, f);
        Console.WriteLine("     当前速率 ....... {0}   x{1}", SpeedName(spd), wid);
        Console.WriteLine("     目标速率 TLS ... {0}", SpeedName(c2 & 0xF));
        Console.WriteLine("     支持速率声明 ... {0}", declared ? "含 Gen2（已解锁）" : "仅 Gen1（未解锁）");
        Console.WriteLine("     LNKCAP ......... 0x{0:X8}   LNKSTA = 0x{1:X8}", lc, ls);
        Console.WriteLine();
        if (spd >= 2)
            Console.WriteLine("  >>> 【成功】当前链路是 {0} x{1}", SpeedName(spd), wid);
        else if (declared && (c2 & 0xF) == 2)
            Console.WriteLine("  >>> 【已解锁】目标速率已是 Gen2；当前显示 Gen1 多为空闲省电降速");
        else
            Console.WriteLine("  >>> 【未解锁】请以管理员运行 bin\\P104Gen2x4.exe -once 重试");
        CloseAll();
        return 0;
    }


    // ================= ★ VBIOS 读取与校验（2026-10-15 新增）=================
    //  发现：本卡的 VBIOS ROM 完整映射在【物理地址 BAR0 + 0x300000】（256KB），只读。
    //        另有一个完全相同的第二镜像在 BAR0 + 0x380000。
    //  ⇒ 可以【不依赖 nvflash】独立读取卡上的 VBIOS，用于：
    //      · 刷写前：确认卡上内容与备份一致
    //      · 刷写后：立刻校验是否写成功（不必等重启才知道变砖）
    //      · 任何时候：检查 EEPROM 有没有意外变化
    static readonly System.Collections.Generic.Dictionary<uint, ulong> _romPages
        = new System.Collections.Generic.Dictionary<uint, ulong>();

    static uint RomRd(uint romOff)
    {
        uint phys = _bar + 0x300000u + (romOff & ~0xFFFu);
        ulong va;
        if (!_romPages.TryGetValue(phys, out va))
        {
            va = MapPage(phys);
            _romPages[phys] = va;
        }
        if (va == 0) return 0xFFFFFFFF;
        return Mrd(va, romOff & 0xFFF);
    }

    static int DoVbios(string[] args)
    {
        // v1.1：不强制 UTF-8，让控制台用自己的代码页（zh-CN 默认 936）—— 中文/子进程输出都不乱码。
        const int ROM_LEN = 0x40000;       // 256 KB
        string outFile = null;
        for (int i = 0; i < args.Length; i++)
        {
            string s = args[i].ToLower().TrimStart('-', '/');
            if ((s == "save" || s == "dump" || s == "o" || s == "out") && i + 1 < args.Length)
                outFile = args[i + 1];
            else if (s.StartsWith("save=") || s.StartsWith("dump=") || s.StartsWith("out="))
                outFile = args[i].Substring(args[i].IndexOf('=') + 1);
        }

        Console.WriteLine("=== P104 VBIOS 读取与校验 ===");
        if (!EnsureDevices()) { Console.WriteLine("驱动装载失败（需要管理员权限）"); return 3; }
        if (!FindP104(out _bus, out _dev, out _fun)) { Console.WriteLine("找不到 P104-100 (10DE:1B87)"); return 4; }
        _bar = PciRead(_bus, _dev, _fun, 0x10) & 0xFFFFFFF0;
        Console.WriteLine("P104 " + _bus.ToString("X2") + ":" + _dev.ToString("X2") + "." + _fun
                          + "  BAR0=0x" + _bar.ToString("X8"));

        // ★ 指纹校验：ROM 头应以 0xAA55 开头
        uint w0 = RomRd(0), w1 = RomRd(4), w2 = RomRd(8);
        Console.WriteLine("ROM 头: 0x" + w0.ToString("X8") + " 0x" + w1.ToString("X8")
                          + " 0x" + w2.ToString("X8"));
        if ((w0 & 0xFFFF) != 0xAA55)
        {
            Console.WriteLine("!! 未找到 0xAA55 签名 —— 该地址不是 VBIOS 映射");
            CloseAll(); return 7;
        }

        // 读整个 ROM
        byte[] rom = new byte[ROM_LEN];
        for (int i = 0; i < ROM_LEN; i += 4)
        {
            uint v = RomRd((uint)i);
            rom[i + 0] = (byte)(v & 0xFF);
            rom[i + 1] = (byte)((v >> 8) & 0xFF);
            rom[i + 2] = (byte)((v >> 16) & 0xFF);
            rom[i + 3] = (byte)((v >> 24) & 0xFF);
        }

        // 哈希
        string md5, sha;
        using (var m = System.Security.Cryptography.MD5.Create())
            md5 = BitConverter.ToString(m.ComputeHash(rom)).Replace("-", "").ToLowerInvariant();
        using (var s2 = System.Security.Cryptography.SHA256.Create())
            sha = BitConverter.ToString(s2.ComputeHash(rom)).Replace("-", "").ToLowerInvariant();

        // ASCII 摘要（ROM 头里有板号与日期）
        var sb = new StringBuilder();
        for (int i = 8; i < 64; i++)
            sb.Append(rom[i] >= 32 && rom[i] < 127 ? (char)rom[i] : '.');
        Console.WriteLine();
        Console.WriteLine("  ROM 摘要 : " + sb.ToString());
        Console.WriteLine("  ROM 大小 : " + ROM_LEN + " 字节 (256 KB)");
        Console.WriteLine("  ★ MD5    : " + md5);
        Console.WriteLine("  ★ SHA256 : " + sha);

        if (!string.IsNullOrEmpty(outFile))
        {
            try
            {
                File.WriteAllBytes(outFile, rom);
                Console.WriteLine("  已保存到 : " + Path.GetFullPath(outFile));
            }
            catch (Exception e) { Console.WriteLine("  保存失败: " + e.Message); }
        }

        Console.WriteLine();
        Console.WriteLine("  【怎么用】");
        Console.WriteLine("    刷写前：算一下你的备份 ROM 的 MD5，跟上面的比。一致就说明卡上是原厂内容。");
        Console.WriteLine("            命令：certutil -hashfile <备份.rom> MD5");
        Console.WriteLine("    刷写后：再跑一次本命令。如果 MD5 变了，说明写进去了；");
        Console.WriteLine("            如果还是旧的，说明刷新没生效（此时不要重启，先排查）。");
        Console.WriteLine();
        Console.WriteLine("  【注意】本命令只读，不改任何东西，也不会重启机器。");
        CloseAll();
        return 0;
    }


    // ================= ★ CUDA 快速带宽实测（无需 Python） =================
    [DllImport("nvcuda.dll")] static extern int cuInit(uint flags);
    [DllImport("nvcuda.dll")] static extern int cuDeviceGetCount(out int count);
    [DllImport("nvcuda.dll")] static extern int cuDeviceGet(out int dev, int ordinal);
    [DllImport("nvcuda.dll")] static extern int cuDeviceGetName(byte[] name, int len, int dev);
    [DllImport("nvcuda.dll", EntryPoint = "cuCtxCreate_v2")] static extern int cuCtxCreate_v2(out IntPtr ctx, uint flags, int dev);
    [DllImport("nvcuda.dll", EntryPoint = "cuCtxDestroy_v2")] static extern int cuCtxDestroy_v2(IntPtr ctx);
    [DllImport("nvcuda.dll", EntryPoint = "cuCtxSynchronize")] static extern int cuCtxSynchronize();
    [DllImport("nvcuda.dll", EntryPoint = "cuMemAlloc_v2")] static extern int cuMemAlloc_v2(out ulong dptr, UIntPtr size);
    [DllImport("nvcuda.dll", EntryPoint = "cuMemFree_v2")] static extern int cuMemFree_v2(ulong dptr);
    [DllImport("nvcuda.dll", EntryPoint = "cuMemAllocHost_v2")] static extern int cuMemAllocHost_v2(out IntPtr host, UIntPtr size);
    [DllImport("nvcuda.dll", EntryPoint = "cuMemFreeHost")] static extern int cuMemFreeHost_v2(IntPtr host);
    [DllImport("nvcuda.dll", EntryPoint = "cuMemcpyHtoD_v2")] static extern int cuMemcpyHtoD_v2(ulong dptr, IntPtr src, UIntPtr size);
    [DllImport("nvcuda.dll", EntryPoint = "cuMemcpyDtoH_v2")] static extern int cuMemcpyDtoH_v2(IntPtr dst, ulong src, UIntPtr size);

    static uint Frd(uint off)
    {
        ulong va = ((off >> 12) == 0x20) ? _vaFuse0 : ((off >> 12) == 0x21) ? _vaFuse1 : _vaFuse2;
        return Mrd(va, off & 0xFFFu);
    }

    static int DoFuse()
    {
        // v1.1：不强制 UTF-8，让控制台用自己的代码页（zh-CN 默认 936）—— 中文/子进程输出都不乱码。
        Console.WriteLine("============================================================");
        Console.WriteLine("  NVIDIA fuse / strap dump (read-only, for model comparison)");
        Console.WriteLine("============================================================");
        if (!EnsureDevices()) { Console.WriteLine("  [失败] 驱动装载失败（需要管理员）"); return 1; }
        if (!FindP104(out _bus, out _dev, out _fun))
        {
            bool found = false;
            for (int b = 1; b <= 0x20 && !found; b++)
                for (int d = 0; d < 0x20 && !found; d++)
                    for (int f = 0; f < 8; f++)
                    {
                        uint id = PciRead((byte)b, (byte)d, (byte)f, 0x00);
                        if (id == 0xFFFFFFFF) { if (f == 0) break; continue; }
                        if ((id & 0xFFFF) == 0x10DE) { _bus = (byte)b; _dev = (byte)d; _fun = (byte)f; found = true; break; }
                    }
            if (!found) { Console.WriteLine("  [失败] 没有找到 NVIDIA GPU"); return 2; }
        }
        _bar = PciRead(_bus, _dev, _fun, 0x10) & 0xFFFFFFF0;
        if (!RemapAll()) { Console.WriteLine("  [失败] BAR0 映射失败"); return 3; }
        uint id0 = PciRead(_bus, _dev, _fun, 0x00);
        uint boot0 = Mrd(_vaLink, 0); // placeholder, will read via _vaFuse? use config
        Console.WriteLine(string.Format("  GPU {0:X2}:{1:X2}.{2}  ID=0x{3:X8}  BAR0=0x{4:X8}", _bus, _dev, _fun, id0, _bar));
        Console.WriteLine();
        var lines = new List<string>();
        Action<uint, string> dump = (off, name) =>
        {
            uint v = Frd(off);
            string s = string.Format("  0x{0:X6}  {1,-26} = 0x{2:X8}", off, name, v);
            Console.WriteLine(s);
            lines.Add(s);
        };
        dump(0x020040, "FUSE_EN_SW_OVERRIDE(M-b)");
        dump(0x021140, "FUSE_EN_SW_OVERRIDE(M-a)");
        dump(0x021580, "GEN3_DIS? (M1)");
        dump(0x021680, "GEN3_DIS? (M3 alt)");
        dump(0x021378, "NVDEC_DISABLE?");
        dump(0x021414, "NVENC_DISABLE?");
        dump(0x0212D8, "OFA_DISABLE?");
        dump(0x021288, "DevInit cond#29 reg");
        dump(0x021290, "DevInit cond#28 reg");
        dump(0x021C00, "DevInit disable group");
        dump(0x021C1C, "DevInit cond#20 reg");
        dump(0x021EB0, "DevInit cond#44 reg");
        dump(0x02240C, "DevInit cond#14 reg");
        dump(0x022500, "HW_MISC_DISABLE");
        dump(0x022580, "SW_MISC_DISABLE");
        Console.WriteLine();
        Console.WriteLine("  请把以上内容整段复制反馈（用于对比 P104 / P106 / 1070 / 1080 的熔丝差异）。");
        try
        {
            Directory.CreateDirectory(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "P104Gen2x4"));
            File.WriteAllLines(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "P104Gen2x4", "fuse_dump.txt"), lines, Encoding.UTF8);
            Console.WriteLine("  (已同时保存到 C:\\ProgramData\\P104Gen2x4\\fuse_dump.txt)");
        }
        catch { }
        return 0;
    }
    static bool CudaReady()
    {
        try
        {
            if (cuInit(0) != 0) return false;
            int n;
            if (cuDeviceGetCount(out n) != 0 || n <= 0) return false;
            for (int i = 0; i < n; i++)
            {
                int dev;
                if (cuDeviceGet(out dev, i) != 0) continue;
                var b = new byte[128];
                if (cuDeviceGetName(b, 128, dev) != 0) continue;
                int z = Array.IndexOf(b, (byte)0);
                if (z < 0) z = b.Length;
                if (Encoding.ASCII.GetString(b, 0, z).ToUpper().Contains("P104")) return true;
            }
        }
        catch { }
        return false;
    }
    static int DoBw()
    {
        // v1.1：不强制 UTF-8，让控制台用自己的代码页（zh-CN 默认 936）—— 中文/子进程输出都不乱码。
        Console.WriteLine("============================================================");
        Console.WriteLine("  P104-100  PCIe 真实带宽实测（CUDA，无需 Python）");
        Console.WriteLine("============================================================");
        Console.WriteLine();
        try
        {
            if (cuInit(0) != 0) { Console.WriteLine("  [跳过] 没有 CUDA 驱动（nvcuda.dll 不可用）"); return 1; }
            int count;
            if (cuDeviceGetCount(out count) != 0 || count <= 0) { Console.WriteLine("  [跳过] 没有 CUDA 设备"); return 1; }
            int target = -1; string targetName = "";
            for (int i = 0; i < count; i++)
            {
                int dev;
                if (cuDeviceGet(out dev, i) != 0) continue;
                var buf = new byte[128];
                string nm = "";
                if (cuDeviceGetName(buf, 128, dev) == 0)
                {
                    int z = Array.IndexOf(buf, (byte)0);
                    if (z < 0) z = buf.Length;
                    nm = Encoding.ASCII.GetString(buf, 0, z);
                }
                if (nm.ToUpper().Contains("P104")) { target = dev; targetName = nm; break; }
            }
            if (target < 0) { Console.WriteLine("  [跳过] 没有找到 P104（安全约定：绝不测别的卡）"); return 1; }
            Console.WriteLine("  目标设备 : " + targetName);
            IntPtr ctx;
            if (cuCtxCreate_v2(out ctx, 0, target) != 0) { Console.WriteLine("  [失败] 创建 CUDA 上下文失败（可能被别的进程占用）"); return 1; }
            try
            {
                // ★ v1.1：CUDA 上下文初始化会让 NVIDIA 驱动把 0x8C040.SPEED 降回 2.5GT，
                //   正在运行的按住进程会在约 2 秒内把链路恢复 Gen2。先等它，否则测到的是 Gen1 带宽
                //   （旧版测出 0.79 而实际链路是 Gen2，就是这个原因）。
                int holderCount = 0;
                foreach (var pr in Process.GetProcessesByName("P104Gen2x4"))
                    if (pr.Id != Process.GetCurrentProcess().Id) holderCount++;
                if (holderCount > 0)
                {
                    Console.WriteLine("  保持进程在运行 → 等待链路恢复 Gen2（约 3 秒）…");
                    Thread.Sleep(3000);
                }
                const int MB = 64;
                ulong size = (ulong)MB * 1024 * 1024;
                ulong dptr;
                if (cuMemAlloc_v2(out dptr, (UIntPtr)size) != 0) { Console.WriteLine("  [失败] 显存分配失败"); return 1; }
                IntPtr host;
                if (cuMemAllocHost_v2(out host, (UIntPtr)size) != 0) { cuMemFree_v2(dptr); Console.WriteLine("  [失败] 页锁定内存分配失败"); return 1; }
                try
                {
                    // ★ v1.1：如果按住进程在跑，CUDA 上下文会让链路短暂掉到 Gen1；
                    //   最多测 4 次（每次间隔 3 秒），取最好的一次 —— 避免把"正在恢复"当成"没解锁"。
                    double h2d = 0, d2h = 0;
                    int attempts = holderCount > 0 ? 4 : 1;
                    for (int a = 1; a <= attempts; a++)
                    {
                        cuMemcpyHtoD_v2(dptr, host, (UIntPtr)size);
                        cuCtxSynchronize();
                        var sw = Stopwatch.StartNew();
                        const int reps = 8;
                        for (int r = 0; r < reps; r++) cuMemcpyHtoD_v2(dptr, host, (UIntPtr)size);
                        cuCtxSynchronize();
                        sw.Stop();
                        double h = size / 1e9 / (sw.Elapsed.TotalSeconds / reps);
                        sw = Stopwatch.StartNew();
                        for (int r = 0; r < reps; r++) cuMemcpyDtoH_v2(host, dptr, (UIntPtr)size);
                        cuCtxSynchronize();
                        sw.Stop();
                        double d = size / 1e9 / (sw.Elapsed.TotalSeconds / reps);
                        if (h > h2d) h2d = h;
                        if (d > d2h) d2h = d;
                        if (h2d >= 1.2 || a == attempts) break;
                        Console.WriteLine("  第 " + a + " 次实测 " + h.ToString("F3") + " GB/s —— 链路可能还在恢复，3 秒后再测…");
                        Thread.Sleep(3000);
                    }
                    Console.WriteLine("  单向带宽 : H2D(主机->显存) " + h2d.ToString("F3") + " GB/s    D2H(显存->主机) " + d2h.ToString("F3") + " GB/s");
                    Console.WriteLine();
                    Console.WriteLine("  参考: Gen1 x4 = 0.78 / 0.85   Gen2 x4 = 1.55 / 1.69   Gen3 x4 = 3.10");
                    Console.WriteLine();
                    if (h2d >= 1.2) Console.WriteLine("  RESULT: Gen2 x4 带宽达标（" + h2d.ToString("F2") + " GB/s）");
                    else if (h2d >= 0.4) Console.WriteLine("  RESULT: 只有 Gen1 x4 带宽（" + h2d.ToString("F2") + " GB/s）—— 链路没上 Gen2");
                    else Console.WriteLine("  RESULT: 带宽异常低（" + h2d.ToString("F3") + " GB/s）");

                    // ★ v1.1：CUDA 上下文会让驱动把链路临时降回 Gen1（声明也会临时回 x4）。
                    //   测试结束后等保持进程把链路恢复，再退出 —— 否则用户紧接着跑 状态.cmd 会看到"x4"而以为坏了。
                    if (holderCount > 0)
                    {
                        Console.WriteLine();
                        Console.WriteLine("  测试完成 —— 等待保持进程恢复链路（Gen2 + 宽度声明 x16，最多 30 秒）…");
                        bool restored = WaitHolderRestored(30000);
                        Console.WriteLine(restored
                            ? "  链路已恢复：Gen2 x4 + 声明宽度 x16。现在跑 状态.cmd 会显示正常。"
                            : "  注意：30 秒内还没恢复到 Gen2；保持进程会继续自动重试（这是正常现象，不是解锁失效）。");
                    }
                    return 0;
                }
                finally { cuMemFreeHost_v2(host); cuMemFree_v2(dptr); }
            }
            finally { cuCtxDestroy_v2(ctx); }
        }
        catch (Exception e) { Console.WriteLine("  [失败] " + e.Message); return 1; }
    }

    /// <summary>打印用法。★ v1.1 修复：未知参数/ -help 绝不能落入默认 bringup（旧版会直接复位显卡）。</summary>
    static void Usage()
    {
        string[] lines = {
            "P104Gen2x4 v1.1 —— P104-100 PCIe Gen2 x4 解锁 + 宽度声明 x16",
            "",
            "用法:",
            "  P104Gen2x4.exe                解锁并常驻保持（默认，需要管理员）",
            "  P104Gen2x4.exe -once          解锁一次，打印 PASS/FAIL，不常驻",
            "  P104Gen2x4.exe -dry-run       只读：快照 + 计划，不写任何寄存器",
            "  P104Gen2x4.exe -check         只读：从状态文件/日志给结论（不需要管理员）",
            "  P104Gen2x4.exe -status        同 -check",
            "  P104Gen2x4.exe -diag          只读：寄存器级诊断（需要管理员）",
            "  P104Gen2x4.exe -bw            CUDA 带宽实测（只读）",
            "  P104Gen2x4.exe -off           停止并还原寄存器（需要管理员）",
            "  P104Gen2x4.exe -install       安装：部署 + 计划任务（需要管理员）",
            "  P104Gen2x4.exe -uninstall     卸载（保留驱动策略键；加 -purgepolicy 彻底清）",
            "  P104Gen2x4.exe -vbios [-save 路径]  VBIOS 读取校验（只读）",
            "",
            "选项:",
            "  -quiet        只写日志文件，不打印",
            "  -noreset      只做 live 快路径，不做整卡复位",
            "  -nowidth      不做宽度声明 x16",
            "  -retry N      本次允许的整卡复位次数（1..2，默认 1）",
            "  -help         显示本帮助",
        };
        foreach (string l in lines) Say(l);
    }

    // ================= main =================
    static int Main(string[] args)
    {
        string mode = "bringup";
        bool dryRun = false;
        bool unknown = false, help = false, skipNext = false;
        for (int ai = 0; ai < args.Length; ai++)
        {
            string a = args[ai];
            string s = a.ToLower().TrimStart('-', '/');
            if (skipNext) { skipNext = false; continue; }
            switch (s)
            {
                case "bringup": case "unlock": mode = "bringup"; break;
                case "status": case "check": mode = "check"; break;
                case "once": mode = "once"; break;
                case "off": mode = "off"; break;
                case "install": mode = "install"; break;
                case "uninstall": mode = "uninstall"; break;
                case "diag": case "diagnose": mode = "diag"; break;
                case "bw": case "bandwidth": mode = "bw"; break;
                case "vbios": case "rom": mode = "vbios"; break;
                case "dry-run": case "dryrun": dryRun = true; break;
                case "quiet": Quiet = true; break;
                case "noreset": AllowReset = false; break;
                case "nowidth": WidthEnabled = false; break;
                case "retry":
                    if (ai + 1 < args.Length)
                    {
                        int n;
                        if (int.TryParse(args[ai + 1], out n) && n >= 1) ResetRetries = Math.Min(n, 2);
                        skipNext = true;
                    }
                    break;
                case "help": case "h": case "?": help = true; break;
                case "purgepolicy": break;   // 仅 -uninstall 使用
                default:
                    if (s.StartsWith("retry="))
                    {
                        int n;
                        if (int.TryParse(a.Substring(a.IndexOf('=') + 1), out n) && n >= 1) ResetRetries = Math.Min(n, 2);
                    }
                    else unknown = true;
                    break;
            }
        }
        // ★ 未知参数 / -help 一律只打印用法并退出 —— 绝不落入默认 bringup（旧版会直接复位显卡）。
        if (help || (unknown && args.Length > 0)) { Usage(); return unknown ? 2 : 0; }

        if (mode == "check") return DoCheck();
        if (mode == "diag") return DoDiag();
        if (mode == "bw") return DoBw();
        if (mode == "vbios") return DoVbios(args);
        if (mode == "install") return DoInstall();
        if (mode == "uninstall") return DoUninstall();

        Log("=== P104Gen2x4 v1.1 启动，模式=" + mode + (Quiet ? "（quiet）" : "") + " ===");

        // ★ 全局单实例锁：同一时刻只允许一个非只读实例映射 BAR0。
        bool created;
        Mutex mtx = new Mutex(true, @"Global\P104Gen2x4Session", out created);
        if (!created)
        {
            Log("已有实例在运行（可能是按住进程），本次退出以避免 MMIO 冲突");
            return 0;
        }

        // 清理上次异常退出可能留下的驱动服务（进程被杀时不会走 CloseAll）
        foreach (string svc in new[] { SVC_WR, SVC_TS })
        {
            string q = Run("sc.exe", "query " + svc);
            if (q.IndexOf("RUNNING", StringComparison.OrdinalIgnoreCase) >= 0 ||
                q.IndexOf("STOPPED", StringComparison.OrdinalIgnoreCase) >= 0)
                Run("sc.exe", "delete " + svc);
        }

        BudgetLoad();

        // S0 就绪门控：设备 + NVIDIA 驱动（子进程；★ 不做 cuInit）
        if (!WaitReady(T_READY_MS))
        {
            Log("找不到 P104-100 (10DE:1B87) 或驱动未就绪 —— 本次退出（若设备已消失请 AC 断电）");
            return 4;
        }
        if (!SelfTestWrite(_bus, _dev, _fun))
        {
            Log("!! 配置空间写通道自检失败 —— 拒绝继续（避免得到错误的\"只读\"结论）");
            return 5;
        }
        if (!RefreshRefs()) { Log("MMIO 映射失败"); return 6; }
        Log(string.Format("P104 {0:X2}:{1:X2}.{2}  BAR0=0x{3:X8}  上游 {4}",
            _bus, _dev, _fun, _bar,
            _rcap != 0 ? string.Format("{0:X2}:{1:X2}.{2}", _rb, _rd, _rf) : "(未找到)"));
        Log("初始: " + StateLine(_rb, _rd, _rf, _rcap));
        SaveSnapshot();

        if (mode == "off")
        {
            RestoreAll();
            CloseAll(); UnloadServices();
            return 0;
        }

        if (dryRun)
        {
            Log("[dry-run] 只读快照完成，未写任何寄存器。");
            Log("[dry-run] 计划: live 写组 → (失败才) 1 次整卡复位 → TLS/retrain → 宽度声明 x16 → 150ms 保持");
            Log("[dry-run] 当前判据: J1=" + J1() + " J2=" + J2() + " J3=" + J3()
                + "  本开机周期已复位 " + _resetsThisBoot + "/" + MAX_RESETS_PER_BOOT + " 次");
            CloseAll();
            return 0;
        }

        WriteDriverPolicy();
        bool ok = UnlockOnce();

        if (mode == "once")
        {
            Log(ok ? "解锁完成（未常驻）" : "解锁未达成");
            CloseAll(); UnloadServices();
            return ok ? 0 : 7;
        }

        Log(ok ? "进入常驻按住循环（150ms 看门狗，仅偏离时补写）" : "未达成，仍进入保持循环持续尝试（不再复位）");
        return HoldLoop();
    }

    /// <summary>常驻保持循环：150ms 周期，只在判据偏离时补写；触发类写 ≥2 秒限流。</summary>
    static int HoldLoop()
    {
        int lostStreak = 0, healFail = 0, lowSpeedStreak = 0, liveRetry = 0;
        long lastStatus = 0, gen2Since = NowTick();
        while (true)
        {
            try
            {
                // 1) 活性（MMIO 连续 0xFFFFFFFF = 映射失效）
                uint chk = Mrd(_vaLink, OFF_LINKCFG0);
                if (chk == 0xFFFFFFFF)
                {
                    if (++lostStreak >= 3)
                    {
                        lostStreak = 0;
                        Log("检测到 MMIO 映射失效，重新装载驱动并重映射…（第 " + (healFail + 1) + " 次）");
                        CloseAll();
                        Thread.Sleep(1500);
                        if (RefreshRefs()) { Log("映射已恢复"); healFail = 0; }
                        else
                        {
                            healFail++;
                            Log("自愈失败 " + healFail + "/5");
                            if (healFail >= 5)
                            {
                                Log("!! 连续 5 次自愈失败 —— 主动退出，交由计划任务在下次触发时重启");
                                CloseAll(); UnloadServices(); return 8;
                            }
                        }
                    }
                    Thread.Sleep(200);
                    continue;
                }
                lostStreak = 0;
                _samples++;

                uint l2 = Lcap2(), lc = LnkCap(), ls = LnkSta();
                bool j1 = (l2 & 0x6) == 0x6, gen2 = (ls & 0xF) >= 2;

                // 2) 声明保持：偏离才补写，≥2s 限流
                if (!j1 && NowTick() - _lastFixTick >= 2000)
                {
                    Log("检测到 Gen2 声明丢失（LCAP2=0x" + l2.ToString("X8") + "）→ live 补写");
                    ApplyLiveGroup("看门狗补声明");
                    _lastFixTick = NowTick();
                }
                else
                {
                    uint cfg = Mrd(_vaLink, OFF_LINKCFG0);
                    if ((cfg & 0xC0000u) != 0x40000u && NowTick() - _lastFixTick >= 2000)
                    {
                        WriteSpeedGen2();
                        _lastFixTick = NowTick();
                    }
                }

                // 3) 速率保持：偏离才 retrain，≥2s 限流（armed 状态先整字回退，否则升不了速）
                if (!gen2)
                {
                    if (gen2Since >= 0) { gen2Since = -1; _lastTriggerTick = -100000; }  // 刚掉速：允许立刻重训一次
                    DisarmWidthForSpeed();
                    if (NowTick() - _lastTriggerTick >= T_TRIGGER_MIN_GAP_MS)
                    {
                        WriteTlsBoth(_rb, _rd, _rf, _rcap);
                        RetrainBoth(_rb, _rd, _rf, _rcap);
                        _lastTriggerTick = NowTick();
                        _rewrites++;
                    }
                    if (++lowSpeedStreak >= 40)
                    {
                        lowSpeedStreak = 0;
                        liveRetry++;
                        Log("连续 " + 40 + " 轮（约 6 秒）未保持 Gen2 → 第 " + liveRetry + " 次补 live 写组");
                        bool w = ApplyLiveGroup("看门狗补速率");
                        if (!w || liveRetry >= 5)
                        {
                            Log("!! 长时间无法保持 Gen2 —— 主动退出，交由计划任务在下次触发时重启");
                            CloseAll(); UnloadServices(); return 9;
                        }
                    }
                }
                else
                {
                    if (gen2Since < 0) { gen2Since = NowTick(); Log("链路已恢复到 Gen2（等待 5 秒稳定后再做宽度声明）"); }
                    lowSpeedStreak = 0;
                }

                // 4) 宽度声明保持：先确认 Gen2 已连续稳定 ≥3 秒、且距上次尝试 ≥3 秒（避免 ping-pong）
                if (WidthEnabled && gen2 && gen2Since > 0 && NowTick() - gen2Since >= 3000 &&
                    ((lc >> 4) & 0x3F) != 16 && NowTick() - _lastWidthFixTick >= T_WIDTH_RETRY_MS)
                {
                    WidthArm();
                    _lastWidthFixTick = NowTick();
                }

                // 5) 状态文件（每约 1 秒）+ 心跳日志
                if (NowTick() - lastStatus >= T_STATUS_PERIOD_MS)
                {
                    lastStatus = NowTick();
                    uint gcap = PciCap(_bus, _dev, _fun);
                    WriteStatusFile(_rb, _rd, _rf, _rcap, gcap, LnkSta(), (uint)_samples);
                }
                if (_samples % 1500 == 0)
                    Log(string.Format("按住中: {0} | 采样 {1} 次 / 补训 {2} 次",
                        StateLine(_rb, _rd, _rf, _rcap), _samples, _rewrites));
            }
            catch { }
            Thread.Sleep(T_HOLD_PERIOD_MS);
        }
    }

    static void CloseAll()
    {
        if (hWr != INVALID) CloseHandle(hWr);
        if (hTs != INVALID) CloseHandle(hTs);
        hWr = hTs = INVALID;
    }

    /// <summary>
    /// 复制文件；如果目标正在被使用但已存在且大小相同，视为"同版本已就位"跳过。
    /// （v1.1：发版场景下驱动 .sys 常被加载中的服务占用，旧版会因此让 install 中断。）
    /// </summary>
    static bool SafeCopy(string src, string dst, string label)
    {
        try
        {
            File.Copy(src, dst, true);
            return true;
        }
        catch (Exception e)
        {
            try
            {
                if (File.Exists(dst) && new FileInfo(dst).Length == new FileInfo(src).Length)
                {
                    Log(label + " 正在使用中，跳过复制（目标已存在同大小文件）");
                    return true;
                }
            }
            catch { }
            Log("复制 " + label + " 失败: " + e.Message);
            return false;
        }
    }

    // ================= 安装 / 卸载 =================
    static readonly string TaskName = "P104 PCIe Gen2x4 Bring-up";

    /// <summary>把计划任务写成 XML 注册 —— 支持"开机 + 登录 + 每 5 分钟自愈"的触发组合</summary>
    static bool RegisterTask(string exe)
    {
        // ★ 为什么要 XML：schtasks 的命令行参数拼不出"多个触发器 + 重复间隔"，
        //   而自愈恰恰依赖这个：按住进程一旦被杀，最多 5 分钟内自动拉起。
        // ★ v1.1 触发器：开机 +90s / 登录 +120s / 每 5 分钟自愈。
        //   v1.0 是 +60s / +15s / 每 1 分钟 + -retry 3 —— 失败时会"每分钟连做 3 次整卡复位"，
        //   这就是历史上"反复黑屏/掉卡"的直接来源；现在每 5 分钟一次、每次最多 1 次复位。
        string xml = string.Format(
@"<?xml version=""1.0"" encoding=""UTF-16""?>
<Task version=""1.2"" xmlns=""http://schemas.microsoft.com/windows/2004/02/mit/task"">
  <RegistrationInfo>
    <Description>P104-100 PCIe Gen2 x4 解锁（v1.1：live 快路径 + 兜底复位 + 150ms 保持）</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT90S</Delay>
    </BootTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT120S</Delay>
    </LogonTrigger>
    <TimeTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Repetition>
        <Interval>PT5M</Interval>
        <Duration>P3650D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id=""Author"">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>false</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context=""Author"">
    <Exec>
      <Command>{0}</Command>
      <Arguments>-quiet -retry 1</Arguments>
    </Exec>
  </Actions>
</Task>", exe);

        string tmp = Path.Combine(Path.GetTempPath(), "P104Gen2x4-task.xml");
        try
        {
            // schtasks /XML 需要 UTF-16
            File.WriteAllText(tmp, xml, new UnicodeEncoding(false, true));
        }
        catch (Exception e) { Log("写任务 XML 失败: " + e.Message); return false; }

        Run("schtasks.exe", "/Delete /TN \"" + TaskName + "\" /F");
        string r = Run("schtasks.exe", "/Create /TN \"" + TaskName + "\" /XML \"" + tmp + "\" /F");
        try { File.Delete(tmp); } catch { }
        if (r.Length > 0) Log("schtasks: " + r);

        // ★ 独立校验：能查询到才算注册成功（schtasks 输出是 OEM 编码，字符串匹配不可靠）
        string q = Run("schtasks.exe", "/query /tn \"" + TaskName + "\" /fo LIST");
        bool ok = q.IndexOf(TaskName, StringComparison.OrdinalIgnoreCase) >= 0;
        Log("计划任务校验: " + (ok ? "存在 ✓" : "**未找到**"));
        return ok;
    }

    static int DoInstall()
    {
        // v1.1：不强制 UTF-8，让控制台用自己的代码页（zh-CN 默认 936）—— 中文/子进程输出都不乱码。
        Log("=== 安装 ===");

        string self = Process.GetCurrentProcess().MainModule.FileName;
        string target = @"C:\Program Files\P104Gen2x4";
        string targetExe = Path.Combine(target, "P104Gen2x4.exe");

        // ★ 先停掉可能正在运行的按住进程（按 PID 跳过自己，绝不 taskkill /IM 自杀）。
        //   v1.1 修正：即使当前进程就是已安装的 exe，也要结束【其他】同名进程；
        //   旧版这里直接跳过，导致"已装好后重跑 install"时驱动文件被占用 → 安装中断。
        bool selfIsTarget = string.Equals(self, targetExe, StringComparison.OrdinalIgnoreCase);
        Run("schtasks.exe", "/end /TN \"" + TaskName + "\"");
        int myPid = Process.GetCurrentProcess().Id;
        int killed = 0;
        try
        {
            foreach (var pr in Process.GetProcessesByName("P104Gen2x4"))
            {
                if (pr.Id == myPid) continue;   // 绝不杀自己
                try { pr.Kill(); killed++; } catch { }
            }
        }
        catch { }
        Log("已结束 " + killed + " 个旧的按住进程（跳过本进程 PID " + myPid + "）");
        System.Threading.Thread.Sleep(2000);

        WriteDriverPolicy();
        try
        {
            Directory.CreateDirectory(target);
            Directory.CreateDirectory(Path.Combine(target, "drivers"));
            if (!selfIsTarget)
            {
                if (!SafeCopy(self, targetExe, "exe")) { Log("部署 exe 失败"); return 1; }
                Log("已部署 exe 到 " + target);
            }
            else
            {
                Log("exe 已在目标位置，无需复制");
            }

            // ★ 驱动文件查找：优先 ExeDir\drivers，其次 <包>\bin\drivers，再次当前目录
            string drvSrc = null;
            foreach (string cand in new[]
            {
                Path.Combine(ExeDir, "drivers"),
                Path.Combine(ExeDir, "..", "bin", "drivers"),
                Path.Combine(Environment.CurrentDirectory, "bin", "drivers"),
                Path.Combine(Environment.CurrentDirectory, "drivers"),
                Path.Combine(target, "drivers"),
            })
            {
                if (File.Exists(Path.Combine(cand, "WinRing0x64.sys")) &&
                    File.Exists(Path.Combine(cand, "ThrottleStop.sys")))
                {
                    drvSrc = Path.GetFullPath(cand);
                    break;
                }
            }
            if (drvSrc == null) { Log("找不到驱动文件（WinRing0x64.sys / ThrottleStop.sys）"); return 1; }
            bool drvOk = true;
            foreach (string f in new[] { "WinRing0x64.sys", "ThrottleStop.sys" })
                drvOk &= SafeCopy(Path.Combine(drvSrc, f), Path.Combine(target, "drivers", f), f);
            if (!drvOk) { Log("部署驱动失败"); return 1; }
            Log("已部署驱动（来源 " + drvSrc + "）");
        }
        catch (Exception e) { Log("部署失败: " + e.Message); return 1; }

        bool taskOk = RegisterTask(targetExe);
        Log("开机自启: " + (taskOk ? "已注册（开机+90s / 登录+120s / 每 5 分钟自愈）" : "注册失败，请手动运行工具"));
        Log("安装完成。重启后会自动解锁；也可立刻运行: \"" + targetExe + "\"");
        return 0;
    }

    static int DoUninstall()
    {
        // v1.1：不强制 UTF-8，让控制台用自己的代码页（zh-CN 默认 936）—— 中文/子进程输出都不乱码。
        Log("=== 卸载 ===");
        Log(Run("schtasks.exe", "/Delete /TN \"" + TaskName + "\" /F"));
        UnloadServices();
        // ★ v1.1 改动：不删除驱动策略键（RMPcieLinkSpeed / RmForceEnableGen2 / RmEnableGen2）。
        //   v1.0 的 bin\uninstall.cmd 会把它们清空，之后再跑手动版必须先让驱动重绑策略，
        //   否则复位锁存必然失败 —— 这是历史"卸载后再也解不开"的坑。
        //   需要彻底清除策略时显式加 -purgepolicy。
        if (Environment.GetCommandLineArgs() != null)
        {
            bool purge = false;
            foreach (string a in Environment.GetCommandLineArgs())
                if (a.ToLower().TrimStart('-', '/') == "purgepolicy") purge = true;
            if (purge) { ClearDriverPolicy(); Log("已按 -purgepolicy 清除驱动策略键。"); }
            else Log("已保留驱动策略键（RMPcieLinkSpeed=1 等；要清除请加 -purgepolicy）。");
        }
        Log("已删除计划任务、卸载驱动。");
        Log("（安装目录 C:\\Program Files\\P104Gen2x4 请手动删除）");
        return 0;
    }
}
