# cache-keepalive — Claude Code の会話キャッシュを延命するフック

Claude Code の会話を放置している間、55分ごとに本体を短く起こして「〔keepalive〕」とだけ返させ、
プロンプトキャッシュ（1時間）が切れないようにする Claude Code フックです。
寝落ちや長い離席のあと **同じ会話に戻る** とき、会話全体をキャッシュに書き直す費用を避けます。

> English summary: A Claude Code hook that wakes an idle session every 55 minutes with a one-word
> "〔keepalive〕" turn so the 1-hour prompt cache never expires. Worth it only if you come back to the
> same session. Stops automatically 12 hours after your last prompt.

## 仕組み

- `Stop` フック（`async` + `asyncRewake`）が、応答が終わるたびに見張りを1本仕掛けます（前の見張りは止めます）。
- 見張りは会話記録（transcript）の **更新時刻だけ** を見ます。中身は読みません。
- 無発話が 55 分（`KEEPALIVE_IDLE_SEC`）続いたら終了コード 2 で終わり、Claude Code が本体を起こします。
  本体は `rewakeMessage` の指示で「〔keepalive〕」とだけ返し、その応答の `Stop` でまた次の見張りが仕掛けられます。
- `UserPromptSubmit` フックが最後の発言時刻を記録します。最後の発言から 12 時間（`KEEPALIVE_MAX_SEC`）で止まります。
  自動の起床やバックグラウンド完了の通知では延長されません。
- `PostToolUse`（Skill）フックは、指定したスキル（既定: `handoff`・`session-wrap`）が呼ばれたらその会話の延命を止めます。
- ヘッドレス実行（環境変数 `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` を付けた `claude -p` 等）では何もしません。

**注意:** `asyncRewake` は Claude Code の文書化されていない内部機能です。Claude Code 2.1.280 で動作を確認しました。
更新後は `KEEPALIVE_IDLE_SEC=60` で起床を1回確かめてください。

## 損益分岐点（いつ得になるか）

キャッシュの料金倍率（Claude API の料金、通常の入力単価を 1 とした場合）:

| 項目 | 倍率 |
|---|---|
| キャッシュ読み出し | 約 0.1（Claude Opus 5.5 は 0.05、Claude Fable 5.1 は 0.025） |
| キャッシュ書き込み（1時間 TTL） | 2 |
| キャッシュ書き込み（5分 TTL） | 1.25 |

会話全体の大きさを C とすると、
- 延命 1 回 ≈ **C × 読み出し倍率**（＋「〔keepalive〕」の短い出力と、その 1 ターン分の小さな書き込み）
- 延命しないで N 時間後に戻る ≈ **C × 2**（1時間 TTL の書き直し 1 回）

| モデル | 延命 1 回（1時間ごと） | 書き直し 1 回 | 損益分岐点 |
|---|---|---|---|
| 読み出し 0.1 のモデル | 0.1 C | 2 C | 約 20 時間 |
| Claude Opus 5.5（0.05） | 0.05 C | 2 C | 約 40 時間 |
| Claude Fable 5.1（0.025） | 0.025 C | 2 C | 約 80 時間 |

- 既定の上限 12 時間は、どのモデルでも損益分岐点より手前です。**同じ会話に戻る限り** 延命のほうが安くなります。
- **戻らずに新しい会話で再開した場合、延命した分はすべて無駄** です。区切りで新しい会話にする運用なら入れないでください。
- 使用量の上限を超えてキャッシュの TTL が 5 分に縮む場合、55 分間隔の延命は効きません。
- 定額プランで利用量がこの料金比のとおりに数えられるかは公表されていません。上の表は料金ベースの目安です。

## 導入

1. `cache-keepalive.py` を置く（例: `~/.claude/hooks/cache-keepalive.py`）。
2. `settings.example.json` の `hooks` を、使う設定ファイル（`~/.claude/settings.json` か、プロジェクトの `.claude/settings.local.json`）へ追記する。
   `command` のパスは置いた場所に合わせる。`asyncTimeout` はミリ秒（43200000 = 12時間）。
3. 通知フック（ntfy 等）を使っている場合は、最後の応答が「〔keepalive〕」で始まるときに通知しないよう分岐を足す
   （しないと 55 分ごとに通知が届きます）。会話記録（transcript）の末尾には、応答の後ろに `system`
   （`stop_hook_summary`・`turn_duration` など）のレコードが付くことがあります。最後の応答を探すときは
   `assistant`・`user` 以外のレコードを読み飛ばしてください（読み飛ばさないと判定が外れて通知が出ます。実機で確認）。

| 環境変数 | 既定 | 意味 |
|---|---|---|
| `KEEPALIVE_IDLE_SEC` | 3300 | 無発話が何秒続いたら起こすか |
| `KEEPALIVE_MAX_SEC` | 43200 | 最後の発言から何秒で止めるか |
| `KEEPALIVE_STATE_DIR` | `~/.claude/cache-keepalive` | 状態ファイルの置き場（権限 700/600 で作成） |

## 止め方

| 止めたい範囲 | 方法 | 効くタイミング |
|---|---|---|
| 今の会話だけ | `$KEEPALIVE_STATE_DIR/<session_id>.stop` を作る（指定スキルを呼べば自動で作られる） | 次に起こす直前 |
| すべての会話を一時的に | `$KEEPALIVE_STATE_DIR/DISABLED` を作る（消せば再開） | 新しい見張りは即時。待機中の見張りは次に起こす直前 |
| 待機中の見張りをすぐ終わらせる | `$KEEPALIVE_STATE_DIR/<session_id>.pid` の先頭の PID を `kill` する | 即時（次の応答で再び仕掛けられる） |
| 完全にやめる | 設定ファイルから 3 つのフック（Stop・UserPromptSubmit・PostToolUse）を削除する | 次の応答から |

## 安全のための動作

- 見張りは会話ごとに 1 本だけ。前の見張りを止めるのは、PID・起動時刻・実行スクリプトの実パスが記録と一致するときだけです
  （PID が別のプロセスに再利用されていても止めません）。
- 例外が起きたら何もせず終了します（本体を起こさない）。
- 会話記録の中身は読みません。

## 既知の制約

- 判定から起床までの間に会話が再開された場合、1 回だけ余分な「〔keepalive〕」が入ることがあります。
- 起床の間隔は「最後の書き込みから 55 分」です。バックグラウンドの完了通知などで会話が更新されると、その分うしろにずれます。

## ライセンス

MIT
