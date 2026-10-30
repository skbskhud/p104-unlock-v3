# 源码：P104Gen2x4.cs（工具 v1.1）

这是发布包 `P104-2.0x16-v1.1.zip` 里 `bin\P104Gen2x4.exe` 的完整 C# 源码（单文件，无外部依赖）。

## 编译

需要 Windows 自带的 .NET Framework 4.x 编译器（`csc.exe`，Win10/11 自带，无需安装 SDK）：

```powershell
# 在仓库根目录执行
$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $csc /nologo /optimize+ /target:exe /platform:x64 /out:bin\P104Gen2x4.exe src\P104Gen2x4.cs
```

编译产物放到 `P104-2.0x16-v1.1\bin\`（与 `drivers\` 同级）即可运行。
由于 csc 会写入编译时间戳，**每次编译的 SHA256 不会相同**；发布包内 exe 的 SHA256 见包内 `SHA256.txt`。

## 程序结构（阅读顺序）

| 区域 | 内容 |
|---|---|
| 文件头注释 | v1.1 完整机理与安全规则（强烈建议先读） |
| `WriteEnables` / `ApplyLiveGroup` | 复位前置写组 / live 声明写组（8 写） |
| `WidthArm` / `DisarmWidthForSpeed` | 宽度声明安全配方、armed+Gen1 时先回退再提速 |
| `WriteTlsBoth` / `RetrainBoth` | 两端 LNKCTL2 目标速率、根端口 retrain |
| `PnpDisable` / `PnpEnable` / `WaitBack` / `DoCardReset` | 整卡复位兜底（预算见 `CanResetNow`） |
| `UnlockOnce` | 状态机：live 快路径 → 复位兜底 → TLS/retrain → 宽度 → 自检 |
| `HoldLoop` | 150 ms 看门狗（只补偏离、触发类写限流） |
| `DoCheck` / `DoDiag` / `DoBw` | 只读状态 / 寄存器诊断 / CUDA 带宽 |
| `DoInstall` / `DoUninstall` / `RegisterTask` | 安装、卸载、计划任务 |

## 安全边界

- 只写**易失寄存器**：不刷 VBIOS、不烧熔丝、不改持久化配置，断电即恢复；
- `0x8C040` 的写值必须过 `Is8C040Legal`（bit31=0 且 bit29=0 / bit31=1 且 bit29=1 都会掉卡）；
- 掉卡（设备从总线消失）只能 AC 断电 30 秒恢复，工具会停手并提示；
- 只对你自己拥有的硬件使用。

配套的 Python 实验工具链见 [`../tools/`](../tools/)，方法论见
[`../docs/07-破解方法论与AI-Agent指南.md`](../docs/07-破解方法论与AI-Agent指南.md)。
