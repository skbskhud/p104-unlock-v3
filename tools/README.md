# P104-100 开源工具链（Python）

> **研究用工具。只对你【自己拥有】的硬件使用。**
> 这里写的是 GPU 的**易失寄存器**（BAR0 MMIO + PCIe 配置空间）：不刷 VBIOS、不动熔丝、
> 不改驱动策略注册表。掉卡/断电即恢复；最坏情况需要 **AC 断电（拔电源 30 秒）** 再开机。
> 如果你不能接受"要拔电源"，**不要运行 `--apply`**。

这是 [P104-2.0x16](../README.md) 项目的 Python 侧工具链，配合仓库里 C# v1.1 的
`P104Gen2x4.exe` 使用。目标是让任何人（包括 AI agent）能读懂、复现、并**移植到自己的矿卡**上。

---

## 目录说明

| 文件 | 作用 | 会写硬件吗 |
|---|---|---|
| `read_probe.py` | **只读探针**：设备 ID / PCIe 能力链 / LNKCap / LNKCap2 / LNKSTA / LNKCTL2 / 根端口 / 关键 BAR0 寄存器快照，输出人类可读 + JSON | **不会**（不探测写通道） |
| `apply_gen2_width.py` | 单文件 CLI：施加 live Gen2 写组（+ 可选宽度声明 x16） | 默认**不会**；只有 `--apply` 且非 `--dry-run` 才写 |
| `lib_p104.py` | BYOVD 通道 + PCI 配置空间读/写 + BAR0 MMIO 映射的公共底座 | 提供写函数（工具会保护） |
| `lib_session.py` | 可靠地拿 Session：优先复用已加载的驱动，避免"设备已存在(183)" | 只有探测写通道时发一次 no-op 写请求 |
| `hwlock.py` | 硬件访问锁：任何写/复位前先拿锁，防止两个进程同时写导致掉卡 | 只写锁文件 |

只读 → 写入的正确顺序：

```
read_probe.py  →  apply_gen2_width.py（默认只读）  →  --apply --dry-run  →  --apply
```

---

## 环境要求

- **Windows 10/11 x64**（BYOVD 驱动 + Win32 API，只支持 Windows）
- **管理员权限**（加载内核驱动、读 PCI 配置空间都需要）
- **Python 3.8+**（标准库即可；不需要 pip 安装任何东西）
- **BYOVD 驱动**：`WinRing0x64.sys` + `ThrottleStop.sys`
  默认按以下顺序查找（找到第一个含 `WinRing0x64.sys` 的目录就用）：

  ```
  tools/drivers/
  tools/../P104-2.0x16-v1.1/bin/drivers/
  tools/../P104-2.0x16-Auto/bin/drivers/      （旧包名，兼容保留）
  tools/../P104-2.0x16-Manual/bin/drivers/    （旧包名，兼容保留）
  ```

  本仓库自带的驱动在 `../P104-2.0x16-v1.1/bin/drivers/`（`WinRing0x64.sys` + `ThrottleStop.sys`）。
  也可以显式指定：`--drivers "..\P104-2.0x16-v1.1\bin\drivers"`，
  或设环境变量 `P104_BYOVD_DIR`。

- 杀软对 `WinRing0x64.sys` / `ThrottleStop.sys` 报毒是**预期行为**（BYOVD 通用现象）。
  只在你自己的测试机上运行；不要在自己不拥有的机器上跑。

---

## 先只读：`read_probe.py`

```powershell
# 管理员 PowerShell，本目录下
python read_probe.py

# 机器可读 JSON（方便喂给 AI agent / 存证）
python read_probe.py --json
python read_probe.py --json-out probe.json

# 别的矿卡（改 vendor:device）
python read_probe.py --device 10de:1b87
```

输出包含：

- `[设备]` BDF / `10DE:1B87` / class / BAR0 / PCIe cap 偏移
- `[能力链]` 配置空间能力链（PM → MSI → PCIe → Vendor Specific …）
- `[目标卡链路]` LNKCAP（声明能力）/ LNKSTA（实际链路）/ LNKCAP2（Gen2 声明位）/ LNKCTL2（TLS）
- `[上游根端口]` 根端口的同一组寄存器 + 总线号
- `[BAR0 关键寄存器]` `0x8C040 / 0x8C080 / 0x8C1C0 / 0x8872C / 0x2241C / 0x8841C / 0x8C2C0 / 0x8C300 / 0x88610 / 0x101000…` 及其解码
- `[判定]` 三个判据 + `PASS/FAIL`

