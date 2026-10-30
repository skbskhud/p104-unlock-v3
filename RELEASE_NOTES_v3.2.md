# v3.2 — P104-100：Gen2 x4 解锁 + x16 宽度声明（工具 v1.1，完整开源）

> 发布：2026-10-30
> 资产：`P104-2.0x16-v1.1.zip`（Windows 工具） / `P104-2.0x16-v1.1-修复说明.md` / `P104-methodology-AI-agent.md`
> 源码与方法：本仓库 `tools/`、`docs/06-v1.1-更新说明.md`、`docs/07-破解方法论与AI-Agent指南.md`

## 致歉与升级提醒

v3.1 的两个 2.0x16 包（Manual / Auto v1.0）在部分机器上**无法稳定锁存**。当时的解锁写组只覆盖
"整卡复位后等驱动重新绑定"这一条路径，冷启动/复位窗口内成功率很低，表现为"有时能解锁、
有时反复超时甚至反复黑屏"。

给下载过、尝试过的朋友添了麻烦，抱歉。

**请删除旧包，改用 v3.2 的 `P104-2.0x16-v1.1.zip`。** 新版本改走实时（live）写入路径，
正常情况下**不需要整卡复位**，约 3 秒完成。

## 本版变化（工具 v1.0 → v1.1）

1. **新增 live 声明写组**：`0x8841C` 的 GEN2 使能位 + `0x2241C` 声明位 + 速率请求一起写，
   `LnkCap2` 在 1 秒内出现 Gen2 声明；不再依赖整卡复位。
2. **整卡复位降级为兜底**：仅当 live 连续 3 轮失败才做 1 次；复位前置 bit15=1 并回读断言；
   预算：单次运行 ≤1 次、同一开机周期 ≤2 次、间隔 ≥10 分钟。
3. **宽度声明安全配方**：`0x8C040 = (cur & ~0xF8000000) | 0x80000000`；四象限断言（两个已知掉卡象限直接拒绝）；
   回退永远整字写 `0x20085800`。
4. **看门狗 20 ms → 150 ms**：只在四项判据偏离时补写，触发类写限流 ≥2 秒。
5. **参数白名单**：`-help` / 未知参数只打印用法并退出（旧版会误触发解锁）。
6. **输出编码修复**：cmd 全部纯 ASCII + CRLF；日志不再出现乱码。
7. **安装幂等 / 卸载保留驱动策略键**：已安装状态重跑 install 不再中断；卸载不再清 `RMPcieLinkSpeed` 等键。
8. **带宽测试体验**：测完自动等待链路恢复（Gen2 + x16，最多 30 秒）；状态页在恢复期提示"正在自动恢复"。

## 实测结果（2026-10-30，本机单卡，CPU 直连 x16 槽）

| 项目 | 结果 |
|---|---|
| 出厂态（`LCAP2=0x02`、TLS=0）`-once` | **0 次复位**；写组后 **0 ms** 锁存；3 秒内 Gen2；2 秒后宽度 x16；总 7.4 秒 PASS |
| `-off` 后再 `-once` | **2.8 秒** PASS |
| 一键流程（install + 计划任务）从出厂态 | 40 秒内 `-check` 显示 Gen2 x4 + 声明 x16 |
| PCIe 链路 | Gen2 x4（`LNKSTA=0x00001042`） |
| `LnkCap` 声明 | `0x00454D02`（maxspeed Gen2 + **maxwidth x16**） |
| CUDA 带宽（页锁定） | **H2D 1.570 / D2H 1.673 GB/s**（Gen1 x4 参考 0.78 / 0.85） |
| 保持 | 150 ms 看门狗；历史实验连续保持 29 分钟无掉速；外部干扰后 10–20 秒自动恢复 |

## 边界（务必阅读）

- **x16 目前是"声明层"**：`LnkCap` 显示 maxwidth=16 是真实写入的，但物理只有 4 条 lane，
  实际链路仍是 x4，**带宽不会因为 x16 声明而提升**。
