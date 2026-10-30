# P104-100: PCIe Gen2 x4 unlock + x16 width declaration (Windows)

**English** | [中文](README.zh-CN.md)

> Windows tool for the NVIDIA **P104-100** (GP104 mining card, `10DE:1B87`):
> unlocks the link from factory **Gen1 x4 (0.78 GB/s)** to **Gen2 x4 (1.55 GB/s measured)**,
> sets the **x16 width declaration** (`LnkCap maxwidth = 16`), and keeps it with a resident watchdog.

## Measured results

| Item | Result |
|---|---|
| **PCIe Gen2 x4** | ✅ unlocked, measured **0.78 → 1.55 GB/s** (pinned H2D / D2H ≈ 1.55 / 1.67) |
| **x16 width declaration** | ✅ written (`LnkCap maxwidth = 16`, visible in GPU-Z / lspci) |
| **Actual link width** | still **x4** (only 4 physical lanes). With the AC coupling caps added it is expected to train to x16 - not tested yet, will update |
| **Gen3 / NVENC / NVDEC** | ❌ fuse-locked, see [`docs/05-功能边界总表.md`](docs/05-功能边界总表.md) |

> In one line: **the bandwidth gain comes from Gen2; x16 is declaration-layer only.**

## Verification status

- ✅ Verified (4.5h continuous): Gen2 x4 under the resident holder, 1.55 GB/s measured; width declaration written (`LnkCap maxwidth=16`)
- ⏳ Pending: clean-boot autostart test (v1.6 flow, expected 2-4 min after boot)
- ⏳ Pending: physical x16 after adding AC coupling caps (expected to work, will update)

## Two builds (pick one, do not run both)

**⭐ Manual is the recommended build**: most controllable, confirm it works before using the auto one.

| Build | Feature | Asset |
|---|---|---|
| **⭐ Manual (recommended)** | **no task / no autostart**; run `手动解锁.cmd` and keep the window open; close it to stop | `P104-2.0x16-Manual-v1.0.zip` |
| Auto (alternative) | registers a SYSTEM task (boot +60s / logon +15s / retry every minute). **The first card reset after a cold boot often fails to latch**, so it relies on the per-minute retry; usually 2-4 min, sometimes longer | `P104-2.0x16-Auto-v1.0.zip` |

> Auto build caveat: the first latch is flaky, expect a few minutes. **Need it now / want to confirm -> Manual**; want it ready right after boot -> Auto.

Both do the same thing (Gen2 x4 + x16 declaration); only autostart differs.

## Quick start

### Auto build
```
1) extract -> right-click install.cmd -> Run as administrator
2) reboot
3) wait 2-4 minutes after boot (the task retries automatically)
4) run status.cmd (expect "Gen2 x4 [declared width x16]")
```

### Manual build
```
1) extract -> right-click 手动解锁.cmd -> Run as administrator, keep the window open
2) check: status.cmd (status + bandwidth)
3) stop: close the window, or run 停止按住.cmd
```

## How it works

1. Map GPU BAR0 via BYOVD drivers (WinRing0 / ThrottleStop);
2. Write enable bits + speed request (`0x8841C / 0x88610 / 0x8C040` ...) so the endpoint requests 5.0GT/s;
3. Do a **real device reset (disable -> enable)** so the PCIe core re-latches the declaration as Gen2;
4. Set `LNKCTL2 = 2` on both ends and retrain;
5. A resident watchdog rewrites every 20ms (the NVIDIA driver resets `0x8C040.SPEED` to Gen1 on every CUDA init);
6. Once Gen2 is up, write `0x8C040 bit31` (**x16 width declaration**, safe recipe: clear bits27-31 first, then set bit31).

## CPU-attached vs PCH slot (why a reset is needed)

- **CPU-attached x16 slot (our test rig)**: writing BAR0 enable bits alone does **not** latch the
  declaration (`LCAP2` stays `0x02`); a real device reset is required. Verified: no-reset = always fails;
  after reset the declaration latches within 4 seconds.
- **PCH chipset slots**: per Linux community feedback (WJNKAC's Linux analysis) and our older experience,
  a plain BAR0 write may latch the declaration immediately (unverified by us).
- This tool always uses the **verified reset path**; the no-reset path is not implemented.

## Important notes

- **x16 is declaration-layer**: GPU-Z showing `maxwidth x16` is real (the register changed), but the actual
  link stays x4 and bandwidth is unchanged; real x16 after soldering caps is unverified.
- **GPU-Z may show "1.1"**: the NVIDIA driver-reported value is unreliable on this mining card;
  trust `status.cmd` RESULT and `bwtest.cmd` measured bandwidth.
- BYOVD drivers (WinRing0/ThrottleStop) — AV alerts are expected; do not run other P104 tools simultaneously.
- Worst case the link training fails and the card drops off the bus: full power cycle recovers it
  (volatile registers only, no hardware damage).

## Compatibility

- Windows 10 / 11 x64 + admin; P104-100 only (`10DE:1B87`); no Python needed (.NET Framework 4.x built-in);
- validated on a **CPU-attached PCIe x16 slot**; x1 mining risers only give x1 bandwidth.

## Uninstall

Auto: right-click `uninstall.cmd` -> Run as administrator.
Manual: just close the hold window (no task/driver residue).

## Credits

- **WJNKAC**'s Linux project [nvidia-p104-unlock-gen2-for-linux](https://github.com/WJNKAC/nvidia-p104-unlock-gen2-for-linux):
  pointed out `LnkCap2` bit semantics, `LnkCtl2` clamping, and CUDA-init induced link downshift;
- the v1.6 Gen2 unlock tool and all boundary-experiment records behind it.

## Disclaimer

For research on hardware you own only. Volatile registers only - no VBIOS flashing, no fuse burning;
power cycle restores everything. Use at your own risk.

## Versions

This README describes the current version only; history lives in **Releases**.