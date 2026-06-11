#!/usr/bin/env python3
"""Select keynote guest speakers from a full Bloomberg China Show transcript."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plan_speaker_highlights import ask_deepseek


ANCHOR_OR_REPORTER_HINTS = {
    "yvonne man",
    "david ingles",
    "annabelle droulers",
    "stephen engle",
    "haidi stroud-watts",
    "paul allen",
    "bloomberg",
    "anchor",
    "host",
    "reporter",
    "correspondent",
}


@dataclass
class Candidate:
    speaker: str
    context: str
    start: float
    end: float
    confidence: float
    importance: float
    reason: str

    @property
    def duration(self) -> float:
        return self.end - self.start


def format_time(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes:02d}:{secs:05.2f}"


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def prompt_lines(segments: list[dict[str, Any]], start: float, end: float) -> str:
    lines: list[str] = []
    for seg in segments:
        seg_start = float(seg["start"])
        seg_end = float(seg["end"])
        if seg_end < start or seg_start > end:
            continue
        text = clean_text(str(seg.get("text", "")))
        if len(text) > 220:
            text = text[:217] + "..."
        lines.append(f"{format_time(seg_start)}-{format_time(seg_end)} {text}")
    return "\n".join(lines)


def find_candidates_in_window(
    api_key: str,
    segments: list[dict[str, Any]],
    start: float,
    end: float,
) -> list[Candidate]:
    transcript = prompt_lines(segments, start, end)
    if not transcript:
        return []

    system_prompt = (
        "You are a Bloomberg TV segment producer. Identify substantive guest/keynote interview segments "
        "from an automatic transcript. Return strict JSON only."
    )
    user_prompt = f"""Transcript window: {format_time(start)}-{format_time(end)}

{transcript}

Task:
Identify non-anchor guest/keynote speakers interviewed in this window.

Include:
- CEOs, founders, economists, strategists, analysts, portfolio managers, policymakers, academics.
- The full interview exchange, including host questions directly attached to the guest answers.

Exclude:
- Bloomberg anchors, hosts, reporters, correspondents, market-board updates, headlines, weather/traffic, teasers, and transitions.
- Segments shorter than 60 seconds.

Return JSON:
{{
  "candidates": [
    {{
      "speaker": "Guest full name",
      "context": "Organization and role if inferable",
      "start": <absolute seconds from video start>,
      "end": <absolute seconds from video start>,
      "confidence": 0.0,
      "importance": 0.0,
      "reason": "why this guest is worth clipping"
    }}
  ]
}}

If there are no real guest/keynote interview segments, return {{"candidates": []}}.
"""
    result = ask_deepseek(api_key, system_prompt, user_prompt, temperature=0.2)
    candidates: list[Candidate] = []
    for item in result.get("candidates", []):
        try:
            speaker = clean_text(str(item["speaker"]))
            context = clean_text(str(item.get("context", "")))
            cand_start = max(start, float(item["start"]))
            cand_end = min(end, float(item["end"]))
            confidence = float(item.get("confidence", 0.5))
            importance = float(item.get("importance", 0.5))
            reason = clean_text(str(item.get("reason", "")))
        except (KeyError, TypeError, ValueError):
            continue
        if not speaker or cand_end - cand_start < 60:
            continue
        if is_anchor_or_reporter(speaker, context):
            continue
        candidates.append(Candidate(speaker, context, cand_start, cand_end, confidence, importance, reason))
    return candidates


def is_anchor_or_reporter(speaker: str, context: str) -> bool:
    text = f"{speaker} {context}".lower()
    return any(hint in text for hint in ANCHOR_OR_REPORTER_HINTS)


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def consolidate(candidates: list[Candidate]) -> list[Candidate]:
    grouped: dict[str, Candidate] = {}
    for cand in sorted(candidates, key=lambda item: (normalize_name(item.speaker), item.start)):
        key = normalize_name(cand.speaker)
        if not key:
            continue
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = cand
            continue
        if cand.start <= existing.end + 180:
            existing.start = min(existing.start, cand.start)
            existing.end = max(existing.end, cand.end)
            existing.confidence = max(existing.confidence, cand.confidence)
            existing.importance = max(existing.importance, cand.importance)
            if len(cand.context) > len(existing.context):
                existing.context = cand.context
            if cand.reason and cand.reason not in existing.reason:
                existing.reason = clean_text(f"{existing.reason}; {cand.reason}".strip("; "))
        elif score(cand) > score(existing):
            grouped[key] = cand
    return list(grouped.values())


def score(candidate: Candidate) -> float:
    duration_score = min(candidate.duration / 600.0, 1.0)
    return candidate.importance * 2.0 + candidate.confidence + duration_score


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--show-date", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-speakers", type=int, default=3)
    parser.add_argument("--window-seconds", type=int, default=720)
    parser.add_argument("--overlap-seconds", type=int, default=90)
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")

    data = json.loads(args.transcript.read_text(encoding="utf-8"))
    segments = data.get("segments", [])
    if not segments:
        raise SystemExit("No transcript segments found")

    duration = max(float(seg["end"]) for seg in segments)
    candidates: list[Candidate] = []
    start = 0.0
    while start < duration:
        end = min(duration, start + args.window_seconds)
        print(f"Selecting speakers in {format_time(start)}-{format_time(end)}", flush=True)
        candidates.extend(find_candidates_in_window(api_key, segments, start, end))
        if end >= duration:
            break
        start = max(0.0, end - args.overlap_seconds)

    selected = sorted(consolidate(candidates), key=score, reverse=True)[: args.max_speakers]
    payload = {
        "show_date": args.show_date,
        "source_transcript": str(args.transcript),
        "max_speakers": args.max_speakers,
        "speakers": [
            {
                "speaker": item.speaker,
                "speaker_context": item.context,
                "segment_start": round(item.start, 2),
                "segment_end": round(item.end, 2),
                "duration": round(item.duration, 2),
                "confidence": item.confidence,
                "importance": item.importance,
                "score": round(score(item), 3),
                "reason": item.reason,
            }
            for item in selected
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Selected {len(selected)} speaker(s): {args.out}", flush=True)
    for item in selected:
        print(f"  {item.speaker}: {format_time(item.start)}-{format_time(item.end)} {item.context}", flush=True)

    if not selected:
        raise SystemExit("No keynote speakers selected")


if __name__ == "__main__":
    main()
