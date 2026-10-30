# P104-100: PCIe Gen2 x4 unlock + x16 width declaration (Windows)

**English** | [中文](README.zh-CN.md)

> Windows tool for the NVIDIA **P104-100** (GP104 mining card, `10DE:1B87`):
> unlock the link from factory **Gen1 x4 (≈0.78 GB/s)** to **Gen2 x4 (≈1.57 GB/s measured)**,
> write the **x16 width declaration** (`LnkCap maxwidth = 16`), and keep it with a resident watchdog.
>
> **Since v3.2 this repo is fully open source**: executable tool + methodology + Python experiment toolchain.
> See [`docs/07-破解方法论与AI-Agent指南.md`](docs/07-破解方法论与AI-Agent指南.md) (methodology, Chinese).

## Current version: v3.2 (tool v1.1)

> ⚠️ **The v3.1 "2.0x16" Manual/Auto packages are deprecated — do not use them.**
> They could not latch reliably on many machines (the write group only covered the
> "card reset, then wait for the driver to rebind" path). Sorry for the trouble this caused.
> Use `P104-2.0x16-v1.1.zip` from v3.2 instead — it uses a live-write path and normally needs **no reset**.

## Measured results (2026-10-30, single card, CPU-attached x16 slot)

| Item | Result |
|---|---|
| Unlock path | **live write group, no card reset required** |
| Factory state → Gen2 | **0 resets, ~3 s** (two cold-state runs: 7.4 s / 2.8 s, both PASS) |
| PCIe link | **Gen2 x4** (`LNKSTA = 0x00001042`) |
| `LnkCap` declaration | maxspeed Gen2, **maxwidth = x16** (`0x00454D02`) |
| CUDA bandwidth (pinned) | **H2D 1.570 / D2H 1.673 GB/s** (Gen1 x4 reference: 0.78 / 0.85) |
| Resident keep-alive | 150 ms watchdog, writes only on deviation; 29 min continuous hold in earlier experiments |
| Autostart | scheduled task: boot +90 s / logon +120 s / retry every 5 min |
| Reset fallback | only if live path fails: max 1 per run (max 2 per boot cycle, ≥10 min apart) |

> ⚠️ **x16 is declaration-layer only.** `LnkCap maxwidth = 16` is really written, but the card has only
> 4 physical lanes; the actual link stays x4 and **bandwidth does not increase because of the x16 declaration**.
> A physical-x16 experiment (AC coupling capacitors) is planned for **2026-10-31 (Saturday)** and will be
> reported in this repo.

## Download & usage

Download `P104-2.0x16-v1.1.zip` from the repo root or **Releases**:

1. Extract the whole zip to a local disk (do not run inside the archive).
2. Right-click `一键解锁.cmd` → **Run as administrator**.
3. Wait for `SUCCESS`; then run `带宽测试.cmd` (H2D should be ≥1.4 GB/s).
4. Check status any time: `状态.cmd` (read-only, no admin needed).
5. Stop: `停止按住.cmd`; uninstall: `卸载.cmd` (keeps the driver policy keys by design).
6. No autostart? Use `手动解锁-不装自启.cmd` (keep the window open; closing it stops the hold).

All `.cmd` files are pure ASCII + CRLF (the v1.1 encoding fix); the package README has the details.

## How it works (v1.1)

1. Map GPU BAR0 via BYOVD drivers (WinRing0 / ThrottleStop);
2. Write the **live declaration group** (we measured it flips the PCIe declaration in real time):
   `0x8872C = 6` → `0x2241C |= 0x81` → `0x8841C &= ~0x6000` → `0x8C2C0 &= ~4`
   → `0x8C300 &= ~0x10` → `0x8C040 SPEED=1` → `0x8C1C0[19:16]=2` → `0x88610 |= 1`
   (the Gen2 declaration bit appears in `LnkCap2` within 1 second; no reset);
3. Set `LNKCTL2 = 2` (5.0 GT/s) on both ends and retrain at the root port → **Gen2 x4**;
4. Write the **x16 width declaration**: `0x8C040 = (cur & ~0xF8000000) | 0x80000000`
   (safe recipe; rollback always writes the whole word `0x20085800`; single-bit clearing of bit31 was
   measured to drop the card);
5. 150 ms watchdog re-applies only when the four criteria deviate (NVIDIA driver resets `0x8C040.SPEED`
   on every CUDA init);
6. Only if the live path fails repeatedly: one card reset as fallback, then re-apply the live group.

## Open source: tool + methodology + toolchain

| Path | Content |
|---|---|
| `P104-2.0x16-v1.1/` | Windows one-click tool (pure ASCII cmd) |
| [`tools/`](tools/README.md) | Python experiment toolchain: read-only probe, live write group, width declaration, hardware lock |
| [`docs/07-破解方法论与AI-Agent指南.md`](docs/07-破解方法论与AI-Agent指南.md) | Full methodology (four-layer model, standard workflow, safety rules, porting to other mining cards) |
| [`docs/06-v1.1-更新说明.md`](docs/06-v1.1-更新说明.md) | v1.0 → v1.1 technical notes and measurements |
| [`docs/05-功能边界总表.md`](docs/05-功能边界总表.md) | Capability boundary: what can and cannot be unlocked |

> **Hand it to an AI agent.** If you own a different mining card (P106 / CMP series, etc.), you can give
> this repo's README, methodology and `tools/` to an AI agent (Claude Code / Codex / DSH, etc.) and let it
> follow the same workflow — **read-only probe → write-channel control → single-variable search for the
> declaration switch → speed/width → resident hold → bandwidth verification** — to analyze and adapt the
> approach to hardware you own. The tool is open source, and so is the methodology.

## Compatibility & safety

- Windows 10 / 11 x64 + **administrator**; P104-100 only (`10DE:1B87`);
- Validated on a CPU-attached x16 slot (PCH slots not tested; community reports they may latch with BAR0 writes only);
- BYOVD drivers may trigger AV/anti-cheat alerts — expected;
- **Volatile register writes only**: no VBIOS flashing, no fuse burning; power cycle restores everything;
- Use on hardware you own only;
- If link training fails badly the card can disappear from the bus: **full AC power-off for 30 s** recovers it;
  the tool stops and tells you instead of retrying in a loop.

## Credits

- **WJNKAC**'s Linux analysis (`LnkCap2` bit semantics, `LnkCtl2` clamping, CUDA-init down-speed, etc.);
- All boundary-experiment records in `docs/05-功能边界总表.md`.

## Disclaimer

Research and personal-hardware use only. Only volatile registers are written; no VBIOS/fuse-level persistent
modifications. Use at your own risk.

## Versions

This README describes the current version; see **Releases** and `RELEASE_NOTES_v*.md` for history.