判据（与 C# v1.1 完全一致）：

| 判据 | 条件 | 含义 |
|---|---|---|
| J1 | `LNKCAP2 & 0x6 == 0x6` | 卡**声明**支持 Gen2 |
| J2 | `LNKSTA` speed ≥ 2（5.0GT/s） | **实际链路**已经是 Gen2 |
| J3 | `LNKCAP[9:4] == 16` | 声明最大宽度 x16（**声明层**，不是物理宽度） |

> 带宽提升来自 **J2（Gen2）**；x16 是声明层，物理只有 x4 时带宽不变。

---

## 写入：`apply_gen2_width.py`

### 1) 看计划（不写）

```powershell
python apply_gen2_width.py --apply --dry-run
```

打印每个寄存器的 `当前值 -> 目标值`、两端 TLS 写什么、根端口 retrain、
以及宽度 arm 的目标整字。**这一步不写任何硬件。**

### 2) 只施加 Gen2（推荐先只做这个）

```powershell
python apply_gen2_width.py --apply
```

做的事（顺序固定，与 C# v1.1 `ApplyLiveGroup()` 相同）：

```
0x8872C 低4位 = 6              XVE_FUSE_OVERRIDE / CHANGE_SPEED_WIDTH=3
0x2241C bits0,7 = 1            NV_PTOP_FS4（live 声明开关）
0x8841C bits14:13 清 0         NV_XVE_PRIV_MISC_1（GEN2_DIS=0）
0x8C2C0 bit2 = 0               NV_XP_PL_CYA_0
0x8C300 bit4 = 0               NV_XP_PL_CYA_1
0x8C040 SPEED[19:18] = 1       NV_XP_PL_LINK_CONFIG_0（5.0GT/s）
0x8C1C0 [19:16] = 2            NV_XP_PL_LCTRL_2（目标 5.0GT/s）
0x88610 bit0=1, bit12=0        NV_XVE_VSEC_HIERARCHY
```

在这之前/之后还会：

- 若 `0x8C040` 的宽度 arm 位（bit31）已置而链路还在 Gen1 → **整字回退** `0x20085800` 再提速；
- 两端 `LNKCTL2` 目标速率 = 2（5.0GT/s），根端口 `LNKCTL.bit5=1` 触发 retrain，**最多 3 轮**；
- 链路没升上去 → 补一轮 live 写组 + retrain（C# v1.1 的"补刀"，**不做整卡复位**）。

> 本 Python 工具**不做整卡复位**（禁用/启用设备）。复位路径、常驻看门狗请用 C# v1.1。

### 3) 宽度声明 x16

```powershell
python apply_gen2_width.py --apply --width on     # arm：0x8C040=(cur&~0xF8000000)|0x80000000
python apply_gen2_width.py --apply --width off    # 回退：整字写 0x8C040=0x20085800
```

- `--width on` **只在 Gen2 已达成且稳定 2 秒后**才写；没到 Gen2 会拒绝。
- 未生效会自动**整字回退** `0x20085800`（不影响已生效的 Gen2）。
- `--width off` 永远**整字写**安全值，**绝不用掩码单 bit 清 bit31**。

### 参数

```
--apply               显式允许写硬件（不传 = 只读）
--read-only           只读快照（默认；与 --apply 互斥）
--width on|off        宽度声明（写操作，必须同时有 --apply）
--timeout N           写后观察窗口秒数（默认 20）
--dry-run             只打印将写入的寄存器序列
--drivers DIR         BYOVD 驱动目录
--device VVVV:DDDD    目标设备（默认 10de:1b87）
--json-out FILE       结果写 JSON（含写入日志 journal）
--quiet               不打印人类可读报告
--no-lock             ★ 不拿硬件锁（只在你确认没有并发写时用）
```

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 只读完成 / 已达成目标（PASS） |
| 1 | 写完了但没达成（FAIL） |
| 2 | 找不到 BYOVD 驱动或设备 |
| 3 | **掉卡**（设备从总线消失）→ 需要 AC 断电 |
| 4 | 拿不到硬件锁（有别的进程在写硬件） |
| 5 | 安全拒绝（非法 `0x8C040` 值 / 非白名单寄存器） |

