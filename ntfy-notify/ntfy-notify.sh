#!/bin/bash

set -uo pipefail

TOPIC="${NTFY_TOPIC:-}"
[ -n "$TOPIC" ] || exit 0
KIND="${1:-stop}"
INPUT="$(cat)"
PROJECT="$(basename "$PWD")"

# ヘッドレス実行（claude -p 等）は CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 を付けておけば通知しない。
if [ "${CLAUDE_CODE_DISABLE_CLAUDE_MDS:-}" = "1" ]; then
  exit 0
fi

case "$KIND" in
  stop)
    TRANSCRIPT_PATH="$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
    CATEGORY="done"
    SNIPPET=""

    if [ -n "$TRANSCRIPT_PATH" ]; then
      STOP_RESULT="$(python3 "$(dirname "$0")/classify_stop.py" "$TRANSCRIPT_PATH" 2>/dev/null || true)"
      if [ -n "$STOP_RESULT" ]; then
        CATEGORY="$(printf '%s' "$STOP_RESULT" | jq -r '.category // "done"' 2>/dev/null || true)"
        SNIPPET="$(printf '%s' "$STOP_RESULT" | jq -r '.snippet // empty' 2>/dev/null || true)"
      fi
    fi

    # キャッシュ延命の応答（〔keepalive〕）は通知しない
    [ "$CATEGORY" = "silent" ] && exit 0

    case "$CATEGORY" in
      question)
        TITLE="Claude Code 指示待ち: ${PROJECT}"
        BODY="${SNIPPET:-判断・指示を求めています。}"
        PRIORITY="default"
        TAGS="question"
        ;;
      progress)
        TITLE="Claude Code 経過報告: ${PROJECT}"
        BODY="${SNIPPET:-作業を継続しています。}"
        PRIORITY="low"
        TAGS="hourglass_flowing_sand"
        ;;
      *)
        TITLE="Claude Code 応答完了: ${PROJECT}"
        BODY="応答が終わりました。"
        PRIORITY="default"
        TAGS="white_check_mark"
        ;;
    esac
    ;;
  notification)
    MSG="$(printf '%s' "$INPUT" | jq -r '.message // empty')"
    if [ -z "$MSG" ]; then
      MSG="確認待ちがあります"
    fi
    TITLE="Claude Code 確認待ち: ${PROJECT}"
    BODY="$MSG"
    PRIORITY="high"
    TAGS="bell"
    ;;
  *)
    TITLE="Claude Code: ${PROJECT}"
    BODY="通知"
    PRIORITY="default"
    TAGS="robot"
    ;;
esac

curl -s -m 10 \
  -H "Title: ${TITLE}" \
  -H "Priority: ${PRIORITY}" \
  -H "Tags: ${TAGS}" \
  -d "${BODY}" \
  "${NTFY_URL:-https://ntfy.sh}/${TOPIC}" >/dev/null 2>&1

exit 0
