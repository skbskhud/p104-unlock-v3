#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""hwlock.py —— 硬件访问锁（多 agent / 多进程协作专用）

================================================================================
★ 唯一规则：任何对显卡的【写操作】或【复位操作】之前，必须先拿到锁。
★ 为什么：显卡是唯一、共享、可被写坏的资源。
    两个进程同时复位 = 显卡掉下 PCIe 总线（实测发生过，只能断电重启）。
    两个进程同时写寄存器 = 测量互相污染，结论全部作废。
★ 锁文件默认放在 %ProgramData%\\P104Unlock\\（不污染 git 仓库）；
    可用环境变量 P104_LOCK_DIR 覆盖。
================================================================================

用法（命令行）：
    python hwlock.py status                      查看锁状态
    python hwlock.py acquire <你的名字> <用途> [分钟数=20]
    python hwlock.py release <你的名字>
    python hwlock.py renew   <你的名字> [分钟数]

用法（在脚本里）：
    import hwlock
    if not hwlock.acquire("my-agent", "Gen2 实验", minutes=20):
        raise SystemExit("拿不到硬件锁，退出")
    try:
        ... 你的硬件操作 ...
    finally:
        hwlock.release("my-agent")
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    """锁文件目录（★ 不做 makedirs：import 本模块不应有任何文件系统副作用）。"""
    d = os.environ.get("P104_LOCK_DIR")
    if d:
        return d
    pd = os.environ.get("ProgramData")
    if pd:
        return os.path.join(pd, "P104Unlock")
    return HERE


DATA_DIR = _data_dir()
LOCK = os.path.join(DATA_DIR, "HARDWARE.LOCK")
LOG = os.path.join(DATA_DIR, "HARDWARE.LOCK.log")


def _now():
    return datetime.datetime.now()


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (_now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def read_lock():
    """读锁文件内容；文件不存在或损坏返回 None。"""
    try:
        with open(LOCK, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def status(verbose=True):
    """返回 (是否被占用, 锁信息 dict 或 None)。过期的锁视为未占用。"""
    if not os.path.exists(LOCK):
        if verbose:
            print("  [锁] 空闲 —— 可以获取")
        return False, None
    info = read_lock()
    if info is None:
        if verbose:
            print("  [锁] 文件存在但读取失败 —— 视为空闲")
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


def acquire(owner, purpose, minutes=20, notes="", force=False):
    """拿锁。force=True 时即使别人持有也抢占（★ 只在确认对方已死时用）。"""
    held, info = status(verbose=(owner != "_quiet"))
    if held and not force:
        return False
    if held and force:
        print("  [锁] ！ --force 抢占：原持有者 = %s" % (info or {}).get("owner"))
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
    print("  [锁] √ 已获取（%s，%d 分钟内有效）" % (owner, minutes))
    return True


def release(owner):
    if not os.path.exists(LOCK):
        print("  [锁] 本来就没有")
        return True
    info = read_lock() or {}
    if info.get("owner") not in (owner, None):
        print("  [锁] ！ 当前持有者是 %s，不是你（%s）—— 不释放"
              % (info.get("owner"), owner))
        return False
    try:
        os.remove(LOCK)
    except Exception:
        return False
    _log("RELEASE by %s" % owner)
    print("  [锁] √ 已释放")
    return True


def renew(owner, minutes=20):
    if not os.path.exists(LOCK):
        return False
    info = read_lock()
    if not info or info.get("owner") != owner:
        return False
    info["expires"] = (_now() + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with open(LOCK, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    _log("RENEW by %s (%d 分钟)" % (owner, minutes))
    print("  [锁] √ 已续期 %d 分钟" % minutes)
    return True


def _pid_alive(pid):
    """跨平台判断 pid 是否还活着（Windows 用 OpenProcess，其它用 os.kill(0)）。"""
    try:
        pid = int(pid)
    except Exception:
        return False
    if os.name == "nt":
        import ctypes as C
        k = C.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = C.c_void_p
        h = k.OpenProcess(0x1000, False, pid)
        if h:
            k.CloseHandle(C.c_void_p(h))
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def clean_stale(owner=None):
    """清理"持有进程已死"的残留锁（例如重启/崩溃后）。返回 True 表示清掉了。"""
    info = read_lock()
    if not info:
        return False
    if owner is not None and info.get("owner") != owner:
        return False
    pid = info.get("pid")
    if pid and _pid_alive(pid):
        return False
    try:
        os.remove(LOCK)
    except Exception:
        return False
    _log("CLEAN STALE by %s (owner=%s pid=%s)" % (sys.argv[0], info.get("owner"), pid))
    print("  [锁] 已清理残留锁（owner=%s pid=%s 进程不存在）" % (info.get("owner"), pid))
    return True


if __name__ == "__main__":
    for _st in (sys.stdout, sys.stderr):          # 管道/重定向时用 UTF-8，避免中文乱码
        try:
            if _st is not None and hasattr(_st, "reconfigure") and not _st.isatty():
                _st.reconfigure(encoding="utf-8")
        except Exception:
            pass
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
    elif cmd == "clean":
        clean_stale(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print(__doc__)
