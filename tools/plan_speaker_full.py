#!/usr/bin/env python3
"""Build a one-clip bilingual plan for a full speaker segment."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plan_speaker_highlights import ask_deepseek


SENSITIVE_REPLACEMENTS = [
    ("投资者", "市场参与者"),
    ("投资主题", "主线"),
    ("投资", "**"),
    ("股票", "权益资产"),
    ("股市", "市场"),
    ("港股", "香港市场"),
    ("A股", "内地市场"),
    ("A 股", "内地市场"),
    ("美股", "美国市场"),
]
TRANSLATION_RETRIES = 3


@dataclass
class SubtitleUnit:
    index: int
    start: float
    end: float
    en: str


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def safe_zh(text: str) -> str:
    text = clean_text(text)
    for old, new in SENSITIVE_REPLACEMENTS:
        text = text.replace(old, new)
    return text


def load_transcript_units(
    transcript: Path,
    segment_start: float,
    segment_end: float,
    *,
    min_seconds: float,
    max_seconds: float,
    max_chars: int,
) -> list[SubtitleUnit]:
    data = json.loads(transcript.read_text(encoding="utf-8"))
    raw_segments = [
        seg for seg in data.get("segments", [])
        if float(seg.get("end", 0)) > segment_start and float(seg.get("start", 0)) < segment_end
    ]
    if not raw_segments:
        raise SystemExit("No transcript segments in the given range")

    units: list[SubtitleUnit] = []
    current_start: float | None = None
    current_end = 0.0
    current_text: list[str] = []

    def flush() -> None:
        nonlocal current_start, current_end, current_text
        if current_start is None or not current_text:
            return
        text = clean_text(" ".join(current_text))
        if text:
            units.append(SubtitleUnit(len(units) + 1, current_start, current_end, text))
        current_start = None
        current_end = 0.0
        current_text = []

    for seg in raw_segments:
        start = max(segment_start, float(seg["start"]))
        end = min(segment_end, float(seg["end"]))
        text = clean_text(str(seg.get("text", "")))
        if end <= start or not text:
            continue
        if current_start is None:
            current_start = start
        current_end = end
        current_text.append(text)
        duration = current_end - current_start
        chars = len(" ".join(current_text))
        ends_sentence = text.endswith((".", "?", "!", "。", "？", "！"))
        if duration >= max_seconds or chars >= max_chars or (duration >= min_seconds and ends_sentence):
            flush()

    flush()
    return units


def translate_batch(api_key: str, units: list[SubtitleUnit], speaker: str, context: str) -> dict[int, dict[str, Any]]:
    system_prompt = (
        "You translate English interview subtitles into natural, concise Simplified Chinese for a finance short video. "
        "Return strict JSON only. Preserve meaning, avoid over-literal wording, and keep each Chinese subtitle readable. "
        "Avoid sensitive Chinese words by rephrasing 投资/股票/A股/港股 as 市场参与者/权益资产/内地市场/香港市场 when needed."
    )
    payload = [
        {"index": unit.index, "start": unit.start, "end": unit.end, "en": unit.en}
        for unit in units
    ]
    user_prompt = f"""Speaker: {speaker}
Context: {context}

Translate these subtitle units:
{json.dumps(payload, ensure_ascii=False, indent=2)}

Return JSON:
{{
  "items": [
    {{
      "index": 1,
      "zh": "自然中文翻译",
      "zh_highlights": ["1-2个中文关键词"],
      "en_highlights": ["1-2 short English phrases copied from en"]
    }}
  ]
}}

