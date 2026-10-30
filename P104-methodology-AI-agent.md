# 矿卡 PCIe 解锁方法论 + AI-Agent 指南

> 本文把本项目从 0 到 1 的**破解思路、判据、踩坑和安全规则**完整写下来。
> 目标：别人拿到这张文档 + 仓库 `tools/`，可以复现 P104-100 的解锁，也能把这套方法迁移到别的矿卡。
>
> **适用范围**：仅限你自己拥有的硬件。本项目只做**易失寄存器**操作（断电即恢复），不刷 VBIOS、不烧熔丝。
> 换卡/换平台时，先按本文做只读阶段，再决定要不要写。

---

## 0. TL;DR：一张流程图

```
① 只读基线（设备 ID / PCIe cap / BAR0 关键寄存器 / 根端口 / 驱动值交叉）
      │
② 写通道对照（对只读寄存器做同值写，证明"这次会话确实能写"）
      │
③ 找"声明开关"（单变量：一次只改一位，立即读 LnkCap2）
      │
④ 找"速率请求"（LINK_CONFIG SPEED / LCTRL2 / 两端 TLS）+ 根端口 retrain
      │
⑤ 训练成功？ ── 否 ── 才考虑整卡复位（预算 1 次，复位前 bit15=1）
      │ 是
⑥ 宽度/其它声明位（安全配方 + 四象限断言）
      │
⑦ 常驻保持（150ms 看门狗，只补偏离，触发类写限流）
      │
⑧ 验收：寄存器判据 + 带宽实测（唯一不骗人的判据）
```

---

## 1. 先建立"四层模型"

矿卡的 PCIe 能力被四层东西管着。**先搞清楚每一层能不能动，再谈解锁**：

| 层 | 能改什么 | 入口 | 典型判据 |
|---|---|---|---|
| ① 能力声明层 | `LnkCap` / `LnkCap2` / `LnkSta` / `LnkCtl2` | PCIe 配置空间（cap 链） | `LnkCap2` 支持速率向量、`LnkCap` maxwidth |
| ② 链路控制层 | XVE/XP 寄存器（声明开关、速率请求、宽度 arm） | GPU BAR0 | 写后 `LnkCap2`/`LnkSta` 立即变化 |
| ③ 驱动策略层 | `RMPcieLinkSpeed` / `RmForceEnableGen2` / `RmEnableGen2` | 注册表 `Class\{4d36e968-...}\<nnnn>` | 复位/重绑后驱动是否允许 Gen2 |
| ④ 固件/熔丝层 | VBIOS DevInit、OTP 熔丝（Gen3、NVENC/NVDEC 等） | VBIOS / PFUSE | 只读值 + 写不进去 ⇒ 通常无解 |

**原则**：
- ①/② 层通常"可写、易失、可逆"，是解锁的主要战场；
- ③ 层只是辅助（让驱动不把链路压回去）；
- ④ 层要先做**只读取证**，再下"不可解"结论 —— 结论必须附证据，不能只说"试过了不行"。

---

## 2. 只读基线（永远第一步）

要记录的东西（缺一不可）：

```
设备：10DE:1B87、BDF、BAR0 地址、上游根端口 BDF
PCIe cap：cap 位置、LnkCap(0x0C)、LnkCap2(0x2C)、LnkSta(0x12)、LnkCtl2(0x30)、LnkSta2(0x32)
BAR0：0x8872C / 0x2241C / 0x8841C / 0x8C040 / 0x8C1C0 / 0x8C2C0 / 0x8C300 / 0x88610（本卡）
根端口：LnkCap/LnkSta（交叉验证链路速率）
驱动侧：nvidia-smi / PnP 属性（注意：驱动上报值在矿卡上可能不可靠，只作旁证）
```

工具：`tools/read_probe.py`（只读，不写任何寄存器）。

**为什么先只读**：
- 确认"读通道正常"（不是所有地址都能读；有些会返回 `0xBADFxxxx` 哨兵 = 未实现）；
- 记录基线，后面所有"写进去没有"都要跟它对比；
- 掉卡/异常时能判断是"本来就没有"还是"刚被写坏"。

