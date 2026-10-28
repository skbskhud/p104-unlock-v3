# P104-100 Feature Boundary Report + All-in-One Toolkit v3.0

**English** | [中文](README.zh-CN.md)

> Measured feature boundaries of the NVIDIA **P104-100** (GP104 mining card, `10DE:1B87`):
> what can actually be unlocked from the host, where each limit lives, and the evidence.
> Also ships the **v3.0 all-in-one toolkit** (Gen2 x4 + width-declaration experiment).

---

## Summary

| Capability | Result | Layer |
|---|---|---|
| **PCIe Gen2 x4** | ✅ **Unlockable** (0.78 → 1.55 GB/s measured) | register layer |
| **Declared link width x4→x16** | ✅ host-writable (`LnkCap` max width becomes 16 live); ⚠️ **actual link width UNVERIFIED**, no bandwidth gain | register + physical (unverified) |
| **PCIe Gen3** | ❌ **Not unlockable** (OTP fuse `GEN3_DIS=1`, 6 independent evidence lines) | fuse layer |
| **NVENC / NVDEC / OFA** | ❌ **Not unlockable** (OTP fuses `NVDEC_DISABLE=1` / `NVENC_DISABLE=3` / `OFA_DISABLE=1`) | fuse layer |

> In one sentence: **everything solvable at the register layer is solved; the firmware layer has no host entry; the fuse layer (Gen3, media engines) is the real wall.**

Full evidence, methodology and coverage boundaries: **[`docs/05-功能边界总表.md`](docs/05-功能边界总表.md)** (Chinese).

---

## Contents

```
docs/05-功能边界总表.md      boundary report (four-layer model / evidence / fuse verdicts)
P104-一键组合包-v3.0/        all-in-one toolkit (Gen2 x4 + width declaration experiment)
  一键解锁.cmd               self-elevating installer + apply now
  一键验证.cmd               status + optional retrain
  一键恢复.cmd               stop task + restore registers + remove drivers
  tool/                      toolkit sources (Python)
  Gen2-2.0/                  legacy multi-card Gen2 tool (alternative; do not run both)
```

**Note**: the v3.0 package is currently a **workstation build** (depends on this project's
Python runtime and driver paths). A standalone distributable will be prepared before public release.
For "stable Gen2 only", use the older repository: `skbskhud/p104-gen2-unlock` → Release **v1.6**.

---

## Disclaimer

- **Width x16 is declaration-layer only.** We have NOT verified that the link actually trains
  to x16 after soldering AC-coupling capacitors. No bandwidth gain is claimed.
- **Gen3 and the media engines are fuse-locked (OTP).** No host-side (register/driver/VBIOS/EFI)
  path exists; only physical fuse attacks remain, which are out of scope and not recommended.
- All writes are **volatile registers**: no VBIOS flashing, no fuse burning; power cycle restores.
- For research on hardware you own only.

---

*This repository is a private preview; content will be published/merged once verified.*