Rules:
- Keep zh concise enough to fit 2-3 subtitle lines.
- zh_highlights must be exact substrings of zh.
- en_highlights must be exact substrings of en.
- Do not add commentary or markdown.
"""
    result = ask_deepseek(api_key, system_prompt, user_prompt, temperature=0.2)
    items = result.get("items", [])
    translated: dict[int, dict[str, Any]] = {}
    for item in items:
        try:
            idx = int(item["index"])
        except (KeyError, TypeError, ValueError):
            continue
        translated[idx] = item
    return translated


def translate_units(units: list[SubtitleUnit], speaker: str, context: str, batch_size: int) -> dict[int, dict[str, Any]]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")

    translated: dict[int, dict[str, Any]] = {}
    for start in range(0, len(units), batch_size):
        batch = units[start:start + batch_size]
        print(f"Translating subtitles {batch[0].index}-{batch[-1].index}", flush=True)
        translated.update(translate_batch(api_key, batch, speaker, context))

    missing = [unit for unit in units if not has_zh_translation(translated.get(unit.index))]
    if missing:
        print(
            "Retrying missing subtitle translations: "
            + ",".join(str(unit.index) for unit in missing),
            flush=True,
        )
    for unit in missing:
        for attempt in range(1, TRANSLATION_RETRIES + 1):
            print(f"  Retry subtitle {unit.index} attempt {attempt}", flush=True)
            retry = translate_batch(api_key, [unit], speaker, context)
            item = retry.get(unit.index)
            if has_zh_translation(item):
                translated[unit.index] = item
                break
    return translated


def has_zh_translation(item: dict[str, Any] | None) -> bool:
    if not item:
        return False
    return bool(clean_text(str(item.get("zh", ""))))


def normalize_highlights(value: Any, text: str, limit: int = 2) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value:
        phrase = clean_text(str(raw))
        if phrase and phrase in text and phrase not in result:
            result.append(phrase)
        if len(result) >= limit:
            break
    return result


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    units = load_transcript_units(
        args.transcript,
        args.segment_start,
        args.segment_end,
        min_seconds=args.subtitle_min_seconds,
        max_seconds=args.subtitle_max_seconds,
        max_chars=args.subtitle_max_chars,
    )
    print(f"Built {len(units)} subtitle units", flush=True)
    translations = translate_units(units, args.speaker, args.speaker_context, args.batch_size)

    subtitles: list[dict[str, Any]] = []
    for unit in units:
        item = translations.get(unit.index, {})
        zh = safe_zh(str(item.get("zh", "")))
        if not zh:
            raise SystemExit(f"Missing Chinese translation for subtitle {unit.index}")
        en = clean_text(unit.en)
        subtitles.append({
            "index": unit.index,
            "start": unit.start,
            "end": unit.end,
            "relative_start": unit.start - args.segment_start,
            "relative_end": unit.end - args.segment_start,
            "en": en,
            "zh": zh,
            "zh_highlights": normalize_highlights(item.get("zh_highlights"), zh),
            "en_highlights": normalize_highlights(item.get("en_highlights"), en),
        })

    title_lines = [args.title_line1, args.title_line2, args.title_line3]
    title = args.title or f"{args.title_line1}：{args.title_line2}"
    return {
        "speaker": args.speaker,
        "speaker_context": args.speaker_context,
        "source_transcript": str(args.transcript),
        "segment_range": [args.segment_start, args.segment_end],
        "duration": args.segment_end - args.segment_start,
        "clips": [
            {
                "start": args.segment_start,
                "end": args.segment_end,
                "speaker": args.speaker,
                "title": title,
                "title_lines": title_lines,
                "title_highlights": [item.strip() for item in args.title_highlights.split(",") if item.strip()],
                "subtitles": subtitles,
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--speaker", required=True)
    parser.add_argument("--speaker-context", default="")
    parser.add_argument("--segment-start", type=float, required=True)
    parser.add_argument("--segment-end", type=float, required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--title-line1", default="高盛王逸")
    parser.add_argument("--title-line2", default="完整版")
    parser.add_argument("--title-line3", default="中国楼市复苏路径")
    parser.add_argument("--title-highlights", default="完整版,楼市复苏")
    parser.add_argument("--subtitle-min-seconds", type=float, default=3.0)
    parser.add_argument("--subtitle-max-seconds", type=float, default=7.5)
    parser.add_argument("--subtitle-max-chars", type=int, default=220)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    plan = build_plan(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote full plan: {args.out}", flush=True)


if __name__ == "__main__":
    main()
