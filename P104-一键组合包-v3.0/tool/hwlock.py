#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
hwlock.py —— 硬件访问锁（多 agent 协作专用）

★★★ 唯一规则：任何对显卡的【写操作】或【复位操作】之前，必须先拿到锁。★★★

为什么需要它：
    显卡是唯一、共享、可被写坏的资源。
    两个人同时复位 = 显卡掉下 PCIe 总线（实测已发生 3 次，只能重启恢复）。
    两个人同时写寄存器 = 测量互相污染，结论全部作废。

用法（命令行）：
    python hwlock.py status                查看锁状态
    python hwlock.py acquire <你的名字> <用途> [分钟数=20]
    python hwlock.py release <你的名字>

用法（在脚本里）：
    import hwlock
    if not hwlock.acquire("verifier", "L 判决实验", minutes=20):
        raise SystemExit("拿不到硬件锁，退出")
    try:
        ... 你的硬件操作 ...
    finally:
        hwlock.release("verifier")
"""
import os
import sys
import json
import time
import datetime

_DIR = os.path.join(os.environ.get("ProgramData", os.path.expanduser("~")), "P104Unlock")
try:
    os.makedirs(_DIR, exist_ok=True)
except Exception:
    _DIR = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(_DIR, "HARDWARE.LOCK")
LOG = os.path.join(_DIR, "HARDWARE.LOCK.log")


def _now():
    return datetime.datetime.now()


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (_now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def status(verbose=True):
    """返回 (是否被占用, 锁信息dict或None)。过期的锁视为未占用。"""
    if not os.path.exists(LOCK):
        if verbose:
            print("  [锁] 空闲 —— 可以获取")
        return False, None
    try:
        with open(LOCK, "r", encoding="utf-8") as f:
            info = json.load(f)
    except Exception as e:
        if verbose:
            print("  [锁] 文件存在但读取失败(%s) —— 视为空闲" % e)
        return False, None
    try:
        exp = datetime.datetime.fromisoformat(info.get("expires", ""))
        if _now() > exp:
            if verbose:
                print("  [锁] 已过期（持有者=%s，过期于 %s）—— 可以抢占"
                      % (info.get("owner"), info.get("expires")))
            return False, info
    except Exception:
        pass
    if verbose:
        print("  [锁] ★ 被占用")
        print("       持有者 = %s" % info.get("owner"))
        print("       用途   = %s" % info.get("purpose"))
        print("       获取于 = %s" % info.get("acquired"))
        print("       过期于 = %s" % info.get("expires"))
        if info.get("notes"):
            print("       备注   = %s" % info.get("notes"))
    return True, info


def acquire(owner, purpose, minutes=20, notes=""):
    held, info = status(verbose=(owner != "_quiet"))
    if held:
        return False
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    data = {
        "owner": owner,
        "purpose": purpose,
        "acquired": _now().isoformat(timespec="seconds"),
        "expires": (_now() + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds"),
        "notes": notes,
        "pid": os.getpid(),
    }
    with open(LOCK, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _log("ACQUIRE by %s : %s (%d 分钟)" % (owner, purpose, minutes))
    print("  [锁] ✓ 已获取（%s，%d 分钟内有效）" % (owner, minutes))
    return True


def release(owner):
    if not os.path.exists(LOCK):
        print("  [锁] 本来就没有")
        return True
    try:
        with open(LOCK, "r", encoding="utf-8") as f:
            info = json.load(f)
    except Exception:
        info = {}
    if info.get("owner") not in (owner, None):
        print("  [锁] ⚠ 当前持有者是 %s，不是你（%s）—— 不释放"
              % (info.get("owner"), owner))
        return False
    os.remove(LOCK)
    _log("RELEASE by %s" % owner)
    print("  [锁] ✓ 已释放")
    return True


def renew(owner, minutes=20):
    if not os.path.exists(LOCK):
        return False
    with open(LOCK, "r", encoding="utf-8") as f:
        info = json.load(f)
    if info.get("owner") != owner:
        return False
    info["expires"] = (_now() + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with open(LOCK, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    _log("RENEW by %s (%d 分钟)" % (owner, minutes))
    print("  [锁] ✓ 已续期 %d 分钟" % minutes)
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1].lower()
    if cmd == "status":
        status()
    elif cmd == "acquire":
        if len(sys.argv) < 4:
            print("用法: python hwlock.py acquire <名字> <用途> [分钟数]")
            sys.exit(1)
        mins = int(sys.argv[4]) if len(sys.argv) > 4 else 20
        sys.exit(0 if acquire(sys.argv[2], sys.argv[3], mins) else 1)
    elif cmd == "release":
        if len(sys.argv) < 3:
            print("用法: python hwlock.py release <名字>")
            sys.exit(1)
        sys.exit(0 if release(sys.argv[2]) else 1)
    elif cmd == "renew":
        if len(sys.argv) < 3:
            print("用法: python hwlock.py renew <名字> [分钟数]")
            sys.exit(1)
        mins = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        sys.exit(0 if renew(sys.argv[2], mins) else 1)
    else:
        print(__doc__)
