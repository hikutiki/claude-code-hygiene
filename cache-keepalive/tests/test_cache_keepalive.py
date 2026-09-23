import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest


HOOK_PATH = Path(__file__).resolve().parents[1] / "cache-keepalive.py"


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def keepalive(tmp_path, monkeypatch):
    module = _load_module("cache_keepalive_test", HOOK_PATH)
    module.STATE_DIR = tmp_path / "state"
    monkeypatch.setenv("KEEPALIVE_IDLE_SEC", "0.08")
    monkeypatch.setenv("KEEPALIVE_MAX_SEC", "2")
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_CLAUDE_MDS", raising=False)
    return module


def _hook(tmp_path, session_id="session-test"):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    return {
        "session_id": session_id,
        "transcript_path": str(transcript),
        "hook_event_name": "Stop",
        "tool_name": "",
        "tool_input": {},
    }


def test_arm_terminates_previous_watcher(keepalive, tmp_path):
    hook = _hook(tmp_path)
    paths = keepalive._state_paths(hook["session_id"])
    paths["pid"].parent.mkdir(parents=True, exist_ok=True)
    previous = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", keepalive.__file__],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.2)
        paths["pid"].write_text(f"{previous.pid} {keepalive._proc_start(previous.pid)}\n", encoding="utf-8")
        os.utime(hook["transcript_path"], (time.time() - 1, time.time() - 1))
        result = keepalive.mode_arm(hook, stdout=io.StringIO())
        assert result == 2
        previous.wait(timeout=2)
        assert paths["pid"].read_text(encoding="utf-8").split()[0] == str(os.getpid())
    finally:
        if previous.poll() is None:
            previous.terminate()
            previous.wait(timeout=2)


def test_arm_does_not_kill_unrelated_reused_pid(keepalive, tmp_path):
    hook, other = _hook(tmp_path, session_id="reuse"), subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _write = keepalive._write_text(keepalive._state_paths("reuse")["pid"], str(other.pid) + "\n")
        os.utime(hook["transcript_path"], (time.time() - 1, time.time() - 1))
        keepalive.mode_arm(hook, stdout=io.StringIO()); assert other.poll() is None
    finally:
        other.terminate(); other.wait(timeout=2)
def test_arm_does_not_kill_keepalive_with_other_start_time(keepalive, tmp_path):
    hook, other = _hook(tmp_path, session_id="other"), subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", keepalive.__file__])
    try:
        keepalive._write_text(keepalive._state_paths("other")["pid"], f"{other.pid} Thu Jan  1 00:00:00 2001\n")
        os.utime(hook["transcript_path"], (time.time() - 1, time.time() - 1))
        keepalive.mode_arm(hook, stdout=io.StringIO()); assert other.poll() is None
    finally:
        other.terminate(); other.wait(timeout=2)
@pytest.mark.parametrize("skill", ["handoff", "session-wrap"])
def test_handoff_modes_create_stop_for_terminal_skills(keepalive, tmp_path, skill):
    hook = _hook(tmp_path, session_id="skill-" + skill)
    hook["tool_input"] = {"skill": skill}
    assert keepalive.mode_handoff(hook) == 0
    assert keepalive._state_paths(hook["session_id"])["stop"].exists()


def test_handoff_mode_ignores_other_skill(keepalive, tmp_path):
    hook = _hook(tmp_path, session_id="skill-other")
    hook["tool_input"] = {"skill": "resume"}
    assert keepalive.mode_handoff(hook) == 0
    assert not keepalive._state_paths(hook["session_id"])["stop"].exists()


def test_stop_flag_suppresses_rewake(keepalive, tmp_path):
    hook = _hook(tmp_path, session_id="stop-test")
    paths = keepalive._state_paths(hook["session_id"])
    paths["stop"].parent.mkdir(parents=True, exist_ok=True)
    paths["stop"].touch()
    os.utime(hook["transcript_path"], (time.time() - 1, time.time() - 1))
    output = io.StringIO()
    assert keepalive.mode_arm(hook, stdout=output) == 0
    assert output.getvalue() == ""


def test_global_disabled_suppresses_rewake(keepalive, tmp_path):
    hook = _hook(tmp_path, session_id="disabled-test")
    keepalive.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (keepalive.STATE_DIR / "DISABLED").touch()
    output = io.StringIO()
    assert keepalive.mode_arm(hook, stdout=output) == 0 and output.getvalue() == ""
    assert not keepalive._state_paths("disabled-test")["pid"].exists()  # 待機・PID記録より前に止まる


def test_max_age_suppresses_rewake(keepalive, tmp_path, monkeypatch):
    hook = _hook(tmp_path, session_id="max-test")
    paths = keepalive._state_paths(hook["session_id"])
    paths["last_user"].parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KEEPALIVE_MAX_SEC", "0.05")
    paths["last_user"].write_text("{:.6f}\n".format(time.time() - 1), encoding="utf-8")
    os.utime(hook["transcript_path"], (time.time() - 1, time.time() - 1))
    output = io.StringIO()
    assert keepalive.mode_arm(hook, stdout=output) == 0
    assert output.getvalue() == ""


def test_headless_marker_exits_without_state(keepalive, tmp_path, monkeypatch):
    hook = _hook(tmp_path, session_id="headless-test")
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_CLAUDE_MDS", "1")
    output = io.StringIO()
    assert keepalive.mode_arm(hook, stdout=output) == 0
    assert output.getvalue() == ""
    assert not keepalive.STATE_DIR.exists()


def test_mtime_update_restarts_idle_wait(keepalive, tmp_path, monkeypatch):
    hook = _hook(tmp_path, session_id="mtime-test")
    monkeypatch.setenv("KEEPALIVE_IDLE_SEC", "0.30")
    transcript = Path(hook["transcript_path"])
    now = time.time()
    os.utime(str(transcript), (now, now))

    def touch_later():
        time.sleep(0.15)
        transcript.touch()

    thread = threading.Thread(target=touch_later)
    thread.start()
    started = time.monotonic()
    output = io.StringIO()
    try:
        assert keepalive.mode_arm(hook, stdout=output) == 2
    finally:
        thread.join(timeout=2)
    elapsed = time.monotonic() - started
    assert elapsed >= 0.40
    assert output.getvalue() == "〔keepalive〕\n"


def test_idle_reaches_rewake_exit_and_output(keepalive, tmp_path):
    hook = _hook(tmp_path, session_id="idle-test")
    os.utime(hook["transcript_path"], (time.time() - 1, time.time() - 1))
    output = io.StringIO()
    assert keepalive.mode_arm(hook, stdout=output) == 2
    assert output.getvalue() == "〔keepalive〕\n"


def test_user_mode_records_last_user_time(keepalive, tmp_path):
    hook = _hook(tmp_path, session_id="user-test")
    before = time.time()
    assert keepalive.mode_user(hook) == 0
    recorded = float(
        keepalive._state_paths(hook["session_id"])["last_user"].read_text(encoding="utf-8")
    )
    assert before <= recorded <= time.time()


def test_main_converts_exceptions_to_exit_zero(keepalive):
    assert keepalive.main(["--mode", "arm"], stdin=io.StringIO("not-json"), stdout=io.StringIO()) == 0


