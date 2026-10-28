# P104-100 一键组合包 v3.0（Gen2 x4 + 宽度声明实验）

> **独立可运行**：自带 Python 运行时，不需要安装 Python；解压即用（需管理员权限）。
> 只写**易失寄存器**：不刷 VBIOS、不烧熔丝，断电即恢复。

## 这是什么

把 P104-100 的两件事做成一次点击：

1. **Gen2 x4（默认，已验证）**：0.78 → 1.55 GB/s，无需整卡复位；
2. **宽度声明 x16（实验项）**：把端点 `LnkCap` 的最大宽度从 x4 改成 x16。
   ⚠️ **只改"声明"**：实际链路宽度仍是 x4（`LNKSTA`），带宽**没有变化**；
   需要补 AC 耦合电容后由物理层决定能否真的训练到 x16（未验证）。

## 使用

```
1) 解压到任意可写目录
2) 双击 一键解锁.cmd（自动请求管理员）→ 装驱动 + 注册 SYSTEM 登录任务 + 立即施加
3) 一键验证.cmd 看状态（可顺手 retrain）
4) 不想要了：一键恢复.cmd（停任务 + 寄存器回出厂 + 删驱动服务）
```

- 保持进程每 100ms 复查补写，登录自动启动；
- 驱动为 BYOVD（WinRing0/ThrottleStop），安全软件/反作弊会报警，属预期；
- 日志/状态：`%ProgramData%\P104Unlock\`（`width16_hold.log` / `width16_state.json`）。

## 要求

- Windows 10/11 x64，管理员权限；
- NVIDIA P104-100（`10DE:1B87`）；换卡/换平台结论不适用；
- 仅供研究自己拥有的硬件。

## 文件

```
python\              自带 Python 3.12 运行时（embeddable）
tool\                width16_hold.py / lib_p104.py / hwlock.py / read_regs.py
drivers\             WinRing0x64.sys / ThrottleStop.sys
一键解锁.cmd / 一键验证.cmd / 一键恢复.cmd
```

## 与旧版

- 只想稳定解锁 Gen2：可用旧仓库 `p104-gen2-unlock` 的 **v1.6**（C# 自包含 exe）。
- 本包 = Gen2 + 宽度声明实验，二选一，不要和其它 P104 工具同时跑。