---

## 安全规矩（必读）

1. **只对自己拥有的硬件使用。** 矿卡可能是别人的、租的、公司的 —— 不要越界。
2. **先只读，再写；先单变量，再看结果。** 一次只改一个变量，否则出事不知道是哪一步。
3. **只写易失寄存器。** 不要刷 VBIOS、不要动熔丝、不要改电压/风扇曲线。
   这些工具没有实现任何持久化写入，也不应该实现。
4. **写前活体检查、写后回读校验。** 工具已内建：
   - 写前读 `config 0x00` 三次，确认还是 `10DE:1B87`；
   - 写后 20ms 回读，校验目标位；
   - 回读失败/设备失联 → 立即停止，不做任何后续 BAR0 访问。
5. **掉卡立即停手。** 看到 `!! 掉卡` 后：
   ```
   1) 关闭所有 P104 工具（包括常驻按住进程）
   2) 关机，拔电源线，等 30 秒以上
   3) 重新开机，先跑 python read_probe.py 确认卡还在
   4) 别在同一个开机周期里反复重试写入
   ```
   掉卡后继续读 BAR0 可能让系统挂死（10-29 实测教训）。
6. **单写者原则。** 写之前先拿 `hwlock` 锁（工具自动做）。两个进程同时写 = 掉卡。
   锁文件默认在 `%ProgramData%\P104Unlock\HARDWARE.LOCK`，可用 `P104_LOCK_DIR` 覆盖。
7. **CUDA / NVIDIA 驱动会改回寄存器。** 每次 CUDA 上下文初始化，驱动可能把
   `0x8C040.SPEED` 降回 2.5GT/s。需要长期保持时用 C# v1.1 的常驻看门狗
   （150ms 周期，只在判据偏离时补写，触发类写 ≥2 秒限流）。
8. **掉卡 ≠ 卡坏。** 寄存器是易失的；绝大多数情况 AC 断电就回来了。

### `0x8C040` 的三条铁律（实测会掉卡的值）

| 写值 | 结果 |
|---|---|
| `bit31=0` 且 `bit29=0` | **掉卡 2/2**（禁止） |
| `bit31=1` 且 `bit29=1` | **掉卡 1/1**（禁止） |
| `bit31=1` 且 `bit29=0` | 宽度 arm 的合法形态 |
| `bit31=0` 且 `bit29=1` | 工厂安全值 `0x20085800` |

工具在每次写 `0x8C040` 前都会做合法性检查，非法值直接抛 `UnsafeValue`（退出码 5）。
回退宽度**只能整字写** `0x20085800`，**绝不能用掩码单 bit 清 bit31**。

---

## 与 C# v1.1 的关系

| | C# `P104Gen2x4.exe`（v1.1） | Python `tools/`（本目录） |
|---|---|---|
| 定位 | **产品级工具**：安装/卸载、计划任务、常驻看门狗、整卡复位兜底 | **透明、可审计的研究脚本**：每一步都在源码里 |
| 写组顺序 | `ApplyLiveGroup()` | 完全相同（`LIVE_GROUP` 常量） |
| 宽度配方 | `WidthArm()` / `DisarmWidthForSpeed()` | `Applier.width_arm()` / `disarm_width_for_speed()` |
| 0x8C040 合法性 | `Is8C040Legal()` | `read_probe.is_8c040_legal()` |
| 活体检查 | `Alive()`：config 0x00 三连读 | `read_probe.alive3()`：同样 |
| 整卡复位 | 有（SetupAPI 禁用→启用，预算 1 次/运行） | **没有**（故意不做） |
| 常驻保持 | 150ms 看门狗 | **没有**（一次性；保持请用 C#） |
| 适合 | 日常使用 / 开机自动解锁 | 学习原理 / 移植到别的卡 / 交给 AI agent 分析 |

**建议组合**：用 Python 工具做**只读探针、实验、单变量验证**；确认配方后，
用 C# v1.1 做长期保持和自动化。两者写的是同一组寄存器，不会互相冲突
（但**不要同时运行**，会互相改寄存器；`hwlock` 只能保护 Python 侧）。

---

## 输出 JSON / 给 AI agent 用

```powershell
python read_probe.py --json --json-out probe.json
python apply_gen2_width.py --apply --dry-run --json-out plan.json
python apply_gen2_width.py --apply --json-out result.json
```

