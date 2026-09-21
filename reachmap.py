#!/usr/bin/env python3
"""reachmap.py: 参照の到達可能性を判定する機械的検証ツール (v1).

あるファイルが、自動読込される入口から参照を辿って到達できるかを機械的に判定する。
是正の優先順位付けと、退役候補（孤児）の検出に用いる。

[v1の到達可能性スコープ]
(b) 参照を辿れば読まれうる＝汚染 のみを判定対象とする。
(a) 起動時に自動でコンテキストへ入るコスト集計や、(c) launchd/cron実行は対象外。

[既知の制約・偽陰性]
否定判定は行単位で行うため、「旧Xは退役。現行はYを読む」のような1行では
Yへの辺も落ちる（偽陰性）。v1ではこれを許容し、出力およびJSON報告に
否定言及の件数を必ず記録して利用者が割り引けるようにする。

[方針]
迷ったら到達側に倒す（偽陰性を避ける）。曖昧参照もすべての候補へ辺を張る。
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import subprocess

# 否定言及の判定語（この語を含む行からの参照は辺にしない）。.reachmap.json の exemption_words で上書き可。
EXEMPTION_WORDS: Tuple[str, ...] = ("退役", "旧", "archive", "統合元", "使わない", "参照しない", "deprecated", "legacy", "do not use")

CONFIG_NAME = ".reachmap.json"


def git_toplevel(start: Path) -> Optional[Path]:
    r = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def default_memory_index(repo: Path) -> Path:
    """Claude Code の自動メモリ索引: ~/.claude/projects/<repoの / を - に置換>/memory/MEMORY.md"""
    return Path.home() / ".claude" / "projects" / str(repo.resolve()).replace("/", "-") / "memory" / "MEMORY.md"


def load_config(repo_root: Path) -> Dict[str, Any]:
    """<repo>/.reachmap.json を読む。無ければ既定。

    keys:
      record_prefixes: [str]   記録・証跡パス（前方一致）。グラフから完全に除外
      extra_entries:   [{"glob": str, "tag": str}]  追加の入口（例: 条件付き入口 cond:xxx）
      exemption_words: [str]   否定言及の判定語（既定を置換）
      home_entries:    bool    ~/.claude/skills・~/.claude/CLAUDE.md・memory・~/.codex/AGENTS.md を入口に含める（既定 true）
      hook_events:     [str]   入口として辿る hook イベント（既定 SessionStart, UserPromptSubmit）
    """
    cfg: Dict[str, Any] = {"record_prefixes": [], "extra_entries": [], "exemption_words": None,
                           "home_entries": True, "hook_events": ["SessionStart", "UserPromptSubmit"]}
    f = repo_root / CONFIG_NAME
    if f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            sys.stderr.write(f"エラー: {f} のJSONが不正: {e}\n")
            sys.exit(2)
        for k in cfg:
            if k in data:
                cfg[k] = data[k]
    return cfg


# 既知の拡張子定義
KNOWN_EXTENSIONS: Set[str] = {
    ".md",
    ".tsv",
    ".py",
    ".sh",
    ".json",
    ".toml",
    ".plist",
}

# 辺の発生元となる拡張子
# ※.json と .tsv は .claude/ 配下のみが対象
DOC_EXTENSIONS: Set[str] = {".md"}

# コード言及の走査対象拡張子（.md を除き、.claude/ 配下でないもの）
CODE_EXTENSIONS: Set[str] = KNOWN_EXTENSIONS - {".md"}


@dataclasses.dataclass
class Edge:
    source: Path
    target: Path
    tag: str  # "normal", "ambiguous", "dir"
    line_no: int


@dataclasses.dataclass
class NegativeMention:
    source: Path
    target: Path
    line_no: int
    raw_token: str


@dataclasses.dataclass
class BrokenRef:
    source: Path
    line_no: int
    raw_token: str


@dataclasses.dataclass
class AmbiguousRef:
    source: Path
    line_no: int
    raw_token: str
    candidates: List[Path]


@dataclasses.dataclass
class GlobRef:
    source: Path
    line_no: int
    raw_token: str


def load_record_prefixes(record_paths_file: Path) -> List[str]:
    """record_paths.txt から記録・除外パス接頭辞を読み込む。"""
    if not record_paths_file.is_file():
        return []
    prefixes: List[str] = []
    with open(record_paths_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip().replace("\\", "/")
            if not line or line.startswith("#"):
                continue
            prefixes.append(line)
    return prefixes


def is_record_or_excluded(rel_path_posix: str, prefixes: List[str]) -> bool:
    """リポジトリ相対パスが記録・除外接頭辞に一致するか判定する。"""
    for p in prefixes:
        if rel_path_posix.startswith(p):
            return True
    return False


def discover_entries(
    repo_root: Path,
    cfg: Dict[str, Any],
    record_prefixes: List[str],
) -> Tuple[Dict[Path, str], List[str]]:
    """入口規則 E1〜E6 に従い入口ファイルを導出し、未認識候補を検出する。

    E1 CLAUDE.md / CLAUDE.local.md（ルート）、~/.claude/CLAUDE.md
    E2 .claude/settings.json と settings.local.json の hook（SessionStart 等）が読み込む .md
    E3 .claude/agents/*.md、.claude/skills/*/SKILL.md、~/.claude/skills/**/SKILL.md
    E4 .reachmap.json の extra_entries（条件付き入口 cond:xxx 等）、ルート以外の CLAUDE.md は cond:<dir>
    E5 自動メモリの MEMORY.md
    E6 AGENTS.md（ルート）、~/.codex/AGENTS.md

    Returns:
        (entries_map: {Path: entry_tag}, warnings: [str])
    """
    entries: Dict[Path, str] = {}
    warnings: List[str] = []
    use_home = bool(cfg.get("home_entries", True))

    # E1: リポジトリルートの CLAUDE.md / CLAUDE.local.md、ユーザー共通の ~/.claude/CLAUDE.md
    e1_path = (repo_root / "CLAUDE.md").resolve()
    if e1_path.is_file():
        entries[e1_path] = "E1"
    e1_local = (repo_root / "CLAUDE.local.md").resolve()
    if e1_local.is_file():
        entries[e1_local] = "E1"
    if use_home:
        home_claude_md = (Path.home() / ".claude" / "CLAUDE.md").resolve()
        if home_claude_md.is_file():
            entries[home_claude_md] = "E1"

    # E2: .claude/settings*.json の hook スクリプトが読み込む .md
    settings_files = [repo_root / ".claude" / "settings.json", repo_root / ".claude" / "settings.local.json"]
    hook_items: List[Dict[str, Any]] = []
    for settings_file in settings_files:
        if settings_file.is_file():
            try:
                with open(settings_file, "r", encoding="utf-8") as f:
                    settings_data = json.load(f)
                for ev in cfg.get("hook_events", []):
                    hook_items.extend(settings_data.get("hooks", {}).get(ev, []))
            except Exception as e:
                warnings.append(f"E2警告: {settings_file.name} の解析中にエラーが発生しました: {e}")
    if hook_items:
        if True:
            try:
                for item in hook_items:
                    for hook in item.get("hooks", []):
                        cmd = hook.get("command", "")
                        # コマンドからスクリプトパスを取得
                        # 例: /path/to/repo/.claude/hooks/session-start.sh
                        script_tokens = cmd.split()
                        script_path: Optional[Path] = None
                        for tok in script_tokens:
                            tok_clean = tok.strip("\"'")
                            p_cand = Path(tok_clean)
                            if not p_cand.is_absolute():
                                p_cand = repo_root / p_cand
                            if p_cand.is_file():
                                script_path = p_cand
                                break

                        if script_path:
                            # スクリプトを読み、非コメント行の .md を入口、
                            # コメント行のみの .md を差分警告とする
                            active_mds: Set[str] = set()
                            comment_only_mds: Set[str] = set()

                            with open(script_path, "r", encoding="utf-8") as sf:
                                for sline in sf:
                                    stripped = sline.strip()
                                    is_comment = stripped.startswith("#")
                                    # 行内の .md パス風トークンを抽出
                                    found_mds = re.findall(
                                        r"""(?:"|'|`|\b)(/[^\s'"<>`]+?\.md|[a-zA-Z0-9_.\-/]+?\.md)""",
                                        sline,
                                    )
                                    for m in found_mds:
                                        m_clean = m.strip("\"'`")
                                        if is_comment:
                                            comment_only_mds.add(m_clean)
                                        else:
                                            active_mds.add(m_clean)

                            diff_comments = comment_only_mds - active_mds
                            for c_md in sorted(diff_comments):
                                warnings.append(
                                    f"E2警告: SessionStart hook ({script_path.name}) のコメント行にのみ現れる入口候補差分: {c_md}"
                                )

                            for a_md in sorted(active_mds):
                                ap = Path(a_md)
                                if not ap.is_absolute():
                                    ap = (repo_root / ap).resolve()
                                else:
                                    ap = ap.resolve()
                                if ap.is_file():
                                    entries[ap] = "E2"
            except Exception as e:
                warnings.append(f"E2警告: hook の解析中にエラーが発生しました: {e}")

    # E3: .claude/agents/*.md、.claude/skills/*/SKILL.md、~/.claude/skills/**/SKILL.md
    # リポジトリ内 .claude/agents/*.md
    agents_dir = repo_root / ".claude" / "agents"
    if agents_dir.is_dir():
        for p in agents_dir.glob("*.md"):
            if p.is_file():
                entries[p.resolve()] = "E3"

    # リポジトリ内 .claude/skills/*/SKILL.md
    claude_skills_dir = repo_root / ".claude" / "skills"
    if claude_skills_dir.is_dir():
        for p in claude_skills_dir.glob("*/SKILL.md"):
            if p.is_file():
                entries[p.resolve()] = "E3"

    # HOME 配下 ~/.claude/skills/**/SKILL.md
    if use_home:
        home_claude_skills = Path("~/.claude/skills").expanduser()
        if home_claude_skills.is_dir():
            for p in home_claude_skills.glob("**/SKILL.md"):
                if p.is_file():
                    entries[p.resolve()] = "E3"

    # E4: 設定の extra_entries（例: {"glob": "notes/.claude/skills/*/SKILL.md", "tag": "cond:notes"}）
    extra_globs: List[Tuple[str, str]] = []
    for ent in cfg.get("extra_entries", []):
        g, tag = ent.get("glob"), ent.get("tag", "E4")
        if not g:
            continue
        extra_globs.append((g, tag))
        for p in repo_root.glob(g):
            if p.is_file():
                entries[p.resolve()] = tag

    # リポジトリ直下以外の CLAUDE.md（サブプロジェクト等）も
    # cond:<dir> の条件付き入口として扱う（自己検査警告の除外対象）
    for candidate in repo_root.glob("**/CLAUDE.md"):
        cand_resolved = candidate.resolve()
        if cand_resolved == e1_path:
            continue
        try:
            rel_p = cand_resolved.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        if is_record_or_excluded(rel_p, record_prefixes):
            continue
        parent_rel = candidate.parent.relative_to(repo_root).as_posix()
        entries[cand_resolved] = f"cond:{parent_rel}"

    # E5: 自動メモリの MEMORY.md
    if use_home:
        e5_path = default_memory_index(repo_root)
        if e5_path.is_file():
            entries[e5_path] = "E5"

    # E6: ~/.codex/AGENTS.md と リポジトリルートの AGENTS.md
    # (CODEX_START.md は含めない)
    repo_agents = (repo_root / "AGENTS.md").resolve()
    if repo_agents.is_file():
        entries[repo_agents] = "E6"

    if use_home:
        home_codex_agents = Path("~/.codex/AGENTS.md").expanduser().resolve()
        if home_codex_agents.is_file():
            entries[home_codex_agents] = "E6"

    # 自己検査 (Self-check):
    # 名前パターンに一致するファイルが存在するのに入口集合へ入らなかった場合警告
    # 対象:
    # 1. CLAUDE.md: リポジトリ直下のみ（直下以外は cond:<dir> として回収済み）
    if e1_path.is_file() and e1_path not in entries:
        warnings.append(f"未認識の入口候補: {e1_path}")

    # 2. AGENTS.md: リポジトリ直下と ~/.codex/ のみ
    if repo_agents.is_file() and repo_agents not in entries:
        warnings.append(f"未認識の入口候補: {repo_agents}")
    if use_home:
        home_codex_agents = Path("~/.codex/AGENTS.md").expanduser().resolve()
        if home_codex_agents.is_file() and home_codex_agents not in entries:
            warnings.append(f"未認識の入口候補: {home_codex_agents}")

    # 3. SKILL.md: .claude/skills/ 配下と extra_entries の glob
    # ※リポジトリ直下の skills/*/SKILL.md は通常ノードなので警告しない
    skill_candidates: List[Path] = []
    if claude_skills_dir.is_dir():
        skill_candidates.extend(claude_skills_dir.glob("*/SKILL.md"))
    for g, _tag in extra_globs:
        skill_candidates.extend(repo_root.glob(g))
    if use_home:
        home_claude_skills = Path("~/.claude/skills").expanduser()
        if home_claude_skills.is_dir():
            skill_candidates.extend(home_claude_skills.glob("**/SKILL.md"))

    for sc in skill_candidates:
        sc_res = sc.resolve()
        try:
            sc_rel = sc_res.relative_to(repo_root).as_posix()
            if is_record_or_excluded(sc_rel, record_prefixes):
                continue
        except ValueError:
            pass  # HOME 配下
        if sc_res not in entries:
            warnings.append(f"未認識の入口候補: {sc_res}")

    return entries, warnings


class RepositoryGraph:
    """リポジトリの参照関係グラフと到達可能性インデックス。"""

    def __init__(
        self,
        repo_root: Path,
        record_prefixes: List[str],
        is_fixture: bool = False,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.record_prefixes = record_prefixes
        self.is_fixture = is_fixture

        # プール内のファイル
        # abs_path -> rel_path_posix
        self.all_files: Dict[Path, str] = {}
        # basename -> [abs_path]
        self.basename_map: Dict[str, List[Path]] = collections.defaultdict(list)
        # 孤児の母集団（記録・除外以外のリポジトリ内 .md ファイル）
        self.orphan_population: Set[Path] = set()

        # 辺・言及
        self.edges: List[Edge] = []
        self.adjacency: Dict[Path, List[Edge]] = collections.defaultdict(list)
        self.negative_mentions: List[NegativeMention] = []
        self.broken_refs: List[BrokenRef] = []
        self.ambiguous_refs: List[AmbiguousRef] = []
        self.glob_refs: List[GlobRef] = []

        # コード言及: target_path -> set(code_file_path)
        self.code_mentions: Dict[Path, Set[Path]] = collections.defaultdict(set)

        # 入口情報
        self.entries: Dict[Path, str] = {}
        self.warnings: List[str] = []

        # 到達可能性結果
        # node -> shortest_distance
        self.shortest_distance: Dict[Path, int] = {}
        # node -> shortest_entry
        self.shortest_entry: Dict[Path, Path] = {}
        # node -> set(all_reaching_entries)
        self.reaching_entries: Dict[Path, Set[Path]] = collections.defaultdict(set)
        # node -> shortest_path
        self.shortest_paths: Dict[Path, List[Path]] = {}

    def is_excluded_path(self, path: Path) -> bool:
        """パスが記録・除外対象か判定する。"""
        try:
            rel = path.resolve().relative_to(self.repo_root).as_posix()
            return is_record_or_excluded(rel, self.record_prefixes)
        except ValueError:
            return False

    def build_index(self) -> None:
        """リポジトリ内の全対象ファイルを走査してインデックスを作成する。"""
        ignore_dirs = {".git", ".venv", "__pycache__", ".pytest_cache"}
        symlink_skipped = 0

        for root, dirs, files in os.walk(self.repo_root):
            # 無視ディレクトリをスキップ
            dirs[:] = [d for d in dirs if d not in ignore_dirs]

            rel_root = Path(root).relative_to(self.repo_root).as_posix()
            if rel_root != "." and is_record_or_excluded(rel_root + "/", self.record_prefixes):
                dirs.clear()
                continue

            for f in files:
                file_p = Path(root) / f
                if file_p.is_symlink():
                    symlink_skipped += 1
                    continue
                p = file_p.resolve()
                try:
                    rel_p = p.relative_to(self.repo_root).as_posix()
                except ValueError:
                    continue
                if is_record_or_excluded(rel_p, self.record_prefixes):
                    continue

                ext = p.suffix.lower()
                if ext in KNOWN_EXTENSIONS:
                    self.all_files[p] = rel_p
                    self.basename_map[p.name].append(p)
                    if ext in DOC_EXTENSIONS:
                        self.orphan_population.add(p)

        if symlink_skipped > 0:
            self.warnings.append(f"symlink_skipped: {symlink_skipped}")

    def extract_tokens_from_line(self, line: str) -> List[str]:
        """行からパス風トークンを抽出する。"""
        tokens: List[str] = []

        # 1. バッククォート内の文字列: `...`
        for m in re.finditer(r"`([^`\r\n]+)`", line):
            tokens.append(m.group(1).strip())
        # 注: 1・3で拾った中身は「パスらしさ」の判定を経ていない。
        # 既知拡張子を持つか、スラッシュを含むものだけを残す（後段でフィルタ）。

        # 2. Markdownリンクの URL: [...](url)
        for m in re.finditer(r"\[[^\]\r\n]*\]\(([^)\r\n]+)\)", line):
            url = m.group(1).strip()
            # #heading やクエリパラメータを除去
            url = url.split("#")[0].split("?")[0].strip()
            if url.startswith("file://"):
                url = url[len("file://"):]
            if url:
                tokens.append(url)

        # 3. 括弧内の文字列: (path) または （path）
        for m in re.finditer(r"(?:\(|（)([^()（）\r\n]+)(?:\)|）)", line):
            tokens.append(m.group(1).strip())

        # 4. Claude Code の @import: `@path/to/file` や `@~/.claude/x.md`（先頭または空白の直後）
        # npm の @scope/pkg 等を拾わないよう、既知拡張子で終わるか ~/ ./ / で始まるものだけ
        for m in re.finditer(r"(?:^|(?<=\s))@((?:~|\.{1,2})?/?[\w./\-]+)", line):
            imp = m.group(1)
            if imp.startswith(("~/", "./", "../", "/")) or any(imp.endswith(e) for e in KNOWN_EXTENSIONS):
                tokens.append(imp)

        # 5. 行内の既知拡張子パターン（裸の basename やパス）
        # Unicode 単語構成文字、~、/、.、-、*、? を含む
        ext_pattern = r"(?:~|/|[a-zA-Z0-9_\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef.\-*?/\\])+?\.(?:md|tsv|py|sh|json|toml|plist)\b"
        for m in re.finditer(ext_pattern, line):
            tokens.append(m.group(0).strip())

        # 1・3（バッククォート内・括弧内）は本文の普通の語も拾ってしまう。
        # 「要確認」「評価」「JST」等がパス候補になり、大量の偽の切れ参照を生む。
        # 既知拡張子で終わるか、スラッシュを含むものだけを残す。
        tokens = [
            t for t in tokens
            if "/" in t or any(t.endswith(e) for e in KNOWN_EXTENSIONS)
        ]

        # 6. ディレクトリ参照は v1 では抽出しない。
        # 実測: 切れ参照9,553件の61%が「目的/」「ログ/」「ホーム/記事/マガジン/」といった
        # 日本語本文中のスラッシュだった。ディレクトリ辺から得られる情報より
        # 偽の切れ参照のノイズが大きい。必要になったら境界条件を詰めて再導入する。

        # トークンのクリーンアップ
        cleaned: List[str] = []
        for t in tokens:
            t = t.strip(" \t\r\n\"'`()[]{}（）「」『』,;:")
            if not t:
                continue
            if t.startswith(("http://", "https://", "mailto:", "ftp://")):
                continue
            if any(c in t for c in "<>{}"):  # プレースホルダ
                continue
            if t in ("/", "//", "/api/", "account/"):
                continue
            cleaned.append(t)

        # 重複を排除しつつ順序を保つ
        seen: Set[str] = set()
        res: List[str] = []
        for t in cleaned:
            if t not in seen:
                seen.add(t)
                res.append(t)
        return res

    def resolve_token(
        self,
        token: str,
        source_file: Path,
    ) -> Tuple[Optional[List[Path]], str]:
        """トークンを指定解決順に従って解決する。

        解決順:
        1. 先頭が ~ なら expanduser して絶対パス扱い
        2. 絶対パス
        3. リポジトリ相対
        4. 参照元ファイルからの相対
        5. basename の一意一致

        Returns:
            (resolved_paths, resolution_status)
            status: "resolved", "ambiguous", "broken", "glob", "dir"
        """
        # パスとして成立しないトークンを先に弾く。
        # 本文中の日本語の長文が「.mdで終わるトークン」として抽出されることがあり、
        # そのまま stat すると OSError(63) File name too long になる。
        # NAME_MAX(255バイト)を超える構成要素、または句読点・空白を含むものは
        # パス参照ではないとみなす。
        if len(token.encode("utf-8")) > 255:
            return None, "broken"
        if any(seg.encode("utf-8").__len__() > 255 for seg in token.split("/")):
            return None, "broken"
        if any(ch in token for ch in "、。，．「」『』（）()〔〕 \t"):
            return None, "broken"

        # グロブ文字を含む場合
        if "*" in token or "?" in token:
            return None, "glob"

        # ディレクトリ参照の場合
        if token.endswith("/"):
            target_dir: Optional[Path] = None
            if token.startswith("~/"):
                cand = Path(token).expanduser()
                if cand.is_dir():
                    target_dir = cand
            elif token.startswith("/"):
                cand = Path(token)
                if cand.is_dir():
                    target_dir = cand
            else:
                cand1 = self.repo_root / token
                if cand1.is_dir():
                    target_dir = cand1
                else:
                    cand2 = source_file.parent / token
                    if cand2.is_dir():
                        target_dir = cand2

            if target_dir and target_dir.is_dir():
                # 直下ファイルを走査
                children: List[Path] = []
                try:
                    for item in target_dir.iterdir():
                        if item.is_file() and item.suffix.lower() in KNOWN_EXTENSIONS:
                            item_res = item.resolve()
                            if not self.is_excluded_path(item_res):
                                children.append(item_res)
                except OSError:
                    pass
                if children:
                    return children, "dir"
            return None, "broken"

        # 1. 先頭が ~
        if token.startswith("~/"):
            cand = Path(token).expanduser().resolve()
            if cand.is_file() and not self.is_excluded_path(cand):
                return [cand], "resolved"

        # 2. 絶対パス
        if token.startswith("/"):
            cand = Path(token).resolve()
            if cand.is_file() and not self.is_excluded_path(cand):
                return [cand], "resolved"

        # 3. リポジトリ相対
        cand = (self.repo_root / token).resolve()
        if cand.is_file() and not self.is_excluded_path(cand):
            return [cand], "resolved"

        # 4. 参照元ファイルからの相対
        cand = (source_file.parent / token).resolve()
        if cand.is_file() and not self.is_excluded_path(cand):
            return [cand], "resolved"

        # 5. basename の一致
        bname = Path(token).name
        candidates = [
            c for c in self.basename_map.get(bname, [])
            if not self.is_excluded_path(c)
        ]
        if len(candidates) == 1:
            return candidates, "resolved"
        elif len(candidates) > 1:
            return candidates, "ambiguous"

        return None, "broken"

    def scan_edges_and_mentions(self) -> None:
        """辺およびコード言及の抽出を行う。"""
        # 1. 辺の発生元ファイルの走査
        # md と .claude/ 配下の json・tsv
        source_files: List[Path] = []
        for p, rel in self.all_files.items():
            ext = p.suffix.lower()
            if ext == ".md":
                source_files.append(p)
            elif ext in (".json", ".tsv"):
                if rel.startswith(".claude/") or "/.claude/" in rel:
                    source_files.append(p)

        # HOME 配下の入口ファイルも発生元に含める
        for ep in self.entries:
            if ep not in self.all_files and ep.is_file():
                source_files.append(ep)

        for src in source_files:
            try:
                with open(src, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError:
                continue

            for line_idx, line in enumerate(lines, start=1):
                # 否定語の判定（memcheck の EXEMPTION_WORDS）
                is_negated = any(word in line for word in EXEMPTION_WORDS)

                tokens = self.extract_tokens_from_line(line)
                for t in tokens:
                    resolved_list, status = self.resolve_token(t, src)

                    if status == "glob":
                        self.glob_refs.append(
                            GlobRef(source=src, line_no=line_idx, raw_token=t)
                        )
                    elif status == "broken":
                        self.broken_refs.append(
                            BrokenRef(source=src, line_no=line_idx, raw_token=t)
                        )
                    elif status == "ambiguous":
                        assert resolved_list is not None
                        self.ambiguous_refs.append(
                            AmbiguousRef(
                                source=src,
                                line_no=line_idx,
                                raw_token=t,
                                candidates=resolved_list,
                            )
                        )
                        if is_negated:
                            for target in resolved_list:
                                self.negative_mentions.append(
                                    NegativeMention(
                                        source=src,
                                        target=target,
                                        line_no=line_idx,
                                        raw_token=t,
                                    )
                                )
                        else:
                            for target in resolved_list:
                                edge = Edge(
                                    source=src,
                                    target=target,
                                    tag="ambiguous",
                                    line_no=line_idx,
                                )
                                self.edges.append(edge)
                                self.adjacency[src].append(edge)
                    elif status in ("resolved", "dir"):
                        assert resolved_list is not None
                        tag = "dir" if status == "dir" else "normal"
                        if is_negated:
                            for target in resolved_list:
                                self.negative_mentions.append(
                                    NegativeMention(
                                        source=src,
                                        target=target,
                                        line_no=line_idx,
                                        raw_token=t,
                                    )
                                )
                        else:
                            for target in resolved_list:
                                edge = Edge(
                                    source=src,
                                    target=target,
                                    tag=tag,
                                    line_no=line_idx,
                                )
                                self.edges.append(edge)
                                self.adjacency[src].append(edge)

        # 2. コード言及の走査
        # 対象: 辺抽出の既知拡張子から .md を除いた集合のうち、.claude/ 配下でないもの
        code_files: List[Path] = []
        for p, rel in self.all_files.items():
            ext = p.suffix.lower()
            if ext in CODE_EXTENSIONS:
                if not (rel.startswith(".claude/") or "/.claude/" in rel):
                    code_files.append(p)

        for cp in code_files:
            try:
                with open(cp, "r", encoding="utf-8", errors="replace") as cf:
                    content = cf.read()
            except OSError:
                continue

            # コードファイルからパス風トークンを抽出して対象への言及を記録
            for line_idx, line in enumerate(content.splitlines(), start=1):
                tokens = self.extract_tokens_from_line(line)
                for t in tokens:
                    resolved_list, status = self.resolve_token(t, cp)
                    if status in ("resolved", "ambiguous", "dir") and resolved_list:
                        for target in resolved_list:
                            self.code_mentions[target].add(cp)

    def compute_reachability(self) -> None:
        """全入口ノードから BFS を実行し、到達可能性・最短ホップ・他経路数を計算する。"""
        # 各入口ノード e から個別に BFS を行い、最短経路を追跡
        # entry -> {node: (distance, [path])}
        entry_paths: Dict[Path, Dict[Path, Tuple[int, List[Path]]]] = {}

        for entry_path in self.entries:
            dist_map: Dict[Path, Tuple[int, List[Path]]] = {entry_path: (0, [entry_path])}
            queue = collections.deque([entry_path])

            while queue:
                curr = queue.popleft()
                curr_dist, curr_path = dist_map[curr]

                for edge in self.adjacency.get(curr, []):
                    nxt = edge.target
                    if nxt not in dist_map:
                        dist_map[nxt] = (curr_dist + 1, curr_path + [nxt])
                        queue.append(nxt)

            entry_paths[entry_path] = dist_map

        # 各ノードについて、到達可能な入口集合と最短距離・最短経路を集約
        # 全ノードの集合
        all_graph_nodes: Set[Path] = set(self.all_files.keys()) | set(self.entries.keys())
        for e in self.edges:
            all_graph_nodes.add(e.source)
            all_graph_nodes.add(e.target)

        for node in all_graph_nodes:
            reaching: Set[Path] = set()
            min_dist: Optional[int] = None
            best_entry: Optional[Path] = None
            best_path: List[Path] = []

            best_entry_is_cond = False
            for entry, dist_map in entry_paths.items():
                if node in dist_map:
                    d, p = dist_map[node]
                    reaching.add(entry)
                    is_cond = self.entries.get(entry, "").startswith("cond:")
                    if (
                        min_dist is None
                        or d < min_dist
                        or (d == min_dist and best_entry_is_cond and not is_cond)
                    ):
                        min_dist = d
                        best_entry = entry
                        best_path = p
                        best_entry_is_cond = is_cond

            if reaching and min_dist is not None and best_entry is not None:
                self.reaching_entries[node] = reaching
                self.shortest_distance[node] = min_dist
                self.shortest_entry[node] = best_entry
                self.shortest_paths[node] = best_path

    def get_duplicates(self) -> Dict[str, List[Path]]:
        """到達可能集合の中で同じ basename が複数パス存在するものを検出する。"""
        reachable_nodes = [
            n for n in self.all_files
            if n in self.reaching_entries and self.reaching_entries[n]
        ]
        bmap: Dict[str, List[Path]] = collections.defaultdict(list)
        for n in reachable_nodes:
            bmap[n.name].append(n)

        return {name: paths for name, paths in bmap.items() if len(paths) > 1}

    def get_orphans(self) -> List[Path]:
        """母集団（記録・除外以外のリポジトリ内 .md ファイル）のうち未到達の孤児を取得。"""
        orphans = [
            p for p in self.orphan_population
            if not self.reaching_entries.get(p)
        ]
        return sorted(orphans)


def format_rel_path(path: Path, repo_root: Path) -> str:
    """表示用パス（リポジトリ相対または絶対パス）に整形する。"""
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return str(path.resolve())


def run_query_mode(
    graph: RepositoryGraph,
    query_str: str,
) -> int:
    """照会モード: reachmap.py <path>"""
    repo_root = graph.repo_root
    target_path: Optional[Path] = None

    # 入力指定の解決: 絶対パス、リポジトリ相対、裸の basename
    raw_p = Path(query_str)
    if query_str.startswith("~"):
        cand = raw_p.expanduser().resolve()
        if cand.is_file():
            target_path = cand
    elif raw_p.is_absolute():
        cand = raw_p.resolve()
        if cand.is_file():
            target_path = cand
    else:
        # リポジトリ相対
        cand = (repo_root / query_str).resolve()
        if cand.is_file():
            target_path = cand
        else:
            # basename 検索
            bname = raw_p.name
            candidates = [
                c for c in graph.all_files
                if c.name == bname and not graph.is_excluded_path(c)
            ]
            if len(candidates) == 1:
                target_path = candidates[0]
            elif len(candidates) > 1:
                sys.stderr.write(f"エラー: 指定された basename '{query_str}' は複数の候補が存在します:\n")
                for c in sorted(candidates):
                    sys.stderr.write(f"  - {format_rel_path(c, repo_root)}\n")
                return 1

    if target_path is None or not target_path.is_file():
        sys.stderr.write(f"エラー: 照会対象が存在しません: {query_str}\n")
        return 1

    # 未認識入口候補の警告は標準エラーへ出力（照会結果が得られれば exit 0）
    for w in graph.warnings:
        sys.stderr.write(f"警告: {w}\n")

    display_name = format_rel_path(target_path, repo_root)
    is_reachable = bool(graph.reaching_entries.get(target_path))

    # 否定言及の集計
    neg_mentions = [
        nm for nm in graph.negative_mentions
        if nm.target == target_path
    ]
    neg_count = len(neg_mentions)
    neg_sources = sorted({format_rel_path(nm.source, repo_root) for nm in neg_mentions})
    neg_str = (
        f"{neg_count}({', '.join(neg_sources)})"
        if neg_count > 0
        else "0"
    )

    # コード言及の集計
    code_count = len(graph.code_mentions.get(target_path, set()))

    # 通常入口と条件付き入口の分類
    reaching_set = graph.reaching_entries.get(target_path, set())
    normal_reaching = [
        e for e in reaching_set
        if not graph.entries.get(e, "").startswith("cond:")
    ]
    cond_reaching = [
        e for e in reaching_set
        if graph.entries.get(e, "").startswith("cond:")
    ]

    print(f"対象: {display_name}")

    if normal_reaching:
        dist = graph.shortest_distance[target_path]
        best_entry = graph.shortest_entry[target_path]
        best_entry_display = format_rel_path(best_entry, repo_root)
        other_routes = len(reaching_set) - 1

        if dist == 0:
            hop_str = "hop0 (入口)"
        else:
            hop_str = f"hop{dist} via {best_entry_display}"

        print(f"到達: 可 {hop_str} ／ 他経路 {other_routes} ／ 否定言及 {neg_str}")
    else:
        cond_count = len(cond_reaching)
        print(f"到達: 不可 ／ 否定言及 {neg_str} ／ 条件付き入口経由 {cond_count} ／ コード言及 {code_count}")

    return 0


def run_report_mode(graph: RepositoryGraph) -> int:
    """報告モード: reachmap.py --report"""
    repo_root = graph.repo_root

    # 未認識の入口候補があれば標準エラーに出力
    has_unrecognized = False
    for w in graph.warnings:
        sys.stderr.write(f"警告: {w}\n")
        if "未認識の入口候補" in w:
            has_unrecognized = True

    # 集計
    total_entries = len(graph.entries)
    cond_entries = sum(1 for tag in graph.entries.values() if tag.startswith("cond:"))

    # 到達数と hop 内訳（入口から参照を辿って到達したノード: hop >= 1）
    reachable_nodes = [
        n for n in graph.all_files
        if graph.shortest_distance.get(n, 0) >= 1
    ]
    reachable_total = len(reachable_nodes)
    hop1 = sum(1 for n in reachable_nodes if graph.shortest_distance[n] == 1)
    hop2 = sum(1 for n in reachable_nodes if graph.shortest_distance[n] == 2)
    hop3_plus = sum(1 for n in reachable_nodes if graph.shortest_distance[n] >= 3)

    # 孤児
    orphans = graph.get_orphans()
    orphan_count = len(orphans)

    # 切れ参照・曖昧参照・同名重複
    broken_count = len(graph.broken_refs)
    ambig_count = len(graph.ambiguous_refs)
    duplicates = graph.get_duplicates()
    dup_count = len(duplicates)

    # 孤児の親ディレクトリ分布（上位5件）
    orphan_dirs: Dict[str, int] = collections.defaultdict(int)
    for op in orphans:
        parent_rel = op.parent.relative_to(repo_root).as_posix()
        orphan_dirs[parent_rel] += 1

    top_orphan_dirs = sorted(orphan_dirs.items(), key=lambda x: x[1], reverse=True)[:5]
    dist_str = ", ".join(f"{d} {c}" for d, c in top_orphan_dirs) if top_orphan_dirs else "なし"

    # 詳細 JSON ファイルの作成
    now_ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = Path(f"/tmp/reachmap-{now_ts}.json")

    json_data: Dict[str, Any] = {
        "generated_at": datetime.datetime.now().isoformat(),
        "summary": {
            "entries_total": total_entries,
            "entries_conditional": cond_entries,
            "reachable_total": reachable_total,
            "hop1": hop1,
            "hop2": hop2,
            "hop3_plus": hop3_plus,
            "orphans": orphan_count,
            "broken_refs": broken_count,
            "ambiguous_refs": ambig_count,
            "duplicates": dup_count,
        },
        "entries": [
            {
                "path": format_rel_path(ep, repo_root),
                "tag": tag,
            }
            for ep, tag in graph.entries.items()
        ],
        "reachable": [
            {
                "path": format_rel_path(n, repo_root),
                "shortest_hops": graph.shortest_distance[n],
                "shortest_entry": format_rel_path(graph.shortest_entry[n], repo_root),
                "shortest_path": [
                    format_rel_path(p, repo_root) for p in graph.shortest_paths[n]
                ],
                "all_reaching_entries": [
                    format_rel_path(e, repo_root) for e in graph.reaching_entries[n]
                ],
            }
            for n in reachable_nodes
        ],
        "orphans": [format_rel_path(op, repo_root) for op in orphans],
        "broken_refs": [
            {
                "source": format_rel_path(br.source, repo_root),
                "line_no": br.line_no,
                "token": br.raw_token,
            }
            for br in graph.broken_refs
        ],
        "ambiguous_refs": [
            {
                "source": format_rel_path(ar.source, repo_root),
                "line_no": ar.line_no,
                "token": ar.raw_token,
                "candidates": [
                    format_rel_path(c, repo_root) for c in ar.candidates
                ],
            }
            for ar in graph.ambiguous_refs
        ],
        "duplicates": [
            {
                "basename": name,
                "paths": [format_rel_path(p, repo_root) for p in paths],
            }
            for name, paths in duplicates.items()
        ],
        "negative_mentions": [
            {
                "source": format_rel_path(nm.source, repo_root),
                "target": format_rel_path(nm.target, repo_root),
                "line_no": nm.line_no,
                "token": nm.raw_token,
            }
            for nm in graph.negative_mentions
        ],
        "warnings": graph.warnings,
    }

    try:
        with open(json_path, "w", encoding="utf-8") as jf:
            json.dump(json_data, jf, ensure_ascii=False, indent=2)
    except OSError as e:
        sys.stderr.write(f"エラー: JSONレポート作成に失敗しました: {e}\n")

    # 標準出力（指定フォーマット）
    print(f"入口: {total_entries}（うち条件付き {cond_entries}）")
    print(
        f"到達: {reachable_total}（hop1 {hop1} / hop2 {hop2} / hop3+ {hop3_plus}）  "
        f"孤児: {orphan_count}  切れ参照: {broken_count}  曖昧参照: {ambig_count}  同名重複: {dup_count}"
    )
    print(f"孤児の分布: {dist_str}")
    print(f"一覧: {json_path}")

    # 未認識の入口候補があれば exit 1
    if has_unrecognized:
        return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="reachmap.py: 参照の到達可能性を判定する機械的検証ツール (v1)"
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="照会対象パス（絶対パス・リポジトリ相対・裸の basename）",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="リポジトリ全体の到達可能性サマリを報告する",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("REACHMAP_ROOT"),
        help="対象リポジトリのルート（既定: cwd の git ルート、無ければ cwd）",
    )
    parser.add_argument(
        "--record-paths",
        default=None,
        help="記録・証跡パターンのファイル（1行1接頭辞）。.reachmap.json の record_prefixes と合成",
    )
    parser.add_argument("--no-home", action="store_true", help="HOME 配下（~/.claude 等）を入口に含めない")

    args = parser.parse_args()

    # 使用法チェック
    if not args.report and not args.path:
        parser.print_usage(file=sys.stderr)
        return 2

    repo_root = Path(args.root).resolve() if args.root else (git_toplevel(Path.cwd()) or Path.cwd()).resolve()
    if not repo_root.is_dir():
        sys.stderr.write(f"エラー: リポジトリルートが存在しません: {repo_root}\n")
        return 2

    cfg = load_config(repo_root)
    if args.no_home:
        cfg["home_entries"] = False
    global EXEMPTION_WORDS
    if cfg.get("exemption_words"):
        EXEMPTION_WORDS = tuple(cfg["exemption_words"])

    # 記録・証跡パターン: .reachmap.json の record_prefixes ＋ record_paths.txt（--record-paths か repo 直下）
    record_prefixes: List[str] = list(cfg.get("record_prefixes", []))
    record_paths_file = Path(args.record_paths).resolve() if args.record_paths else repo_root / "record_paths.txt"
    if record_paths_file.is_file():
        record_prefixes += [p for p in load_record_prefixes(record_paths_file) if p not in record_prefixes]

    # グラフの初期化とインデックス構築
    graph = RepositoryGraph(
        repo_root=repo_root,
        record_prefixes=record_prefixes,
        is_fixture=not cfg["home_entries"],
    )
    graph.build_index()

    # 入口の導出
    entries, warnings = discover_entries(repo_root, cfg, record_prefixes)
    graph.entries = entries
    graph.warnings = warnings + graph.warnings

    if len(entries) == 0:
        sys.stderr.write("エラー: 入口が0件です。\n")
        return 2

    # 辺・言及の抽出と到達可能性の計算
    graph.scan_edges_and_mentions()
    graph.compute_reachability()

    if args.report:
        return run_report_mode(graph)
    else:
        return run_query_mode(graph, args.path)


if __name__ == "__main__":
    sys.exit(main())
