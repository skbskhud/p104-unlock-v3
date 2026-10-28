# P104-100 Unlock & Feature Boundaries (v3.0)

**English** | [中文](README.zh-CN.md)

> Measured facts for the NVIDIA **P104-100** (GP104 mining card, `10DE:1B87`):
> what can be unlocked, where each limit lives, and the evidence.

## 30-second summary

| Capability | Result |
|---|---|
| **PCIe Gen2 x4** | ✅ unlockable (0.78 → 1.55 GB/s measured) |
| **Declared width x4→x16** | ✅ host-writable (`LnkCap` max width becomes 16); ⚠️ actual link/bandwidth UNVERIFIED (needs capacitors) |
| **PCIe Gen3** | ❌ fuse-locked (`GEN3_DIS=1`) |
| **NVENC / NVDEC / OFA** | ❌ fuse-locked (`NVDEC_DISABLE=1` / `NVENC_DISABLE=3` / `OFA_DISABLE=1`) |

Full evidence & coverage boundaries → [`docs/05-功能边界总表.md`](docs/05-功能边界总表.md) (Chinese)

## Download

- **Latest v3.0**: see **Releases** → `P104-v3.0-standalone-toolkit.zip`
  (standalone: bundled Python runtime + drivers, no Python install needed, admin required)
- **Stable Gen2 only**: use **v1.6** (older repo `skbskhud/p104-gen2-unlock`, or historical releases here)

## Usage (3 steps)

```
1. Extract → run 一键解锁.cmd (self-elevates)
2. 一键验证.cmd → declared Gen2 / declared x16 / actual link
3. 一键恢复.cmd → stop task + restore registers + remove drivers
```

- watchdog re-asserts every 100ms; auto-start at logon;
- drivers are BYOVD (WinRing0/ThrottleStop) — AV alerts are expected;
- logs/state: `%ProgramData%\P104Unlock\`.

## Disclaimer

- **Width x16 is declaration-layer only.** We do NOT claim bandwidth gains; link width after
  soldering capacitors is unverified.
- **Gen3 and the media engines are OTP-fuse locked.** No host-side path exists.
- All writes are volatile registers; power cycle restores everything. For research on hardware you own.

## Versions

This README describes the **current version** only; historical versions live in **Releases**.