JSON 里关键字段：

```jsonc
{
  "mode": "read-only",              // read-only / dry-run / apply
  "device": { "bdf": "01:00.0", "vendor_id": "0x10DE", "device_id": "0x1B87" },
  "link": { "lnkcap": …, "lnksta": …, "lnkcap2": …, "lnkctl2": …, "tls": … },
  "root_port": { "bdf": "00:01.0", "link": { … } },
  "bar0": { "0x8C040": { "value": …, "hex": "0x…", "decoded": "SPEED=… 宽度arm=…" } },
  "judge": { "j1_declared_gen2": true, "j2_link_gen2": false, "j3_width16_declared": false,
             "result": "FAIL" },
  "baseline": { "0x8C040": "0x…" }, // 写前基线（apply 模式）
  "journal": [ { "kind": "bar0", "offset": "0x8C040", "old": "0x…", "new": "0x…",
                 "readback": "0x…", "ok": true, "note": "…" } ],
  "lost": false
}
```

`journal` 是完整的写入账本；出问题时可据此判断**哪一步**写坏了。

---

## 把同样的思路用到别的矿卡（方法论）

这套方法的本质是：**在官方驱动里找到"PCIe 速率/宽度声明"的寄存器写组，
用 BYOVD 通道绕开驱动直接写，并让链路两端重新协商。**

以 NVIDIA Pascal 矿卡为例，步骤是：

1. **确定设备 ID**：`--device VVVV:DDDD`（例如 P104-100 是 `10de:1b87`）。
   `read_probe.py` 的枚举逻辑在 `lib_session._find_gpu()` / `read_probe.discover()`。
2. **先只读探针**：跑 `read_probe.py --json`，确认：
   - 有 PCIe 能力结构、`LNKCAP2` 是什么（Gen2 声明位是否被钉死）；
   - `BAR0` 的 `0x8C040` 等寄存器能不能读；
   - 上游是 CPU 直连根端口还是 PCH 端口（决定要不要整卡复位）。
3. **验证写通道**（**必须做**，否则后面所有"写成功"都是假的）：
   对 PCI 配置空间 `0x04`（Command）做**可逆**写：
   `读 → 写 (原值|1) → 读回 → 恢复原值 → 再读回`。
   读回值必须等于写入值；不等说明写 IOCTL 功能号/access 位算错了。
   （`lib_p104.IOCTL_PCI_WRITE = 0x9C40A148` 是 WinRing0 实测可用的值。）
4. **再单变量写寄存器**：一次只改一个寄存器，写后回读 + 读 LNKSTA。
   工具里的 `LIVE_GROUP` 就是 P104 的最终答案（8 个寄存器），
   但**别的卡不会一样**：先在厂商驱动里反汇编找写组（Pascal 的
   `pcie_pascal_gen2_write_group` / `pcie_pascal_gen2_override` 就是这么找到的），
   或者从开源驱动（nouveau / amdgpu）里抄寄存器定义。
5. **最后处理"保持"**：驱动会抢改寄存器。确认配方有效后再写常驻看门狗，
   并给它加**触发限流**（两次触发类写 ≥2 秒），不要把 CHANGE_SPEED 打成 50Hz。

移植时要改的地方：

| 位置 | 改什么 |
|---|---|
| `lib_p104.py` 的 `WR` / `TS` | 驱动路径（或 `--drivers` / `P104_BYOVD_DIR`） |
| `read_probe.py` 的 `VENDOR_P104` / `DEVICE_P104` / `PHYS_ID` | 你的卡的 vendor:device |
| `read_probe.py` 的 `BAR0_REGS` | 你的卡的寄存器表 |
| `read_probe.py` 的 `decode_bar0()` / `PCFG_SPD` | 位域定义 |
| `apply_gen2_width.py` 的 `LIVE_GROUP` / `REG_NAMES` | 你的卡的写组（顺序通常重要） |
| `apply_gen2_width.py` 的 `LINKCFG0_SAFE` | 你的卡的"安全整字" |
| `lib_p104.Session.find()` | 如果你的卡不在 `0x1B87` 或有多 function |

> ★ 不要直接改常量就 `--apply`：先 `--dry-run` 看清楚每个目标值，
> 再对照厂商驱动的写组逐条核对。

---

## 可以交给 AI agent 的提示词模板

