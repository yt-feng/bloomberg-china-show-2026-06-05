#!/usr/bin/env python3
"""Generate a highlight_plan.json for a speaker segment using DeepSeek.

Outputs the exact format expected by render_clips_linux.py:
- clips[].start, end, title, title_lines, title_highlights
- clips[].subtitles[].start, end, relative_start, relative_end, zh, en, zh_highlights, en_highlights
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
HOST_OUTRO_PATTERNS = [
    r"\bthank you\b",
    r"\bleave it there\b",
    r"\bhere'?s a look\b",
    r"\basian markets\b",
    r"\bmarkets are doing\b",
    r"\bas we head into\b",
    r"到此为止",
    r"非常感谢",
    r"亚洲市场",
    r"开盘情况",
]


def ask_deepseek(api_key: str, system_prompt: str, user_prompt: str, temperature: float = 0.3) -> dict:
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload).encode()
    req = Request(
        DEEPSEEK_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    for attempt in range(3):
        try:
            with urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"]
            return json.loads(content)
        except (HTTPError, URLError, json.JSONDecodeError, KeyError) as exc:
            print(f"  DeepSeek attempt {attempt + 1} failed: {exc}", flush=True)
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
            else:
                raise SystemExit(f"DeepSeek API failed: {exc}")
    return {}


def format_time(seconds: float) -> str:
    m = int(seconds // 60)
    s = seconds % 60
    return f"{m:02d}:{s:05.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=Path, required=True, help="Transcript JSON file")
    parser.add_argument("--speaker", type=str, required=True)
    parser.add_argument("--speaker-context", type=str, default="")
    parser.add_argument("--segment-start", type=float, required=True)
    parser.add_argument("--segment-end", type=float, required=True)
    parser.add_argument("--min-seconds", type=int, default=20)
    parser.add_argument("--max-seconds", type=int, default=90)
    parser.add_argument("--min-clips", type=int, default=0, help="Preferred minimum number of clips; 0 disables.")
    parser.add_argument("--max-clips", type=int, default=0, help="Maximum number of clips to keep; 0 disables.")
    parser.add_argument("--out", type=Path, required=True, help="Output highlight_plan.json path")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.out.exists() and not args.force:
        print(f"Using cached plan: {args.out}", flush=True)
        return

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")

    # Load transcript and filter to segment range
    data = json.loads(args.transcript.read_text(encoding="utf-8"))
    all_segments = data.get("segments", [])
    segments = [
        s for s in all_segments
        if float(s["end"]) >= args.segment_start and float(s["start"]) <= args.segment_end
    ]
    if not segments:
        raise SystemExit("No transcript segments in the given range")

    transcript_lines = []
    for s in segments:
        transcript_lines.append(f"{s['start']:.1f}-{s['end']:.1f}: {s['text'].strip()}")
    transcript_text = "\n".join(transcript_lines)

    segment_duration = args.segment_end - args.segment_start
    print(f"Planning for {args.speaker}: {args.segment_start:.0f}s-{args.segment_end:.0f}s ({segment_duration:.0f}s)", flush=True)
    print(f"Transcript segments in range: {len(segments)}", flush=True)

    # Step 1: Ask DeepSeek to split into clips with bilingual subtitles
    system_prompt = (
        "You are a senior Chinese short-video editor. Your task is to split an interview segment "
        "into short clips and provide bilingual subtitles with keyword highlights. "
        "Return strict JSON only. All Chinese text must avoid sensitive words: "
        "replace 投资 with ** or rephrase, replace 股票/A股/港股 with 权益资产/内地市场/香港市场."
    )

    user_prompt = f"""Speaker: {args.speaker}
Context: {args.speaker_context}
Segment: {args.segment_start:.1f}s - {args.segment_end:.1f}s (total {segment_duration:.0f}s)

Transcript:
{transcript_text}

---

Task:
1. Select the strongest short-video highlight clips of {args.min_seconds}-{args.max_seconds} seconds each. Each clip should start/end at natural topic boundaries.
2. For each clip, provide:
   - A Chinese title (机构/嘉宾身份 + 热点事件 + hook结构, 例如 "高盛王逸：5万亿城市更新，会拖住楼市吗？")
   - title_lines: split the title into exactly 3 short lines for display (line1=机构嘉宾, line2=主题关键词, line3=hook/观点)
   - title_highlights: 2-3 keywords from title_lines to highlight in yellow
   - Bilingual subtitles covering the full clip duration

3. For each subtitle entry:
   - Provide the original English text
   - Provide a natural Chinese translation (avoid 投资/股 words)
   - zh_highlights: 1-2 key Chinese phrases to highlight yellow
   - en_highlights: 1-2 key English phrases to highlight yellow
   - Each subtitle should be 3-8 seconds long

Return JSON:
{{
  "clips": [
    {{
      "start": <absolute seconds>,
      "end": <absolute seconds>,
      "speaker": "{args.speaker}",
      "title": "<full Chinese title with colon>",
      "title_lines": ["<line1>", "<line2>", "<line3>"],
      "title_highlights": ["<keyword1>", "<keyword2>"],
      "subtitles": [
        {{
          "index": 1,
          "start": <absolute seconds>,
          "end": <absolute seconds>,
          "en": "<English text>",
          "zh": "<Chinese translation>",
          "zh_highlights": ["<phrase>"],
          "en_highlights": ["<phrase>"]
        }}
      ]
    }}
  ]
}}