---

## 3. 写通道对照（安全三律）

在写任何"有副作用"的寄存器前，先证明**当前会话确实能写**：

1. **写前活体**：每次写前读一次 `config 0x00`（设备 ID），连续 3 次都读不到才判掉卡；
2. **写后回读**：写后 20 ms 再读同一地址，必须等于期望值（或至少位匹配）；
3. **非法值断言**：对本卡 `0x8C040`，写之前必须检查 bit31/bit29 组合（见 §7），非法直接拒绝。

对照实验：对一个**只读**寄存器（如 `config 0x00`）做"同值写"，请求成功即说明写通道可用。
记住：**写不进去**和**写进去了但被硬件忽略**是两件事，要分开判。

> 出任何异常（连续读失败、`0xFFFFFFFF`）立即停手。掉卡只能 AC 断电 30 秒恢复，软件救不回。

---

## 4. 找"声明开关"：单变量法

这是整个解锁的核心。不同卡的寄存器位置不同，但方法一样：

### 4.1 候选来源
- 同架构/同代卡的公开逆向（例如 NVPermissive 的 Pascal gen2 写组）；
- 固件/驱动的写入表（VBIOS DevInit、驱动二进制里的 MMIO 写序列）；
- 社区已知的寄存器名（`NV_XVE_PRIV_MISC_1`、`NV_PTOP_FS4`、`NV_XP_PL_LINK_CONFIG_0` 等）。

### 4.2 方法
- **一次只改一个寄存器/一个位**（单变量），写完 100 ms 内读 `LnkCap2`；
- 记录：`old → new → readback → LnkCap2 → 时间`；
- 判据：`LnkCap2` 的 5 GT/s 支持位从 0 变 1（本卡是 `0x02 → 0x06`）。

### 4.3 P104-100 上的实测结论（可直接抄）

| 寄存器 | 运算 | 作用 |
|---|---|---|
| `0x8841C` | `old & ~0x6000` | GEN2_DIS（bits13,14）清 0 —— **声明开关** |
| `0x2241C` | `old \| 0x81` | PTOP_FS4 的声明位 —— **第二条声明开关** |
| `0x8872C` | `(old & ~0xF) \| 6` | XVE/LTSSM 覆盖使能 |
| `0x8C2C0` | `old & ~0x4` | CYA_0 bit2 = 0 |
| `0x8C300` | `old & ~0x10` | CYA_1 bit4 = 0 |
| `0x8C040` | `(old & ~0xC0000) \| 0x40000` | 速率请求 SPEED = 5.0 GT/s |
| `0x8C1C0` | `(old & ~0xF0000) \| 0x20000` | LCTRL2 目标速率 = 5.0 GT/s |
| `0x88610` | `(old & ~0x1000) \| 0x1` | VSEC 门 |

**关键组合**：声明开关（前两条）+ 速率请求（`0x8C040`/`0x8C1C0`）必须一起写。
只写声明 → 声明有了但链路停在 Gen1；只写速率 → 链路升不上去。

### 4.4 常见误区
- 只改一个"看起来像使能"的位，没做单变量对照 → 无法判断是谁起的作用；
- 把"复位后能锁上"误当成"写组有效" → 其实复位只是在赌驱动重初始化（本项目 v1.0 就是这个问题）；
- 忽略 bit15 这类"复位前置位"（只影响复位锁存路径，不影响 live 路径）。

---

## 5. 训练链路

1. 两端 `LNKCTL2`（cap+`0x30`）目标速率设为 2（5.0 GT/s）；
2. 根端口 `LNKCTL`（cap+`0x10`）bit5 = 1 触发 retrain；
3. 轮询 `LNKSTA`（cap+`0x12`）：`speed=2`（5 GT/s）、`width=4` 即成功；
4. 重训要限流：两次触发之间 ≥1–2 秒，不要 50 Hz 打触发位；
5. 声明有了但速度没上 → 补写速率 + 再 retrain；仍不行才考虑复位。

