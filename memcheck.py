#!/usr/bin/env python3
"""memcheck.py: memory ディレクトリの機械的陳腐化検査スクリプト。

Python 3 標準ライブラリのみを使用し、memory 配下の markdown ファイルを検査する。
問題がなければ exit 0、検出があれば内容を表示して exit 1 とする。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# 対象パス。main() で --repo / --memory-dir / <repo>/.memcheck.json から確定する。
REPO_ROOT = Path.cwd()
MEMORY_DIR = Path()
MEMORY_INDEX_FILE = Path()
CONFIG_NAME = ".memcheck.json"

# 検査4の検出語（退役した正本の名前など）。<repo>/.memcheck.json の deprecated_terms で与える。
DEPRECATED_TERMS: tuple[str, ...] = ()
EXEMPTION_WORDS: tuple[str, ...] = (
    "退役",
    "旧",
    "archive",
    "統合元",
    "使わない",
    "参照しない",
)


class DeprecatedTermHit(NamedTuple):
    file_path: Path
    line_number: int
    snippet: str
    term: str


class PathHit(NamedTuple):
    file_path: Path
    line_number: int
    raw_path: str
    resolved_path: Path


class WikiLinkHit(NamedTuple):
    file_path: Path
    line_number: int
    raw_link: str
    target_file: str


def is_candidate_path(token: str) -> bool:
    """バッククォート内の文字列が検査対象のパスらしいかを判定する。"""
    token = token.strip()
    if not token:
        return False

    # * や ? を含むものは判定不能としてスキップ
    if "*" in token or "?" in token:
        return False

    # スラッシュを含まないものは除外
    if "/" not in token:
        return False

    # /api/ で始まるものは除外
    if token.startswith("/api/"):
        return False

    # account/ で始まるものは除外
    if token.startswith("account/"):
        return False

    # URL スキーム等は除外
    if "://" in token:
        return False

    # 拡張子も / も持たない語は除外
    # （例: ディレクトリなら末尾 /、ファイルなら拡張子を持つ）
    path_obj = Path(token)
    has_extension = bool(path_obj.suffix)
    has_trailing_slash = token.endswith("/")
    if not has_extension and not has_trailing_slash:
        return False

    return True


def resolve_path(token: str) -> Path:
    """指定ルールに従ってパスを解決する。"""
    token = token.strip()
    if token.startswith("~"):
        return Path(token).expanduser()
    if token.startswith("/"):
        return Path(token)
    return REPO_ROOT / token


def check_paths(files: list[Path]) -> list[PathHit]:
    """1. 存在しないパスの参照を検査する。"""
    hits: list[PathHit] = []
    backtick_pattern = re.compile(r"`([^`\n]+)`")

    for file_path in sorted(files):
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError:
            continue

        for line_idx, line in enumerate(content.splitlines(), start=1):
            for match in backtick_pattern.finditer(line):
                token = match.group(1).strip()
                # プレースホルダ（<案件>・{a,b}）と、文中の相対断片（projects/・raw/ 等）は
                # パス参照ではないため除外する。後者は先頭要素がリポジトリ直下に無いもの。
                if any(c in token for c in "<>{}"):
                    continue
                if not token.startswith(("/", "~", ".")) and "/" in token:
                    head = token.split("/", 1)[0]
                    if not (REPO_ROOT / head).exists():
                        continue
                if not is_candidate_path(token):
                    continue
                resolved = resolve_path(token)
                try:
                    exists = resolved.exists()
                except OSError:
                    exists = False

                if not exists:
                    hits.append(
                        PathHit(
                            file_path=file_path,
                            line_number=line_idx,
                            raw_path=token,
                            resolved_path=resolved,
                        )
                    )
    return hits


def check_wiki_links(files: list[Path]) -> list[WikiLinkHit]:
    """2. 切れた wiki link ([[...]]) を検査する。"""
    hits: list[WikiLinkHit] = []
    wiki_pattern = re.compile(r"\[\[([^\]\n]+)\]\]")

    for file_path in sorted(files):
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError:
            continue

        for line_idx, line in enumerate(content.splitlines(), start=1):
            for match in wiki_pattern.finditer(line):
                raw_target = match.group(1).strip()
                # パイプ (|) や見出しリンク (#) を除去してターゲットファイル名を正規化
                link_name = raw_target.split("|")[0].split("#")[0].strip()
                if not link_name:
                    continue

                target_filename = f"{link_name}.md" if not link_name.endswith(".md") else link_name
                target_file = MEMORY_DIR / target_filename
                try:
                    exists = target_file.exists()
                except OSError:
                    exists = False

                if not exists:
                    hits.append(
                        WikiLinkHit(
                            file_path=file_path,
                            line_number=line_idx,
                            raw_link=raw_target,
                            target_file=target_filename,
                        )
                    )
    return hits


def check_index_mismatch(files: list[Path]) -> tuple[list[str], list[str]]:
    """3. 索引 (MEMORY.md) と実体ファイルの不一致を検査する。

    Returns:
        (索引にあるが実体がないリスト, 実体はあるが索引にないリスト)
    """
    if not MEMORY_INDEX_FILE.is_file():
        return (["MEMORY.md (索引ファイル自体が存在しません)"], [])

    try:
        content = MEMORY_INDEX_FILE.read_text(encoding="utf-8")
    except OSError:
        return (["MEMORY.md の読み込みに失敗しました"], [])

    # ](ファイル名.md) を抽出
    link_pattern = re.compile(r"\]\(([^)\n]+\.md)\)")
    indexed_entries: set[str] = set()
    for match in link_pattern.finditer(content):
        entry_path = match.group(1).strip()
        # ファイル名のみをキーとして扱う
        entry_name = Path(entry_path).name
        indexed_entries.add(entry_name)

    # 実体集合 (MEMORY.md を除く *.md)
    entity_files: set[str] = {
        f.name for f in files if f.name != "MEMORY.md"
    }

    missing_in_disk = sorted(indexed_entries - entity_files)
    missing_in_index = sorted(entity_files - indexed_entries)

    return missing_in_disk, missing_in_index


def check_deprecated_terms(files: list[Path]) -> list[DeprecatedTermHit]:
    """4. 退役語を「正本」として参照する記述を検査する。"""
    hits: list[DeprecatedTermHit] = []

    for file_path in sorted(files):
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError:
            continue

        for line_idx, line in enumerate(content.splitlines(), start=1):
            # 免責語が同一行に含まれていればスキップ
            if any(word in line for word in EXEMPTION_WORDS):
                continue

            for term in DEPRECATED_TERMS:
                if term in line:
                    snippet = line[:120]
                    hits.append(
                        DeprecatedTermHit(
                            file_path=file_path,
                            line_number=line_idx,
                            snippet=snippet,
                            term=term,
                        )
                    )
    return hits


def git_toplevel(start: Path) -> Path | None:
    r = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def default_memory_dir(repo: Path) -> Path:
    """Claude Code の自動メモリの既定位置: ~/.claude/projects/<cwdの / を - に置換>/memory"""
    encoded = str(repo.resolve()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded / "memory"


def configure(repo: Path | None, memory_dir: Path | None) -> None:
    global REPO_ROOT, MEMORY_DIR, MEMORY_INDEX_FILE, DEPRECATED_TERMS, EXEMPTION_WORDS
    REPO_ROOT = (repo or git_toplevel(Path.cwd()) or Path.cwd()).resolve()
    MEMORY_DIR = (memory_dir or default_memory_dir(REPO_ROOT)).expanduser()
    MEMORY_INDEX_FILE = MEMORY_DIR / "MEMORY.md"
    cfg = REPO_ROOT / CONFIG_NAME
    if cfg.is_file():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        DEPRECATED_TERMS = tuple(data.get("deprecated_terms", ()))
        if data.get("exemption_words"):
            EXEMPTION_WORDS = tuple(data["exemption_words"])
        if data.get("memory_dir") and not memory_dir:
            MEMORY_DIR = Path(data["memory_dir"]).expanduser()
            MEMORY_INDEX_FILE = MEMORY_DIR / "MEMORY.md"


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude Code の memory ディレクトリの陳腐化検査（存在しないパス・索引不一致・退役語）")
    ap.add_argument("--repo", type=Path, help="相対パスの解決基準（既定: cwd の git ルート）")
    ap.add_argument("--memory-dir", type=Path, help="memory ディレクトリ（既定: ~/.claude/projects/<repo>/memory）")
    a = ap.parse_args()
    configure(a.repo, a.memory_dir)
    if not MEMORY_DIR.is_dir():
        print(f"エラー: 対象ディレクトリが存在しません: {MEMORY_DIR}", file=sys.stderr)
        return 1

    all_md_files = sorted(MEMORY_DIR.glob("*.md"))

    path_hits = check_paths(all_md_files)
    wiki_hits = check_wiki_links(all_md_files)
    missing_in_disk, missing_in_index = check_index_mismatch(all_md_files)
    deprecated_hits = check_deprecated_terms(all_md_files)

    # 未作成の [[link]] は運用上エラーではない（将来書くべき印）。件数に数えない。
    total_issues = (
        len(path_hits)
        + len(missing_in_disk)
        + len(missing_in_index)
        + len(deprecated_hits)
    )

    if total_issues == 0:
        print(f"memcheck: OK（{len(all_md_files)}ファイル）")
        return 0

    print(f"memcheck: {total_issues} 件の不整合を検出しました（対象: {len(all_md_files)}ファイル）\n")

    if path_hits:
        print("## 1. 存在しないパスの参照")
        for hit in path_hits:
            print(
                f"- {hit.file_path.name}:{hit.line_number} -> `{hit.raw_path}` (解決先: {hit.resolved_path})"
            )
        print()

    if wiki_hits:
        print("## 参考: 実体のない wiki link（エラーではない。将来書くべき印）")
        for hit in wiki_hits:
            print(
                f"- {hit.file_path.name}:{hit.line_number} -> [[{hit.raw_link}]] (実体未検出: {hit.target_file})"
            )
        print()

    if missing_in_disk or missing_in_index:
        print("## 3. 索引と実体の不一致")
        if missing_in_disk:
            print("  [索引にあるが実体ファイルが存在しない]")
            for name in missing_in_disk:
                print(f"  - {name}")
        if missing_in_index:
            print("  [実体ファイルが存在するが索引 (MEMORY.md) に未記載]")
            for name in missing_in_index:
                print(f"  - {name}")
        print()

    if deprecated_hits:
        print("## 4. 退役語の正本参照記述")
        for hit in deprecated_hits:
            print(
                f"- {hit.file_path.name}:{hit.line_number} [{hit.term}]\n    {hit.snippet}"
            )
        print()

    return 1


if __name__ == "__main__":
    sys.exit(main())
