# ntfy-notify — a hook that sends Claude Code replies to your phone

[日本語](README.ja.md)

When Claude Code finishes a reply (`Stop`) or waits for your confirmation (`Notification`), this hook sends a push notification to your phone through [ntfy](https://ntfy.sh). It looks at the end of the reply, sorts it into "question / progress report / done", and changes the priority and icon accordingly.

**Why:** to keep the AI from making important decisions on its own. When it stops to ask you, you notice right away even when you are away from the desk, and you decide yourself (this reduces the AI's incentive to "just keep going rather than make you wait" and decide for you).
Approving things from buttons on the phone is handled by a separate tool (ntfy approval).

## Notification types

| type | condition (`classify_stop.py`) | notification | priority |
|---|---|---|---|
| question | the last reply ends with "？" or contains a question phrase | "waiting for instructions" + first 120 characters of the reply | normal |
| progress | contains phrases such as "running now" or "will report when done" | "progress report" + first 120 characters of the reply | low |
| done | none of the above | "reply finished" | normal |
| silent | the reply starts with "〔keepalive〕" (from [cache-keepalive](../cache-keepalive/)) | not sent | — |
| (Notification) | Claude Code is waiting for confirmation | "waiting for confirmation" + the message | high |

The phrase lists are `QUESTION_PHRASES` and `PROGRESS_PHRASES` in `classify_stop.py`. They are **Japanese**; replace or extend them with phrases in your own language.

## Install

1. Install the ntfy app on your phone and subscribe to a topic name you choose.
2. Put `ntfy-notify.sh` and `classify_stop.py` in the same folder (for example `~/.claude/hooks/`). Make `ntfy-notify.sh` executable.
3. Add the `hooks` from `settings.example.json` to `~/.claude/settings.json` (adjust the paths to where you placed the files).
4. Pass the topic name in the environment variable `NTFY_TOPIC`. The easiest way is the `env` block of Claude Code's settings file:
   ```json
   { "env": { "NTFY_TOPIC": "<a hard-to-guess string>" } }
   ```
   Without `NTFY_TOPIC`, nothing is sent.

| environment variable | default | meaning |
|---|---|---|
| `NTFY_TOPIC` | none (nothing is sent if unset) | topic to send to |
| `NTFY_URL` | `https://ntfy.sh` | URL of your own ntfy server, if you run one |
| `CLAUDE_CODE_DISABLE_CLAUDE_MDS` | — | when `1` (marker for headless runs), nothing is sent |

Requirements: `bash`, `curl`, `jq`, `python3`.

## About topic names (important)

- A topic name can be **any string of letters, digits, `_` and `-`, up to 64 characters** (ntfy spec: https://docs.ntfy.sh/publish/).
- On the public ntfy.sh, **anyone who knows the topic name can read and send its notifications**. The topic name is effectively a password.
- Notifications include the first 120 characters of the reply, so conversation content goes out. Use a **random, hard-to-guess string, as long as you can**.
- Do not commit a settings file containing the topic name to a public repository.

## Notes

- The end of the transcript can have `system` records (`stop_hook_summary`, `turn_duration`, …) after the reply.
  `classify_stop.py` skips them to find the last reply (if it didn't, everything would be classified as "done").
- Classification is simple phrase matching. Adjust the lists when it misfires.

## License

MIT
