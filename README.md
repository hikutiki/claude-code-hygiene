# claude-code-hygiene — four tools that mechanically check Claude Code docs, memory, and hooks

[日本語](README.ja.md)

The longer you use Claude Code, the more the references between CLAUDE.md, skills, hooks, and auto memory drift apart.
These small tools find "memory pointing to paths that no longer exist", "hooks that are registered but cannot start", and "references to files you already retired" — without calling a model (zero tokens).

| tool | what it finds | exit |
|---|---|---|
| `memcheck.py` | Stale memory: references to missing paths, mismatch between the MEMORY.md index and the actual files, lines that still cite retired terms as current, `[[wiki links]]` with no target (informational) | 0 = clean / 1 = found |
| `hooks-selftest.py` | Starts every hook in settings.json and checks it: can it launch, does it run cleanly, what does SessionStart output | 0 = all OK / 1 = some FAIL |
| `refscan.sh` | Searches everywhere for the name of a retired or moved file and splits the hits into "live" and "records / evidence" | 0 = live hits / 1 = no live hits / 2 = no argument |
| `reachmap.py` | Follows references from what Claude Code reads automatically (CLAUDE.md, hooks, skills, memory) and tells whether a document is reachable. Also inventories orphans, broken and ambiguous references | 0 = OK / 2 = error |

Requirements: Python 3.9+ standard library, zsh, git, grep.

## memcheck.py

```bash
python3 memcheck.py                      # check the memory that belongs to the git root of the cwd
python3 memcheck.py --repo /path/to/repo # base for resolving relative paths
python3 memcheck.py --memory-dir ~/.claude/projects/-path-to-repo/memory
```

The memory location is derived from Claude Code's rule (`~/.claude/projects/<cwd with / replaced by ->/memory`).
Put a `.memcheck.json` at the repository root to list retired terms and disclaimer words (see `.memcheck.example.json`).

Checks:
1. Do paths written in backticks exist (`~`, absolute, and repo-relative are resolved)
2. Does the target of each `[[link]]` exist (missing targets are shown as "to be written", not as errors)
3. Two-way match between the MEMORY.md index and the actual files
4. Lines that write a retired term without a disclaimer such as "retired" or "old"

## hooks-selftest.py

```bash
python3 hooks-selftest.py                       # <repo>/.claude/settings.local.json (or settings.json)
python3 hooks-selftest.py --settings path.json  # explicit file
```

For every registered hook it checks command resolution (CHECK-A), a run with dummy input (CHECK-B), and the output of SessionStart hooks (CHECK-C). No side effects; `.claude/` is not modified.

## refscan.sh

```bash
/bin/zsh refscan.sh <target path> [output base]   # search terms (hyphen and underscore variants) come from the basename
/bin/zsh refscan.sh --text <string> [output base]
```

Searches the whole repository plus `~/.claude`, LaunchAgents, shell config, and crontab. Writes `<base>-all.txt` (every hit) and `<base>-live.txt` (live hits) and prints only the counts and the live list. Falls back to stdout if it cannot write.

A `.refscan.conf` at the repository root adds search locations, per-location excludes, and path prefixes to treat as "records / evidence" (see `.refscan.example.conf`). The verdict uses the live count, not the total, because logs can record the command line of the search itself and match it.

**Known limit:** search terms come from the basename, so choosing identifiers is still up to you. Run it once per identifier of whatever you retire.

## reachmap.py

```bash
python3 reachmap.py docs/some-guide.md   # can this document be reached from an entry point?
python3 reachmap.py --report             # inventory of orphans, broken / ambiguous references, duplicate names (also JSON)
python3 reachmap.py --no-home            # do not treat ~/.claude etc. as entry points
```

Entry points:
- `CLAUDE.md` and `CLAUDE.local.md` at the root, `~/.claude/CLAUDE.md`
- `.md` files loaded by hooks (SessionStart, UserPromptSubmit) in `.claude/settings.json` / `settings.local.json`
- `.claude/agents/*.md`, `.claude/skills/*/SKILL.md`, `~/.claude/skills/**/SKILL.md`
- the auto memory `MEMORY.md`, the root `AGENTS.md`, `~/.codex/AGENTS.md`
- `CLAUDE.md` files outside the root are conditional entries `cond:<dir>`. More can be added with `extra_entries` in `.reachmap.json`

References followed: paths in backticks, Markdown links, paths in parentheses, `@path` imports, words with a known file extension.
References on lines that contain words such as "retired" or "deprecated" are treated as negative mentions and are not made into edges (they are still counted in the output).

Example output (the tool prints Japanese labels):
```
到達: 可 hop2 via CLAUDE.md ／ 他経路 3 ／ 否定言及 0        # reachable, 2 hops via CLAUDE.md / 3 other routes / 0 negative mentions
到達: 不可 ／ 否定言及 1(CLAUDE.md) ／ 条件付き入口経由 0 ／ コード言及 2   # not reachable / 1 negative mention / 0 via conditional entries / 2 code mentions
```

Configuration lives in `.reachmap.json` (see `.reachmap.example.json`). `tests/reachmap-fixture/` holds a small synthetic repository and expected results.

**Known limit:** negative detection works per line, so a single line like "Old X is retired; read Y instead" also drops the edge to Y. When in doubt it errs toward "reachable", and ambiguous references get edges to every candidate.

## Included: cache-keepalive (keeps the conversation cache alive)

Separately from the checks, [`cache-keepalive/`](cache-keepalive/README.md) is included.
It is a Claude Code hook that briefly wakes an idle conversation every 55 minutes so the prompt cache (1 hour) does not expire.
It only pays off when you come back to the same conversation. The break-even point and how to stop it are in its README.

## Included: ntfy-notify (push notifications to your phone)

[`ntfy-notify/`](ntfy-notify/README.md) is a hook that sends Claude Code replies to your phone through ntfy.
The point is to keep the AI from making important decisions on its own: when it stops to ask you, you notice right away even when you are away from the desk.

## Using with AI agents

Each tool answers with one fixed command. Instead of asking an agent to list things from memory, have it run these and read only the summary lines.

## License

MIT
