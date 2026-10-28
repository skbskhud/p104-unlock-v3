# P104 一键组合包 v3.0（Gen2 x4 + 声明宽度 x16）

> 2026-10-29 打包。把两件事合成一个一键工具：
> 1. **Gen2 x4**（2.0 系列的能力，集成进同一个保持进程，不再需要单独跑 Gen2 工具）
> 2. **声明宽度 x16**（2026-10-28 新发现：`0x8C040 bit31=1` 可把端点 LnkCap 最大宽度从 x4 改成 x16）
>
> 剩下物理层（x4→x16 的真实 lane/AC 耦合电容）在你手里；工具负责让状态**一直保持、开机自动**。

## 当前机器上的状态（2026-10-29 00:01 实测）

```
LNKCAP  = 0x00454D02  → maxspeed = Gen2(5GT), maxwidth = x16   ✔ 声明已到位
LNKCAP2 = 0x00000006  → 声明 2.5/5.0 GT/s（Gen2）              ✔
LNKSTA  = 0x00001042  → 当前链路 Gen2 x4                        ✔（Gen2 已生效）
BAR0: 0x8C040=0x80045800（bit31=1 + SPEED=1）  0x8872C=6
计划任务 "P104 PCIe Width x16 Hold": Running（SYSTEM / 登录触发 / 隐藏）
驱动 P104LibWr / P104LibTs: auto-start（开机自动加载）
```

## 一键文件

| 文件 | 作用 |
|---|---|
| `一键解锁.cmd` | ★ 自动提权：装/确认驱动 → 注册 SYSTEM 登录任务 → 立即施加 Gen2+宽度x16 → 打印状态 |
| `一键验证.cmd` | 看三项状态（声明 Gen2 / 声明 x16 / 当前链路），可顺手 retrain |
| `一键恢复.cmd` | 停任务 + 寄存器回出厂 + 删驱动服务 |

> 三个 cmd 里用的是本机绝对路径 `D:\workplace2\dsh-workspace\...`（工具本体在
> `_scratch\directions\width16\`）。换机器需改路径。

## 里面做了什么（寄存器配方，均为易失写）

```
Gen2:
  0x8872C  = (v & ~0xF) | 6        # NV_XVE_FUSE_OVERRIDE
  0x2241C |= 0x81                  # PTOP_FS4 GEN2_PCIE 位
  0x8841C &= ~0x6000               # 清 GEN2_DIS → LNKCAP2 0x06
  0x8C2C0 &= ~0x4 ; 0x8C300 &= ~0x10
  0x8C040 SPEED[19:18] = 1         # 5.0GT
  0x8C1C0 [19:16] = 2              # LNK_CAP_SPEED=5GT
  0x88610 |= 1
  根端口 LNKCTL2 TLS=3 + Retrain → 链路训到 Gen2 x4
宽度 x16:
  0x8C040 bit31 = 1                # arm/commit：实时把 LnkCap 最大宽度改成 x16
```
看门狗每 100ms 复查补写；开机登录自动施加并 retrain 一次。
**不刷 VBIOS、不烧熔丝**，断电即恢复。

## 补完电容之后

1. 断电焊电容 → 开机登录（任务自动施加 + retrain）
2. 双击 `一键验证.cmd`：
   - `LNKSTA ... width=16` ⇒ 物理层也通了，测带宽（Gen2 x16 ≈ 6 GB/s 量级）
   - 仍 `width=4` ⇒ lane/走线还没通，可再 retrain 或继续查
3. 想确认声明：`一键验证.cmd` 里 `LNKCAP maxwidth=x16` 就是声明到位

## Gen3（3.0）结论：不可达（2026-10-28 最后一枪已打完）

- 端点把 Gen3 目标速率钳回 Gen2；`LNKCAP2` 的 8GT 能力位从未出现；`LNKSTA2` 均衡位全程 0
- 熔丝 `FUSE_PCIE_GEN3_DIS = 1`（OTP，主机只读）；在 arm 状态下写 `SYSTEM_MAX_SPEED=8GT` 仍被硬件拒绝
- 170HX 有 SEC2/HS 权限、强制 PHY 速率也训练不到 Gen3；P104 无 SEC2，PMU 路线实测封死
⇒ **要 Gen3 只能换卡或物理熔丝攻击（不建议）**。宽度 x16 才是这张卡可行的 4 倍带宽路线。

## 风险

- 驱动是 BYOVD（WinRing0/ThrottleStop），安全软件会报警，开机自启；不想要用 `一键恢复.cmd` 清理
- 保持任务会占用 `_scratch\HARDWARE.LOCK`（owner=width16-hold）；做别的硬件实验前先停任务
- 声明 x16 但只有 4 lane 时 retrain 实测安全（回落 x4）；若掉卡 → 完全断电冷启动即可
- 与 2.0 Gen2 工具（`Gen2-2.0/P104Gen2x4.exe`）**不要同时跑**：二选一。本包的一键已经包含 Gen2。

## 目录

```
一键解锁.cmd / 一键验证.cmd / 一键恢复.cmd
README.md                                  ← 本文件
tool/width16_hold.py  tool/read_regs.py    ← 工具本体（实际运行位置见下）
Gen2-2.0/P104Gen2x4.exe + 文档              ← 原 2.0 多卡 Gen2 工具（备查/备选）
```
> 实际生效的工具本体在 `D:\workplace2\dsh-workspace\_scratch\directions\width16\`；
> 本包 `tool/` 是同一份代码的拷贝，便于归档。
