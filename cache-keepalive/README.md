# cache-keepalive — a hook that keeps Claude Code's conversation cache alive

[日本語](README.ja.md)

While a Claude Code conversation sits idle, this hook wakes it briefly every 55 minutes and has it reply with just "〔keepalive〕", so the prompt cache (1 hour) never expires.
When you **come back to the same conversation** after falling asleep or a long break, you avoid paying to write the whole conversation into the cache again.

## How it works

- A `Stop` hook (`async` + `asyncRewake`) sets up one watcher each time a reply finishes (the previous watcher is stopped).
- The watcher looks **only at the modification time** of the transcript. It does not read its contents.
- After 55 minutes of silence (`KEEPALIVE_IDLE_SEC`) it exits with code 2, and Claude Code wakes the session.
  Following `rewakeMessage`, the session replies with just "〔keepalive〕", and that reply's `Stop` sets up the next watcher.
- A `UserPromptSubmit` hook records when you last spoke. It stops 12 hours (`KEEPALIVE_MAX_SEC`) after your last prompt.
  Automatic wake-ups and background-completion notices do not extend it.
- A `PostToolUse` (Skill) hook stops the keepalive for the conversation when one of the listed skills (default: `handoff`, `session-wrap`) is called.
- Does nothing in headless runs (for example `claude -p` with the environment variable `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`).

**Note:** `asyncRewake` is an undocumented internal Claude Code feature. Verified on Claude Code 2.1.280.
After updating Claude Code, check one wake-up with `KEEPALIVE_IDLE_SEC=60`.

## Break-even (when it pays off)

Cache price multipliers (Claude API pricing, normal input price = 1):

| item | multiplier |
|---|---|
| cache read | about 0.1 (Claude Opus 5.5: 0.05, Claude Fable 5.1: 0.025) |
| cache write (1-hour TTL) | 2 |
| cache write (5-minute TTL) | 1.25 |

With C = the size of the whole conversation:
- one keepalive ≈ **C × read multiplier** (plus the short "〔keepalive〕" output and a small write for that one turn)
- not keeping it alive and coming back N hours later ≈ **C × 2** (one 1-hour-TTL rewrite)

| model | one keepalive (hourly) | one rewrite | break-even |
|---|---|---|---|
| models with read 0.1 | 0.1 C | 2 C | about 20 hours |
| Claude Opus 5.5 (0.05) | 0.05 C | 2 C | about 40 hours |
| Claude Fable 5.1 (0.025) | 0.025 C | 2 C | about 80 hours |

- The default 12-hour limit is below the break-even point for every model. **As long as you return to the same conversation**, keeping it alive is cheaper.
- **If you start a new conversation instead of returning, every keepalive was wasted.** If you usually start fresh at each break, do not install this.
- If you go over your usage limit and the cache TTL drops to 5 minutes, a 55-minute keepalive does nothing.
- Whether subscription plans count usage according to these price ratios is not published. The table is a price-based estimate.

## Install

1. Place `cache-keepalive.py` (for example at `~/.claude/hooks/cache-keepalive.py`).
2. Add the `hooks` from `settings.example.json` to the settings file you use (`~/.claude/settings.json` or the project's `.claude/settings.local.json`).
   Adjust the `command` path to where you placed the file. `asyncTimeout` is in milliseconds (43200000 = 12 hours).
3. If you use a notification hook (ntfy etc.), add a branch so it does not notify when the last reply starts with "〔keepalive〕"
   (otherwise you get a notification every 55 minutes). The end of the transcript can have `system` records
   (`stop_hook_summary`, `turn_duration`, …) after the reply. When looking for the last reply, skip records other than
   `assistant` and `user` (if you don't, the check misses and the notification goes out; confirmed on a real machine).

| environment variable | default | meaning |
|---|---|---|
| `KEEPALIVE_IDLE_SEC` | 3300 | seconds of silence before waking the session |
| `KEEPALIVE_MAX_SEC` | 43200 | seconds after your last prompt to stop |
| `KEEPALIVE_STATE_DIR` | `~/.claude/cache-keepalive` | where state files live (created with permissions 700/600) |

## How to stop it

| scope | how | takes effect |
|---|---|---|
| this conversation only | create `$KEEPALIVE_STATE_DIR/<session_id>.stop` (created automatically when a listed skill is called) | just before the next wake-up |
| all conversations, temporarily | create `$KEEPALIVE_STATE_DIR/DISABLED` (delete it to resume) | new watchers: immediately. Waiting watchers: just before the next wake-up |
| end a waiting watcher now | `kill` the PID at the top of `$KEEPALIVE_STATE_DIR/<session_id>.pid` | immediately (set up again at the next reply) |
| remove completely | delete the 3 hooks (Stop, UserPromptSubmit, PostToolUse) from the settings file | from the next reply |

## Safety behavior

- Only one watcher per conversation. The previous watcher is stopped only when its PID, start time, and the real path of the running script all match the record
  (a PID reused by another process is left alone).
- On any exception it exits without doing anything (it does not wake the session).
- It does not read the contents of the transcript.

## Known limits

- If the conversation resumes between the check and the wake-up, one extra "〔keepalive〕" can slip in.
- The wake-up interval is "55 minutes after the last write". If the conversation is updated by, for example, a background-completion notice, the wake-up shifts later by that much.

## License

MIT