- **物理层（真实 x16 链路）**：计划 **2026-10-31（周六）** 补 AC 耦合电容后实测，结果会更新到本仓库。
  在实测完成前，本版**不声称**物理 x16 可用。
- **Gen3（8.0 GT/s）不可解**：能力声明无法形成，主机侧无入口（证据见 `docs/05-功能边界总表.md`）。
- **NVENC / NVDEC / OFA 不可解**：OTP 熔丝禁用。
- **干净 AC 冷启动后的自动拉起**尚未实测（本次用"计划任务手动触发"覆盖了等价路径）。
- 掉卡（设备从 PCIe 总线消失）只能 **AC 断电 30 秒**恢复，软件复位救不回。

## 资产与校验

| 文件 | 大小 | SHA256 |
|---|---|---|
| `P104-2.0x16-v1.1.zip` | 77,443 | `121E7284B0627CEF73387A29251F900ECF44EC68DB48A099FB31EE791029EBE6` |
| └ `bin\P104Gen2x4.exe` | 65,536 | `E24811FA4C2ACD517BD2B3C00FA30CA4CC82575C77D2298C9F932ECD01A4821A` |

包内另有 `SHA256.txt`（exe + WinRing0x64.sys + ThrottleStop.sys）。

## 快速开始

1. 完整解压 `P104-2.0x16-v1.1.zip` 到本地硬盘；
2. 右键 `一键解锁.cmd` → 以管理员身份运行；
3. 看到 `SUCCESS` 即完成；跑 `带宽测试.cmd` 验证（H2D ≥1.4 GB/s）；
4. 日常查状态：`状态.cmd`（只读，不需要管理员）；
5. 停止：`停止按住.cmd`；卸载：`卸载.cmd`（默认保留驱动策略键）。
6. **窗口能不能关**：一键解锁装好后**可以关窗口**（保持进程在后台任务里）；手动解锁的窗口**不能关**（窗口就是保持进程）。

## 完整开源：工具 + 思路

本仓库从 v3.2 起完整公开：

- `P104-2.0x16-v1.1/`：可执行工具（Windows）；
- `tools/`：Python 实验工具链（只读探针、live 写组、宽度声明、硬件锁）；
- `docs/07-破解方法论与AI-Agent指南.md`：方法论（四层模型、标准流程、安全规则、迁移到其它矿卡）；
- `docs/06-v1.1-更新说明.md`：v1.0 → v1.1 技术说明与实测数据；
- `docs/05-功能边界总表.md`：能力边界（哪些可解、哪些不可解）。

> **你可以把它交给 AI agent**：如果你手上有别的矿卡（P106 / CMP 系列等），
> 可以把本仓库的 README、方法论和 `tools/` 一起交给 AI agent（Claude Code / Codex / DSH 等），
> 让它按同样的流程——只读探针 → 写通道对照 → 单变量找声明开关 → 速率/宽度 → 常驻保持 → 带宽验收——
> 帮你分析、适配属于你自己的卡。工具是开源的，思路也是。

## 从旧版升级

- 删除 v3.1 的 `P104-2.0x16-Manual` / `P104-2.0x16-Auto` 解压目录；
- 运行 v3.2 的 `一键解锁.cmd`（会自动替换 `C:\Program Files\P104Gen2x4` 下的旧 exe 并重建计划任务）；
- 驱动策略键会保留/重写，无需手动处理。

## English summary

v3.2 replaces the v3.1 x16 packages (whose reset-only latch path was unreliable — sorry for the trouble)
with a **live-write unlock** that needs **no card reset** and completes in ~3 s. Verified on a P104-100
(CPU-attached slot): Gen2 x4 link, `LnkCap maxwidth = 16` declaration, CUDA H2D 1.570 / D2H 1.673 GB/s.

**Important:** x16 is declaration-layer only; the physical link is still x4. A physical-x16 experiment
(AC coupling caps) is planned for 2026-10-31 and will be reported here.

The repo now ships the full open-source package: the Windows tool, a Python experiment toolchain (`tools/`),
and a complete methodology document (`docs/07-...`) so you can hand it to an AI agent and adapt it to a card
you own. Volatile registers only — no VBIOS/fuse flashing.
