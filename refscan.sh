#!/bin/zsh
# refscan.sh v4 - 参照所在スキャン
# 退役・移設したパスへの参照がどこに残っているかを機械的に洗い出す。

setopt NULL_GLOB

usage() {
  echo "使用法: $0 <対象path> [出力ファイルbase]" >&2
  echo "       $0 --text <検索文字列> [出力ファイルbase]" >&2
}

# 1. 引数チェックと検索語の導出（未指定または空文字のときはファイルを作らず exit 2）
if [[ -z "$1" ]]; then
  usage
  exit 2
fi

if [[ "$1" == "--text" ]]; then
  # 文字列モード。検索語は渡された文字列そのもの1語（変種生成はしない）。
  if [[ -z "$2" ]]; then
    usage
    exit 2
  fi
  WORDS=("$2")
  WORDS_STR="$2"
  SPECIFIED_OUT_BASE="$3"
else
  # パスモード（既定）
  TARGET="${1%/}"
  if [[ -z "$TARGET" ]]; then
    usage
    exit 2
  fi

  BASE_NAME="${TARGET:t}"
  if [[ -z "$BASE_NAME" ]]; then
    usage
    exit 2
  fi

  # 2. 検索語の導出
  WORD1="$BASE_NAME"
  WORD2="${BASE_NAME//-/_}"

  if [[ "$WORD1" == "$WORD2" ]]; then
    WORDS=("$WORD1")
    WORDS_STR="$WORD1"
  else
    WORDS=("$WORD1" "$WORD2")
    WORDS_STR="$WORD1, $WORD2"
  fi
  SPECIFIED_OUT_BASE="$2"
fi

# 3. 出力先ファイルの決定
TS="$(date +%Y%m%d-%H%M%S)"
OUT_BASE="${SPECIFIED_OUT_BASE:-/tmp/refscan-$TS}"
OUT_DIR="${OUT_BASE:h}"
if [[ -n "$OUT_DIR" && ! -d "$OUT_DIR" ]]; then
  mkdir -p "$OUT_DIR" 2>/dev/null
fi

ALL="${OUT_BASE}-all.txt"
LIVE="${OUT_BASE}-live.txt"

# 4. 探索先
#   REPO: $REFSCAN_REPO > cwd の git ルート > cwd
#   追加の探索先・除外・記録パターンは <REPO>/.refscan.conf（zsh。EXTRA_TARGETS / REPO_PRUNE / RECORD_PATTERN / CODEX_EXCLUDES）
REPO="${REFSCAN_REPO:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
REPO="${REPO%/}"
EXTRA_TARGETS=(
  "$HOME/Library/LaunchAgents"
  "$HOME/.claude/settings.json"
  "$HOME/.claude/skills"
  "$HOME/.claude/hooks"
  "$HOME/.zshrc"
  "$HOME/.zprofile"
)
REPO_PRUNE=()
RECORD_PATTERN='^$'
CODEX_EXCLUDES=()
if [[ -f "$REPO/.refscan.conf" ]]; then
  source "$REPO/.refscan.conf"
fi
SEARCH_TARGETS=("$REPO" "${EXTRA_TARGETS[@]}")

# 5. 除外オプション（固定）
EXCLUDES=(
  --exclude-dir=.git
  --exclude-dir=node_modules
  --exclude-dir=__pycache__
  --exclude-dir=.venv
  --exclude-dir=.pytest_cache
  --exclude-dir=_snapshot_2026-08-09
  --exclude-dir=_backup_2026-07-11
  --exclude-dir=worktrees
  --exclude-dir=archived_sessions
  --exclude-dir=attachments
  --exclude='*.sqlite*'
  --exclude='*.jsonl'
)



# 6. 書き込み可否の判定
# read-onlyサンドボックス（LUNA等）はどこへも書けないため、
# その場合は一覧をファイルへ落とさず標準出力へ直接出す stdout モードへ落ちる。
STDOUT_MODE=0
if ! : > "$ALL" 2>/dev/null; then
  STDOUT_MODE=1
fi

# 7. 検索実行（/usr/bin/grep を使用、語ごとに実行して結合・重複排除・パス正規化）
RESULTS="$(
{
  for w in "${WORDS[@]}"; do
    for target in "${SEARCH_TARGETS[@]}"; do
      if [[ -e "$target" ]]; then
        # 探索先ごとの追加除外だけを切り替え、grepの呼び出しは1箇所に保つ
        local_extra=()
        case "$target" in
          "$HOME/.codex")      local_extra=("${CODEX_EXCLUDES[@]}") ;;
          "$REPO")             local_extra=("${REPO_PRUNE[@]}") ;;
        esac
        /usr/bin/grep -r -l -F -w --binary-files=without-match \
          "${EXCLUDES[@]}" "${local_extra[@]}" "$w" "$target" 2>/dev/null
      fi
    done
    if crontab -l 2>/dev/null | /usr/bin/grep -q -F -w "$w"; then
      echo "crontab"
    fi
  done
} | sed "s|^${REPO}/||" | sort -u
)"

# 7. 「記録・証跡」の判定・除外（.refscan.conf の RECORD_PATTERN に前方一致するものを除く）

LIVE_RESULTS="$(printf '%s\n' "$RESULTS" | /usr/bin/grep -vE "$RECORD_PATTERN" || true)"

if (( STDOUT_MODE == 0 )); then
  printf '%s\n' "$RESULTS" > "$ALL"
  printf '%s\n' "$LIVE_RESULTS" > "$LIVE"
fi

TOTAL_COUNT=$(printf '%s\n' "$RESULTS" | /usr/bin/grep -c . || true)
LIVE_COUNT=$(printf '%s\n' "$LIVE_RESULTS" | /usr/bin/grep -c . || true)

# 8. 標準出力（指定順に出力）
echo "検索語: $WORDS_STR"
echo "全件: ${TOTAL_COUNT}　うち現役: ${LIVE_COUNT}"
if (( STDOUT_MODE == 1 )); then
  echo "一覧: (書き込み不可のため標準出力へ出力)"
else
  echo "一覧: $ALL / $LIVE"
fi
echo "--- 現役（要対応） ---"

if (( LIVE_COUNT <= 40 )); then
  (( LIVE_COUNT > 0 )) && printf '%s\n' "$LIVE_RESULTS"
elif (( STDOUT_MODE == 1 )); then
  # ファイルを参照できないため標準出力に出すが、上限は設ける。
  # 汎用basename（SKILL.md等）だと数百件になり受け手のコンテキストを圧迫するため。
  printf '%s\n' "$LIVE_RESULTS" | head -n 60
  if (( LIVE_COUNT > 60 )); then
    echo "（他 $(( LIVE_COUNT - 60 )) 件。識別子が汎用すぎる可能性がある。親ディレクトリ名を含めて渡し直すこと）"
  fi
else
  printf '%s\n' "$LIVE_RESULTS" | head -n 40
  echo "（他 $(( LIVE_COUNT - 40 )) 件。全件は ${LIVE} を参照）"
fi

# 9. 終了コード判定
# 判定は現役件数で行う。全件だと、このコマンド自身の文字列が
# ログ等へ記録されて自己一致し、常に1件以上になりうるため。
if (( LIVE_COUNT == 0 )); then
  echo "警告: 現役のヒット0件。識別子が誤っているか、参照がすべて記録・証跡の可能性がある" >&2
  exit 1
fi

exit 0
