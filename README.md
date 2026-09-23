# claude-code-hygiene — Claude Code の運用文書・memory・hooks を機械的に点検する4本

Claude Code を長く使うと、CLAUDE.md・skills・hooks・自動 memory の間で参照がずれていきます。
「存在しないパスを指す memory」「登録したのに起動できない hook」「退役したはずのファイルへの参照」を、
モデルを呼ばずに（トークン0で）見つけるための小さな道具です。

| 道具 | 何を見つけるか | exit |
|---|---|---|
| `memcheck.py` | memory ディレクトリの陳腐化: 存在しないパス参照、索引 MEMORY.md と実体の不一致、退役語を正本として参照する記述、実体のない `[[wiki link]]`（参考表示） | 0=正常 / 1=検出 |
| `hooks-selftest.py` | settings.json の全 hook を実際に起動して検査: 起動可能性、実行正常性、SessionStart の内容 | 0=全OK / 1=FAIL あり |
| `refscan.sh` | 退役・移設したファイルの名前がどこに残っているかを横断検索し、「現役」と「記録・証跡」に分けて出す | 0=現役あり / 1=現役0件 / 2=引数なし |
| `reachmap.py` | Claude Code が自動で読む入口（CLAUDE.md・hooks・skills・memory）から参照を辿って、その文書に到達できるかを判定。孤児・切れ参照・曖昧参照の棚卸し | 0=正常 / 2=エラー |

依存: Python 3.9+ 標準ライブラリ、zsh、git、grep。

## memcheck.py

```bash
python3 memcheck.py                      # cwd の git ルートに対応する memory を検査
python3 memcheck.py --repo /path/to/repo # 相対パスの解決基準を指定
python3 memcheck.py --memory-dir ~/.claude/projects/-path-to-repo/memory
```

memory の場所は Claude Code の規則（`~/.claude/projects/<cwd の / を - に置換>/memory`）から自動で導きます。
リポジトリ直下に `.memcheck.json` を置くと、退役語と免責語を指定できます（`.memcheck.example.json` 参照）。

検査内容:
1. バッククォートで囲まれたパスが実在するか（`~`・絶対・リポジトリ相対を解決）
2. `[[link]]` の実体があるか（無くてもエラーにしない。将来書くべき印として表示）
3. MEMORY.md の索引と実体ファイルの双方向一致
4. 退役語を「退役」「旧」などの免責語なしに書いている行

## hooks-selftest.py

```bash
python3 hooks-selftest.py                       # <repo>/.claude/settings.local.json（無ければ settings.json）
python3 hooks-selftest.py --settings path.json  # 明示
```

登録された全 hook について、コマンドの解決（CHECK-A）、ダミー入力での実行（CHECK-B）、
SessionStart hook の出力内容（CHECK-C）を検査します。副作用はなく `.claude/` は変更しません。

## refscan.sh

```bash
/bin/zsh refscan.sh <対象path> [出力base]     # basename から検索語（ハイフン版・アンダースコア版）を導出
/bin/zsh refscan.sh --text <文字列> [出力base]
```

リポジトリ全体と `~/.claude`、LaunchAgents、シェル設定、crontab を横断検索し、
`<base>-all.txt`（全件）と `<base>-live.txt`（現役）に落として、標準出力には件数と現役一覧だけを出します。
書き込めない環境では標準出力モードに落ちます。

リポジトリ直下の `.refscan.conf` で探索先の追加、探索先ごとの除外、「記録・証跡」とみなすパスの前方一致パターンを指定します
（`.refscan.example.conf` 参照）。判定は全件ではなく現役件数で行います。ログに自分自身のコマンド文字列が記録されて自己一致することがあるためです。

**既知の限界**: 検索語は basename から導くので、識別子の選び方は人に残ります。退役対象ごとに識別子を列挙して回してください。

## reachmap.py

```bash
python3 reachmap.py docs/some-guide.md   # この文書は入口から辿って読まれうるか
python3 reachmap.py --report             # 孤児・切れ参照・曖昧参照・同名重複の棚卸し（JSON も出力）
python3 reachmap.py --no-home            # ~/.claude 等を入口に含めない
```

入口として扱うもの:
- ルートの `CLAUDE.md`・`CLAUDE.local.md`、`~/.claude/CLAUDE.md`
- `.claude/settings.json`・`settings.local.json` の hook（SessionStart・UserPromptSubmit）が読み込む `.md`
- `.claude/agents/*.md`、`.claude/skills/*/SKILL.md`、`~/.claude/skills/**/SKILL.md`
- 自動メモリの `MEMORY.md`、ルートの `AGENTS.md`、`~/.codex/AGENTS.md`
- ルート以外の `CLAUDE.md` は条件付き入口 `cond:<dir>`。`.reachmap.json` の `extra_entries` で追加できる

参照として辿るもの: バッククォート内のパス、Markdown リンク、括弧内のパス、`@path` 形式の import、既知拡張子を持つ語。
「退役」「deprecated」などの語を含む行からの参照は否定言及として辺にしません（件数は出力に出ます）。

出力例:
```
到達: 可 hop2 via CLAUDE.md ／ 他経路 3 ／ 否定言及 0
到達: 不可 ／ 否定言及 1(CLAUDE.md) ／ 条件付き入口経由 0 ／ コード言及 2
```

設定は `.reachmap.json`（`.reachmap.example.json` 参照）。`tests/reachmap-fixture/` に合成テスト用の小さなリポジトリと期待値があります。

**既知の限界**: 否定判定は行単位なので「旧Xは退役。現行はYを読む」の1行では Y への辺も落ちます。迷ったら到達側に倒す方針で、曖昧参照は全候補に辺を張ります。

## 同梱: cache-keepalive（会話キャッシュの延命フック）

点検の道具とは別に、[`cache-keepalive/`](cache-keepalive/README.md) を同梱しています。
放置中の会話を 55 分ごとに短く起こしてプロンプトキャッシュ（1時間）を延命する Claude Code フックです。
同じ会話に戻るときだけ得になります。損益分岐点・止め方は同フォルダの README にあります。

## AI エージェントと使う

どれも固定コマンド1本で答えが出るので、エージェントに「記憶から列挙させる」代わりにこれを実行させ、
出力の集計行だけを読ませる使い方を想定しています。

## ライセンス

MIT
