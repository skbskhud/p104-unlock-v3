# P104Gen2x4 v2.0-beta（多卡 BETA）

> **BETA 实验版**：单卡 P104-100 的稳定版是 v1.6；v2.0 增加多卡与可扩展型号支持。
> **多卡逻辑未经实机验证**（开发者没有多卡环境）——请先 `-list` / `-dry-run` 确认识别正确，
> 再把输出反馈给作者。详见 [`docs/变更说明.md`](docs/变更说明.md)。

## 包内结构

```
P104Gen2x4-v2.0-beta/
├─ install.cmd            安装（部署 + 写驱动策略 + 注册计划任务）
├─ status.cmd             查状态（不需要管理员）
├─ src/
│  ├─ P104Gen2x4.cs       v2.0-beta 完整源码（单文件，带注释）
│  └─ 编译.cmd            用系统自带 csc.exe 重新编译
├─ bin/
│  ├─ P104Gen2x4.exe      编译好的程序（文件名与 v1.6 相同，兼容已部署任务/脚本）
│  ├─ uninstall.cmd       卸载
│  ├─ vbios.cmd           读 VBIOS 并算哈希（只读）
│  ├─ check-bw.cmd        带宽自测（需要 Python + torch）
│  ├─ bw_test.py
│  └─ drivers/
│     ├─ WinRing0x64.sys  读 PCI 配置空间
│     └─ ThrottleStop.sys 映射 BAR0
└─ docs/
   ├─ 使用说明.md / .txt
   ├─ 变更说明.md / .txt
   ├─ 测试计划.md / .txt
   └─ 单卡假设清单.md
```

## 快速开始

1. 解压**整个 zip**（drivers 必须一起，不能只拿 exe）。
2. 只读确认（不需要管理员）：
   ```
   bin\P104Gen2x4.exe -list
   bin\P104Gen2x4.exe -dry-run
   ```
3. 安装常驻（右键以管理员身份运行 `install.cmd`），或先单次解锁：
   ```
   bin\P104Gen2x4.exe -once
   ```
4. 看状态：`status.cmd`（不需要管理员）。

## 多卡纪律（代码保证）

- **一次只复位一张卡**：复位后等该卡声明锁存稳定（最多 20 秒），再处理下一张；
  两次复位起点间隔 ≥3 秒；某张卡失败记录后继续下一张。
- 按住循环里掉回 Gen1 **只补写声明，不主动复位**。
- 每张卡独立 try/catch：一张卡被拔掉/失败不影响其它卡。
- 拔插/换槽：每 30 秒按 BDF 重新枚举，不会死等旧 BDF。
- P104-101 / P106-100 / P106-090 标 **EXPERIMENTAL**：默认只读检测，
  写入必须显式 `-force-experimental`（配方未验证，风险自负）。
- 未支持的 NVIDIA 设备只会在 `-list` 显示，工具绝不写入。

## 文档

| 文件 | 内容 |
|---|---|
| [`docs/使用说明.md`](docs/使用说明.md) / `.txt` | 怎么装、怎么用、多卡须知、故障排查 |
| [`docs/变更说明.md`](docs/变更说明.md) / `.txt` | 与 v1.6 的差异、新参数、状态文件格式、验证状态与风险 |
| [`docs/测试计划.md`](docs/测试计划.md) / `.txt` | 哪些本机能验证、哪些必须等实机、步骤与验收标准 |
| [`docs/单卡假设清单.md`](docs/单卡假设清单.md) | 重构前的 32 处单卡假设（代码审查证据） |

## 风险提示

- 整卡复位会让画面黑一下（每卡默认 1 次；`-retry N` 会放大黑屏次数）。
- 临时加载 WinRing0 / ThrottleStop 两个 BYOVD 驱动；玩带反作弊的游戏前先卸载。
- 回退：管理员运行 `bin\uninstall.cmd` → 重启。v1.6 可直接覆盖 exe。
