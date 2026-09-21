#!/usr/bin/env python3
"""Claude Code hooks 自己検査ツール。

.claude/settings.local.json に登録された全hookを検査し、
起動可能性（CHECK-A）、実行正常性（CHECK-B）、SessionStart内容（CHECK-C）を検証する。
副作用を持たず、.claude/ 配下は一切変更しない。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path



def _git_toplevel() -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else Path.cwd()


REPO = _git_toplevel()


def _default_settings(repo: Path) -> Path:
    for name in ("settings.local.json", "settings.json"):
        p = repo / ".claude" / name
        if p.is_file():
            return p
    return repo / ".claude" / "settings.json"


DEFAULT_SETTINGS = _default_settings(REPO)

INTERPRETERS = {
    "python",
    "python3",
    "bash",
    "sh",
    "zsh",
    "node",
    "ruby",
    "perl",
}


def parse_hooks(settings_path: Path) -> list[dict]:
    if not settings_path.is_file():
        raise FileNotFoundError(f"Settings file not found: {settings_path}")

    with settings_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    hooks_section = data.get("hooks", {})
    if not isinstance(hooks_section, dict):
        return []

    entries = []
    for event_name, group_list in hooks_section.items():
        if not isinstance(group_list, list):
            continue
        for group in group_list:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher")
            sub_hooks = group.get("hooks")
            if isinstance(sub_hooks, list):
                for h in sub_hooks:
                    if isinstance(h, dict) and "command" in h:
                        entries.append({
                            "event": event_name,
                            "matcher": matcher,
                            "command": h["command"],
                        })
            elif "command" in group:
                entries.append({
                    "event": event_name,
                    "matcher": matcher,
                    "command": group["command"],
                })
    return entries


def check_hook(entry: dict) -> tuple[bool, str]:
    event = entry["event"]
    cmd_str = entry["command"]

    try:
        tokens = shlex.split(cmd_str)
    except Exception as exc:
        return False, f"failed to parse command: {exc}"

    if not tokens:
        return False, "empty command"

    # CHECK-A: 起動可能性
    cmd0 = tokens[0]
    base0 = os.path.basename(cmd0)

    check_tokens = tokens
    if base0 == "env" and len(tokens) > 1:
        idx = 1
        while idx < len(tokens) and tokens[idx].startswith("-"):
            idx += 1
        if idx < len(tokens):
            cmd0 = tokens[idx]
            base0 = os.path.basename(cmd0)
            check_tokens = tokens[idx:]

    is_interpreter = (
        base0 in INTERPRETERS
        or bool(re.match(r"^(python\d+(\.\d+)?)$", base0))
    )

    if is_interpreter:
        script_path = None
        for arg in check_tokens[1:]:
            if not arg.startswith("-"):
                script_path = arg
                break
        if script_path:
            p = Path(script_path).expanduser()
            if not p.is_file():
                return False, f"CHECK-A FAIL: script path not found: {script_path}"
    else:
        p = Path(cmd0).expanduser()
        if not p.is_file():
            return False, f"CHECK-A FAIL: file not found: {cmd0}"
        if not os.access(p, os.X_OK):
            return False, f"CHECK-A FAIL: missing executable bit (+x): {cmd0}"

    # CHECK-B: 実行
    if event == "PreToolUse":
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo selftest"}})
    else:
        payload = "{}"

    try:
        proc = subprocess.run(
            tokens,
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(REPO),
        )
    except subprocess.TimeoutExpired:
        return False, "CHECK-B FAIL: timeout (10s)"
    except Exception as exc:
        return False, f"CHECK-B FAIL: execution error: {exc}"

    if proc.returncode != 0:
        err_msg = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:200]
        return False, f"CHECK-B FAIL: exit code {proc.returncode} ({err_msg})"

    # CHECK-C: SessionStartの内容
    if event == "SessionStart":
        try:
            out_data = json.loads(proc.stdout)
        except Exception as exc:
            snip = proc.stdout.strip().replace("\n", " ")[:200]
            return False, f"CHECK-C FAIL: SessionStart output is not valid JSON ({snip}): {exc}"
        if not isinstance(out_data, dict):
            return False, "CHECK-C FAIL: SessionStart output is not a JSON object"
        hso = out_data.get("hookSpecificOutput")
        if not isinstance(hso, dict):
            return False, "CHECK-C FAIL: missing hookSpecificOutput in JSON"
        ctx = hso.get("additionalContext")
        if not isinstance(ctx, str) or "CLAUDE_START" not in ctx:
            return False, "CHECK-C FAIL: hookSpecificOutput.additionalContext does not contain 'CLAUDE_START'"

    return True, "OK"


def run_selftest(settings_path: Path) -> int:
    try:
        hooks = parse_hooks(settings_path)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not hooks:
        print(f"WARN: no hooks found in {settings_path}")
        return 0

    ok_count = 0
    fail_count = 0

    for h in hooks:
        event = h["event"]
        matcher = h["matcher"]
        cmd = h["command"]
        label = f"[{event}:{matcher}]" if matcher else f"[{event}]"

        ok, msg = check_hook(h)
        if ok:
            print(f"{label} {cmd} -> OK")
            ok_count += 1
        else:
            print(f"{label} {cmd} -> FAIL: {msg}")
            fail_count += 1

    total = ok_count + fail_count
    print(f"Hooks self-test: total={total}, OK={ok_count}, FAIL={fail_count}")
    return 0 if fail_count == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude Code hooks self-test")
    parser.add_argument("--repo", type=Path, help="Repository root used as cwd for hooks (default: git toplevel of cwd)")
    parser.add_argument(
        "--settings",
        type=Path,
        help="Path to settings JSON file (default: <repo>/.claude/settings.local.json, else settings.json)",
    )
    args = parser.parse_args()
    global REPO
    if args.repo:
        REPO = args.repo.resolve()
    settings = args.settings or _default_settings(REPO)
    return run_selftest(settings)


if __name__ == "__main__":
    sys.exit(main())
