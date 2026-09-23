# ntfy-notify — Claude Code の応答をスマホへ通知するフック

Claude Code が応答を終えたとき（`Stop`）と確認待ちになったとき（`Notification`）に、
[ntfy](https://ntfy.sh) 経由でスマホへ通知します。応答の最後を見て「質問・経過報告・完了」を分け、
優先度とアイコンを変えます。

**目的:** 重要な判断を AI に勝手にさせないこと。AI が判断を人に仰いで止まったときに、離席中でもすぐ気づいて
自分で決められるようにします（AI が「待たせるより進めてしまおう」と判断を肩代わりする動機を減らす）。
承認そのものをスマホのボタンで行う仕組みは、別の道具（ntfy 承認）で扱います。

> English summary: Push notifications for Claude Code via ntfy. Classifies the last reply as
> question / progress / done (Japanese phrase lists, easy to edit) and skips cache-keepalive turns.

## 通知の種類

| 分類 | 条件（`classify_stop.py`） | 通知 | 優先度 |
|---|---|---|---|
| question | 最後の応答が「？」で終わる、または質問の言い回しを含む | 「指示待ち」＋応答の先頭 120 文字 | 通常 |
| progress | 「実行中です」「完了次第報告」などの言い回しを含む | 「経過報告」＋応答の先頭 120 文字 | 低 |
| done | 上のどれでもない | 「応答完了」 | 通常 |
| silent | 応答が「〔keepalive〕」で始まる（[cache-keepalive](../cache-keepalive/) の延命） | 送らない | — |
| （Notification） | Claude Code の確認待ち | 「確認待ち」＋メッセージ | 高 |

言い回しの一覧は `classify_stop.py` の `QUESTION_PHRASES`・`PROGRESS_PHRASES`（日本語）にあります。自分の使い方に合わせて足し引きしてください。

## 導入

1. スマホに ntfy アプリを入れ、自分で決めたトピック名を購読する。
2. `ntfy-notify.sh` と `classify_stop.py` を同じフォルダに置く（例: `~/.claude/hooks/`）。`ntfy-notify.sh` に実行権限を付ける。
3. `settings.example.json` の `hooks` を `~/.claude/settings.json` に追記する（パスは置いた場所に合わせる）。
4. トピック名を環境変数 `NTFY_TOPIC` で渡す。Claude Code の設定ファイルの `env` に書くのが手軽です:
   ```json
   { "env": { "NTFY_TOPIC": "<推測されにくい文字列>" } }
   ```
   `NTFY_TOPIC` が無ければ何も送りません。

| 環境変数 | 既定 | 意味 |
|---|---|---|
| `NTFY_TOPIC` | なし（未設定なら送らない） | 送り先のトピック名 |
| `NTFY_URL` | `https://ntfy.sh` | 自前の ntfy サーバーを使う場合の URL |
| `CLAUDE_CODE_DISABLE_CLAUDE_MDS` | — | `1` のとき（ヘッドレス実行の目印）は通知しない |

依存: `bash`・`curl`・`jq`・`python3`。

## トピック名について（大事）

- トピック名は **英数字と `_`・`-` で、最大 64 文字まで自由に設定できます**（ntfy の仕様: https://docs.ntfy.sh/publish/ ）。
- 公開の ntfy.sh では、**トピック名を知っている人は誰でもその通知を読め、送り込めます**。トピック名が実質のパスワードです。
- 通知には応答の先頭 120 文字が入ります。会話の中身が載るので、**推測されにくいランダムな文字列を、なるべく長く** 使ってください。
- トピック名を書いた設定ファイルを公開リポジトリに入れないでください。

## 注意

- 会話記録（transcript）の末尾には、応答の後ろに `system`（`stop_hook_summary`・`turn_duration` など）のレコードが付くことがあります。
  `classify_stop.py` はそれを読み飛ばして最後の応答を探します（読み飛ばさないと、常に「応答完了」になります）。
- 分類は言い回しの一致による簡単なものです。外れたときは一覧を調整してください。

## ライセンス

MIT
