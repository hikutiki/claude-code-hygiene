#!/usr/bin/env python3

import json
import sys


QUESTION_PHRASES = (
    "どうしますか",
    "よろしいですか",
    "進めてよいですか",
    "承認をお願いします",
    "教えてください",
    "確認してください",
    "ご確認ください",
    "判断をお願いします",
    "指示をお願いします",
    "どちらが",
    "選んでください",
    "決めてください",
    "でしょうか",
    "いかがでしょうか",
    "指示はありますか",
)

PROGRESS_PHRASES = (
    "実行中です",
    "進行中です",
    "バックグラウンドで",
    "並行して",
    "着手します",
    "引き続き",
    "監視します",
    "完了次第報告",
    "完了したらお知らせ",
    "完了通知が届き次第",
    "待ちます",
    "待機します",
    "作業を続け",
    "継続します",
)


def result(category, text=""):
    snippet = text[:120].replace("\r", " ").replace("\n", " ")
    return {"category": category, "snippet": snippet}


def assistant_text(record):
    if record.get("type") != "assistant":
        return None

    message = record.get("message")
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = record.get("content")

    if not isinstance(content, list):
        return ""

    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text", ""), str)
    )


def classify(transcript_path):
    with open(transcript_path, "r", encoding="utf-8") as transcript:
        records = transcript.readlines()

    text_parts = []
    for line in reversed(records):
        if not line.strip():
            continue
        record = json.loads(line)
        # 応答の後ろに書かれるシステム系レコード（stop_hook_summary・turn_duration・last-prompt 等）は読み飛ばす
        if record.get("type") not in ("assistant", "user"):
            continue
        text = assistant_text(record)
        if text is None:
            break
        text_parts.append(text)

    text = "".join(reversed(text_parts))
    if not text:
        return result("done")

    if text.lstrip().startswith("〔keepalive〕"):
        return result("silent", text)

    if text.rstrip()[-200:].endswith(("?", "？")) or any(
        phrase in text for phrase in QUESTION_PHRASES
    ):
        return result("question", text)

    if any(phrase in text for phrase in PROGRESS_PHRASES):
        return result("progress", text)

    return result("done", text)


def main():
    if len(sys.argv) < 2:
        print(json.dumps(result("done"), ensure_ascii=False))
        return

    try:
        classification = classify(sys.argv[1])
    except Exception:
        classification = result("done")

    print(json.dumps(classification, ensure_ascii=False))


if __name__ == "__main__":
    main()
