# claude-code-hygiene — Claude Code の運用文書・memory・hooks を機械的に点検する3本

Claude Code を長く使うと、CLAUDE.md・skills・hooks・自動 memory の間で参照がずれていきます。
「存在しないパスを指す memory」「登録したのに起動できない hook」「退役したはずのファイルへの参照」を、
モデルを呼ばずに（トークン0で）見つけるための小さな道具です。

| 道具 | 何を見つけるか | exit |
|---|---|---|
| `memcheck.py` | memory ディレクトリの陳腐化: 存在しないパス参照、索引 MEMORY.md と実体の不一致、退役語を正本として参照する記述、実体のない `[[wiki link]]`（参考表示） | 0=正常 / 1=検出 |
| `hooks-selftest.py` | settings.json の全 hook を実際に起動して検査: 起動可能性、実行正常性、SessionStart の内容 | 0=全OK / 1=FAIL あり |
| `refscan.sh` | 退役・移設したファイルの名前がどこに残っているかを横断検索し、「現役」と「記録・証跡」に分けて出す | 0=現役あり / 1=現役0件 / 2=引数なし |

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

## AI エージェントと使う

どれも固定コマンド1本で答えが出るので、エージェントに「記憶から列挙させる」代わりにこれを実行させ、
出力の集計行だけを読ませる使い方を想定しています。

## ライセンス

MIT
