#!/usr/bin/env python3

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


STATE_DIR = Path(os.environ.get("KEEPALIVE_STATE_DIR", Path.home() / ".claude" / "cache-keepalive"))
DEFAULT_IDLE_SEC = 3300.0
DEFAULT_MAX_SEC = 43200.0
KEEPALIVE_TEXT = "〔keepalive〕"
STOP_SKILLS = ("handoff", "session-wrap")


def _seconds_from_env(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    value = float(raw)
    if value < 0:
        raise ValueError("negative interval")
    return value


def _session_id(hook):
    value = hook.get("session_id")
    if not isinstance(value, str) or not value:
        raise ValueError("missing session_id")
    if value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError("unsafe session_id")
    return value


def _state_paths(session_id):
    return {
        "pid": STATE_DIR / (session_id + ".pid"),
        "last_user": STATE_DIR / (session_id + ".last_user"),
        "stop": STATE_DIR / (session_id + ".stop"),
    }


def _write_text(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)


def _pid_is_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _proc_start(pid):
    return subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True).stdout.strip()


def _terminate_previous_pid(pid, start=""):
    if pid <= 1 or pid == os.getpid():
        return
    # PID 再利用対策: 記録した起動時刻と一致するプロセスだけを止める（記録が無ければ止めない）
    if not _pid_is_alive(pid) or not start or _proc_start(pid) != start:
        return
    # PID 再利用対策: 延命フック自身のプロセスだけを止める
    cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout.split()
    if os.path.realpath(__file__) not in (os.path.realpath(a) for a in cmd if a.endswith(".py")):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return


def _claim_pid(pid_path):
    pid_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with pid_path.open("a+", encoding="utf-8") as handle:
        os.chmod(pid_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read().strip()
        if raw:
            parts = raw.split(maxsplit=1)
            try:
                previous_pid = int(parts[0])
            except ValueError:
                previous_pid = 0
            _terminate_previous_pid(previous_pid, parts[1] if len(parts) > 1 else "")
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()} {_proc_start(os.getpid())}\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_last_user(path, fallback):
    if not path.exists():
        return fallback
    return float(path.read_text(encoding="utf-8").strip())


def mode_user(hook):
    session_id = _session_id(hook)
    paths = _state_paths(session_id)
    _write_text(paths["last_user"], "{:.6f}\n".format(time.time()))
    return 0


def mode_handoff(hook):
    tool_input = hook.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    skill = tool_input.get("skill")
    if skill not in STOP_SKILLS:
        return 0

    session_id = _session_id(hook)
    paths = _state_paths(session_id)
    paths["stop"].parent.mkdir(parents=True, exist_ok=True)
    paths["stop"].touch(exist_ok=True)
    return 0


def _transcript_stat(transcript_path):
    stat_result = os.stat(transcript_path)
    return stat_result.st_mtime_ns, stat_result.st_mtime


def _wait_until_idle(transcript_path, idle_sec):
    mtime_ns, mtime = _transcript_stat(transcript_path)
    while True:
        remaining = (mtime + idle_sec) - time.time()
        if remaining > 0:
            time.sleep(remaining)

        new_mtime_ns, new_mtime = _transcript_stat(transcript_path)
        if new_mtime_ns > mtime_ns:
            mtime_ns, mtime = new_mtime_ns, new_mtime
            continue
        return


def mode_arm(hook, stdout=None):
    if stdout is None:
        stdout = sys.stdout

    if os.environ.get("CLAUDE_CODE_DISABLE_CLAUDE_MDS") == "1" or (STATE_DIR / "DISABLED").exists():
        return 0

    session_id = _session_id(hook)
    transcript_path = hook.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        raise ValueError("missing transcript_path")

    watcher_started = time.time()
    paths = _state_paths(session_id)
    _claim_pid(paths["pid"])

    idle_sec = _seconds_from_env("KEEPALIVE_IDLE_SEC", DEFAULT_IDLE_SEC)
    max_sec = _seconds_from_env("KEEPALIVE_MAX_SEC", DEFAULT_MAX_SEC)
    _wait_until_idle(transcript_path, idle_sec)

    # 全セッション一括停止: 状態置き場に DISABLED を置く（消せば再開）
    if paths["stop"].exists() or (STATE_DIR / "DISABLED").exists():
        return 0

    last_user = _read_last_user(paths["last_user"], watcher_started)
    if time.time() - last_user >= max_sec:
        return 0

    stdout.write(KEEPALIVE_TEXT + "\n")
    stdout.flush()
    return 2


def _read_hook(stdin):
    payload = json.load(stdin)
    if not isinstance(payload, dict):
        raise ValueError("hook payload must be an object")
    return payload


def main(argv=None, stdin=None, stdout=None):
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("user", "handoff", "arm"))
    args = parser.parse_args(argv)

    try:
        hook = _read_hook(stdin)
        if args.mode == "user":
            return mode_user(hook)
        if args.mode == "handoff":
            return mode_handoff(hook)
        return mode_arm(hook, stdout=stdout)
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