> 本卡实测：根端口 target speed 写 3（8 GT/s）会被端点钳回 2（Gen2），所以 Gen3 不可能通过这条路径达成。

---

## 6. 复位兜底（能不折腾就不折腾）

复位 = 用 SetupAPI `DIF_PROPERTYCHANGE` 把设备禁用 → 保持几秒 → 启用。它会闪屏、可能掉卡，**只在 live 路径失败时用**.

经验规则（本卡）：
- 复位前 `0x8841C` bit15 必须 = 1（实测 7/7 必要性），写后要回读确认；
- 复位后给驱动 4–8 秒完成重绑，再读 `LnkCap2`；
- 复位后**不管锁没锁上，都再跑一遍 live 写组**（"补刀"），把成功率补上来；
- 预算：单次运行 1 次、同一开机周期 2 次、两次间隔 ≥10 分钟；
- "设备离开总线=True"这个判据没用（本卡 59/59 都是 False），别拿它判成败；
- 设备 30 秒没回来 → 停手，提示用户 AC 断电，绝不反复试。

---

## 7. 声明宽度（先分清"声明"和"物理"）

本卡：写 `0x8C040` 的 bit31 会让 `LnkCap` 的 maxwidth 从 x4 变成 x16 —— **只改声明，不改物理宽度**。

安全配方：
```
new = (cur & ~0xF8000000) | 0x80000000
```

四象限（实测，务必写进断言）：

| bit31 | bit29 | 结果 |
|---|---|---|
| 1 | 0 | ✅ 安全 |
| 0 | 1 | ✅ 安全（工厂/回退态） |
| 0 | 0 | ❌ 掉卡（2/2） |
| 1 | 1 | ❌ 掉卡（1/1） |

- 回退：**整字写** `0x20085800`，禁止掩码单 bit 清 bit31；
- 宽度 arm 后如果链路掉回 Gen1：先整字回退，再提速，成功后再重新 arm；
- 物理宽度 = min(两端声明, GPU strap, PCB 走线/AC 耦合电容)；补电容能否成功，需要单独做物理实验验证。

---

## 8. 常驻保持（watchdog）

为什么需要：NVIDIA 驱动在**每次 CUDA 上下文初始化**时会把 `0x8C040.SPEED` 降回 2.5 GT/s，链路掉回 Gen1。

设计要点：
- 周期 150–200 ms（20 ms 没必要，还会把触发位打成 50 Hz）；
- **只在偏离时写**：`LnkCap2`、`LNKSta.speed`、`0x8C040.SPEED`、`LnkCap.maxwidth` 四项分别判；
- 触发类写（retrain）限流 ≥2 秒；
- 连续失败要有退出机制（交计划任务重启），不要无限 while；
- 状态写到 `C:\ProgramData\P104Gen2x4\当前状态.txt`（带原始寄存器值 + 时间戳），方便人工核对。

---

## 9. 验收：唯一不骗人的判据

| 判据 | 期望（本卡） |
|---|---|
| `LnkCap2`（cap+0x2C） | `0x00000006`（含 5 GT/s 声明） |
| `LNKSTA`（cap+0x12） | `0x00001042`（5 GT/s x4） |
| `LnkCap`（cap+0x0C） | `0x00454D02`（maxspeed Gen2 + maxwidth x16） |
| 带宽（CUDA pinned） | H2D ≥1.4 GB/s（Gen1 x4 约 0.78） |

**不要用 GPU-Z / nvidia-smi 的 `pcie.link.gen.current` 当判据**：矿卡上驱动上报值不可靠，经常一直显示 1。
也不要只看一个字段：声明、实际速率、宽度、带宽是四件事。

---

## 10. 迁移到别的矿卡（P106 / CMP 系列等）