Important:
- Timestamps are ABSOLUTE (from video start, not segment start)
- Subtitles must cover the full clip without gaps
- Return {clip_count_rule(args.min_clips, args.max_clips)}. If the interview genuinely cannot support the minimum without filler, return fewer high-quality clips.
- Each clip must have at least 3 subtitles
- Every clip must contain a substantive answer from {args.speaker}. A host question is allowed only if the speaker answer follows in the same clip.
- Do not create clips from host outros, thank-you lines, post-interview market recaps, market open boards, or transitions to the next segment. If the provided range includes that material, stop before it and return fewer clips.
- Do not reuse the same subtitle text in multiple clips unless it genuinely appears twice in the source transcript.
- Chinese titles must have hook/conflict angle, not flat descriptions
- Avoid: 投资, 股票, A股, 港股, 美股 in Chinese text
"""

    print("Requesting clip plan from DeepSeek...", flush=True)
    result = ask_deepseek(api_key, system_prompt, user_prompt)
    clips = result.get("clips", [])

    if not clips:
        raise SystemExit("DeepSeek returned no clips")

    clips = postprocess_clips(
        clips,
        args.segment_start,
        args.segment_end,
        args.min_seconds,
        args.max_seconds,
        args.max_clips,
    )
    if not clips:
        raise SystemExit("All generated clips failed the speaker-content quality gate")

    # Write output
    payload = {
        "speaker": args.speaker,
        "speaker_context": args.speaker_context,
        "source_transcript": str(args.transcript),
        "segment_range": [args.segment_start, args.segment_end],
        "duration": segment_duration,
        "clips": clips,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote plan: {args.out} ({len(clips)} clips)", flush=True)


def clip_count_rule(min_clips: int, max_clips: int) -> str:
    if min_clips > 0 and max_clips > 0:
        return f"{min_clips}-{max_clips} clips total"
    if max_clips > 0:
        return f"up to {max_clips} clips total"
    if min_clips > 0:
        return f"at least {min_clips} clips total"
    return "as many clips as the segment naturally supports"


def postprocess_clips(
    clips: list[dict[str, Any]],
    segment_start: float,
    segment_end: float,
    min_seconds: int,
    max_seconds: int,
    max_clips: int,
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for idx, clip in enumerate(clips, start=1):
        try:
            start = max(segment_start, float(clip["start"]))
            end = min(segment_end, float(clip["end"]))
        except (KeyError, TypeError, ValueError):
            print(f"Skipping clip {idx}: invalid start/end", flush=True)
            continue

        if end <= start:
            print(f"Skipping clip {idx}: empty after segment clamp", flush=True)
            continue

        duration = end - start
        if min_seconds > 0 and duration < min_seconds:
            print(f"Skipping clip {idx}: too short ({duration:.1f}s < {min_seconds}s)", flush=True)
            continue
        if max_seconds > 0 and duration > max_seconds:
            print(f"Skipping clip {idx}: too long ({duration:.1f}s > {max_seconds}s)", flush=True)
            continue

        if is_host_outro_or_market_recap(clip):
            print(f"Skipping clip {idx}: host outro or market recap detected", flush=True)
            continue

        subtitles = []
        for sub_idx, sub in enumerate(clip.get("subtitles", []), start=1):
            try:
                sub_start = max(start, float(sub["start"]))
                sub_end = min(end, float(sub["end"]))
            except (KeyError, TypeError, ValueError):
                print(f"  Skipping subtitle {sub_idx} in clip {idx}: invalid start/end", flush=True)
                continue
            if sub_end <= sub_start:
                continue
            normalized = dict(sub)
            normalized["start"] = sub_start
            normalized["end"] = sub_end
            normalized["relative_start"] = sub_start - start
            normalized["relative_end"] = sub_end - start
            subtitles.append(normalized)

        if len(subtitles) < 2:
            print(f"Skipping clip {idx}: fewer than 2 valid subtitles", flush=True)
            continue

        normalized_clip = dict(clip)
        normalized_clip["start"] = start
        normalized_clip["end"] = end
        normalized_clip["subtitles"] = subtitles
        cleaned.append(normalized_clip)

    if max_clips > 0:
        cleaned = cleaned[:max_clips]
    return cleaned


def is_host_outro_or_market_recap(clip: dict[str, Any]) -> bool:
    pieces = [
        str(clip.get("title", "")),
        " ".join(str(item) for item in clip.get("title_lines", []) if item),
    ]
    for sub in clip.get("subtitles", []):
        pieces.append(str(sub.get("en", "")))
        pieces.append(str(sub.get("zh", "")))
    combined = "\n".join(pieces)
    return any(re.search(pattern, combined, flags=re.IGNORECASE) for pattern in HOST_OUTRO_PATTERNS)


if __name__ == "__main__":
    main()
