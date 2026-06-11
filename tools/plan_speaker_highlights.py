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
1. Split this into clips of {args.min_seconds}-{args.max_seconds} seconds each. Each clip should start/end at natural topic boundaries.
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
- Each clip must have at least 3 subtitles
- Chinese titles must have hook/conflict angle, not flat descriptions
- Avoid: 投资, 股票, A股, 港股, 美股 in Chinese text
"""

    print("Requesting clip plan from DeepSeek...", flush=True)
    result = ask_deepseek(api_key, system_prompt, user_prompt)
    clips = result.get("clips", [])

    if not clips:
        raise SystemExit("DeepSeek returned no clips")

    # Post-process: add relative_start/relative_end to subtitles
    for clip in clips:
        clip_start = float(clip["start"])
        for sub in clip.get("subtitles", []):
            sub["relative_start"] = float(sub["start"]) - clip_start
            sub["relative_end"] = float(sub["end"]) - clip_start

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


if __name__ == "__main__":
    main()