1. **选同代同架构**优先（GP104 → GP102/GP106/GP107；P106-100 等）；
2. 换设备 ID（如 P106-100 是 `10DE:1B83`），先用 `tools/read_probe.py` 做只读基线；
3. 对比 BAR0 关键寄存器基线（是否同一套 XVE/XP 布局）；
4. 用本文 §4 的单变量法重新确认"声明开关"（同代大概率同位置，但要实测）；
5. 确认速率请求字段与根端口行为（PCH 槽 vs CPU 直连槽可能不同）；
6. 宽度声明、Gen3、编解码器这些独立结论要单独验证 —— 不要从 P104 外推；
7. 每张卡/每个平台都重新做一次 §9 的验收。

工具：`tools/read_probe.py`（只读）、`tools/apply_gen2_width.py`（写，默认只读模式，需要显式 `--apply`）。

---

## 11. 交给 AI agent：怎么用这套东西

你完全可以把**本仓库 + 本文 + `tools/`** 一起丢给 AI agent（Claude Code / Codex / DSH / 其它支持工具调用的 agent），让它帮你分析、适配、甚至替你写脚本。建议这样分工：

### 11.1 提示词模板（中文，可直接改）

```
你是硬件寄存器实验助手。目标：对 <你的卡型号> 做 PCIe 链路解锁实验。
硬规则：
1) 第一阶段只读：先读设备 ID、PCIe cap、BAR0 关键寄存器、根端口状态，输出基线表，不做任何写操作；
2) 第二阶段写通道对照：只对只读寄存器做同值写，证明通道可用；
3) 第三阶段单变量：一次只改一个寄存器/一位，写完 100ms 内回读并记录 LnkCap2/LnkSta；
4) 任何写操作前先读 config 0x00 三连（活体），写后 20ms 回读；异常立即停手；
5) 不刷 VBIOS、不写熔丝、不重启机器；掉卡只提示 AC 断电；
6) 每一步都写日志（old→new→readback→结论），不要跳步。
参考资料：仓库 docs/07-破解方法论与AI-Agent指南.md、tools/、docs/06-v1.1-更新说明.md
```

### 11.2 给 agent 的边界（很重要）

- **只读阶段没跑完，不许进入写阶段**；
- 每个写操作都要"活体检查 + 回读"；
- 非法 `0x8C040` 值（bit31=0&bit29=0 或 bit31=1&bit29=1）必须拒绝；
- 复位预算要硬限制（单次 ≤1、同开机周期 ≤2），失败不许自动重试；
- AI agent 也会犯错：**真正的写操作前，人工复核一遍它要写的地址和值**，并准备好 AC 断电。

### 11.3 你可能需要改的东西

- `tools/read_probe.py` 里的 `DEVICE_ID`（当前 `10DE:1B87`）；
- `tools/apply_gen2_width.py` 里的寄存器表（如果是同代卡，通常同一套布局，但仍需实测）；
- C# 一键包只支持 P104-100；别的卡建议先用 Python 工具做实验，确认稳定后再做图形化/一键包。

---

## 12. 安全与伦理

- **只对自己拥有的硬件操作**；不要对租用/他人设备使用；
- 本项目只写**易失寄存器**：不刷 VBIOS、不烧熔丝、不改持久化配置，断电即恢复；
- 掉卡（设备从总线消失）只能彻底断电 30 秒恢复；工具会停手，不会让你陷入"反复复位"循环；
- BYOVD 驱动（WinRing0/ThrottleStop）会被杀软/反作弊拦截，这是预期行为；
- 本文不提供熔丝攻击、VBIOS 刷写、破解 DRM 等内容；
- 因使用本工具造成的任何后果由使用者自行承担。

---

## English summary

This document describes the full methodology behind the P104-100 PCIe unlock and how to reuse it on other mining
cards: **read-only baseline → write-channel control test → single-variable search for the declaration switch →
speed request + retrain → reset only as a fallback → width declaration (safe quadrants) → 150 ms watchdog →
register + bandwidth verification**. The repository ships the executable tool (`P104-2.0x16-v1.1/`), the Python
experiment toolchain (`tools/`), and this methodology so that you can hand the whole thing to an AI agent and
adapt it to a card you own. All writes are volatile (no VBIOS/fuse flashing); only use it on hardware you own.