把仓库交给 AI agent 时，建议把下面这段作为系统提示/首条消息：

```text
你是一个 PCIe 硬件研究助理，工作目录是本仓库的 tools/。
目标：帮我把 <你的矿卡型号，例如 NVIDIA P104-100 / 10DE:1B87> 从 Gen1 x4 解锁到 Gen2，
      并（可选）把 LnkCap 的最大宽度声明改成 x16。

硬性规则（必须遵守）：
1. 先只读，后写入。任何时候都先跑：
     python read_probe.py --json --json-out probe.json
   并把关键结论（J1/J2/J3、LNKCAP2、LNKSTA、根端口）复述给我。
2. 在我明确说"可以写"之前，禁止执行任何带 --apply 且不带 --dry-run 的命令。
   允许执行：read_probe.py、apply_gen2_width.py --apply --dry-run。
3. 禁止：刷 VBIOS、写熔丝、改驱动策略注册表、改电压/风扇、装/卸系统驱动服务
   （tools 内部为拿 BYOVD 通道加载驱动是允许的）。
4. 任何写操作前必须确认：
     a) 我已拿到硬件访问权（没有别的进程在跑 P104 工具）；
     b) 你已拿到 hwlock（apply_gen2_width.py 会自动拿，不要用 --no-lock 绕过）；
     c) 你已记录写前基线（--json-out 的 baseline）。
5. 一次只改一个变量；每步写入后必须回读，并把 journal 贴给我。
6. 如果看到 "!! 掉卡" 或退出码 3：
     立即停止一切写入，把 AC 断电指引贴给我，不要重试。
7. 遇到失败先解释为什么，再给下一步选项（最多 3 个），不要擅自复位/重启/连续重试。
8. 所有结论必须引用实际读到的寄存器值（十六进制），不要编造。

工作流程：
  Step 1  read_probe.py，解读 J1/J2/J3
  Step 2  apply_gen2_width.py --apply --dry-run，逐条核对写入计划
  Step 3  我确认后：apply_gen2_width.py --apply，检查退出码与 RESULT
  Step 4  仅在 Gen2 PASS 后，才讨论 --width on；并说明 x16 是声明层、物理仍是 x4
  Step 5  给出下一步建议（保持方案用 C# v1.1 的常驻看门狗）
```

更保守的版本：把第 2 条改成"每一步等你回复 'approve' 再执行"，适合完全不想让 agent 自主写的场景。

---

## 常见问题

**Q：`read_probe.py` 报打不开 BYOVD 通道？**
A：① 不是管理员；② 驱动目录不对（用 `--drivers` 指定）；③ 卡已经从总线掉了（设备管理器里看不到 `10DE:1B87`）→ AC 断电。

**Q：跑完 `--apply` 但 `RESULT: FAIL`？**
A：按顺序排查：① 是否 CPU 直连 x16 槽（PCH 槽的行为可能不同）；② NVIDIA 驱动策略是否 `RMPcieLinkSpeed=1`；③ 是否刚做过 CUDA 初始化（驱动会把 SPEED 改回 2.5GT/s）；④ 再跑一次 `--apply`（live 写组可重试）；⑤ 用 C# v1.1 `-status` 交叉验证。

**Q：为什么 `--width on` 被拒绝？**
A：v1.1 的规矩：**先达成 Gen2，再 arm 宽度**。链路还在 Gen1 时 arm 宽度会干扰速率协商（10-30 实测）。先让 `--apply` 出 PASS。

**Q：宽度显示 x16 了，但带宽没变？**
A：正常。x16 是 **LnkCap 声明层**；物理只有 4 条 lane 时链路仍是 x4、带宽不变。真正 x16 需要补 AC 耦合电容（本项目未验证）。

**Q：两个工具能同时跑吗？**
A：不能。C# 常驻进程和 Python 脚本会互相改寄存器。停掉一个再跑另一个。

---

## 免责声明

本项目是**硬件逆向研究**记录，不是官方工具。作者不对任何硬件损坏、数据丢失、
保修失效或法律问题负责。使用即表示你理解：

- 你的卡可能**变砖**（虽然理论上 AC 断电可恢复）；
- BYOVD 驱动会被杀软/EDR 拦截；
- 修改 PCIe 链路能力可能违反某些地区的法规/保修条款；
- **只对你拥有的硬件、在你自己的责任范围内使用。**
