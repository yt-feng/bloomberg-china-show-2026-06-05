#!/usr/bin/env python3
"""Cut a known speaker segment into short clips using DeepSeek for smart splitting.

This script skips transcription entirely. It takes a known time range where the
speaker appears, extracts audio, does a quick transcription of ONLY that segment
using ffmpeg + DeepSeek (sending raw text), then cuts clips.

For faster operation when transcript is already available, pass --transcript.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


def run(cmd: Sequence[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run(
        list(cmd), check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def ffmpeg_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def ask_deepseek(api_key: str, system_prompt: str, user_prompt: str) -> dict:
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload).encode()
    req = Request(
        DEEPSEEK_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    for attempt in range(3):
        try:
            with urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"]
            return json.loads(content)
        except (HTTPError, URLError, json.JSONDecodeError, KeyError) as exc:
            print(f"DeepSeek attempt {attempt + 1} failed: {exc}", flush=True)
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
            else:
                raise SystemExit(f"DeepSeek API failed: {exc}")
    return {}


def extract_segment_audio_and_transcribe(
    video_path: Path, start: float, end: float, work_dir: Path, api_key: str
) -> str:
    """Extract audio from segment and get a quick transcription via whisper or subtitle extraction."""
    work_dir.mkdir(parents=True, exist_ok=True)
    audio_path = work_dir / "segment_audio.wav"

    # Extract audio for just this segment
    run([
        "ffmpeg", "-y",
        "-ss", ffmpeg_time(start),
        "-i", str(video_path),
        "-t", f"{end - start:.3f}",
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(audio_path),
    ], capture=True)

    # Try faster-whisper if available (only transcribes ~8 min segment, fast even on CPU)
    try:
        from faster_whisper import WhisperModel
        print("Transcribing segment with faster-whisper (base model, ~8 min only)...", flush=True)
        model = None
        for attempt in range(3):
            try:
                model = WhisperModel("base", device="cpu", compute_type="int8")
                break
            except Exception as exc:
                if "429" in str(exc) or "Too Many Requests" in str(exc):
                    wait = 20 * (attempt + 1)
                    print(f"HuggingFace rate limit, waiting {wait}s...", flush=True)
                    time.sleep(wait)
                else:
                    raise
        if model is None:
            raise ImportError("Model download failed")

        segments_iter, _ = model.transcribe(
            str(audio_path), language="en", vad_filter=True,
            word_timestamps=False, beam_size=5,
        )
        lines = []
        for seg in segments_iter:
            t = seg.start + start  # Offset back to absolute video time
            lines.append(f"{t:.1f}s: {seg.text.strip()}")
        return "\n".join(lines)
    except ImportError:
        pass

    # Fallback: no transcription available, use duration-based even splitting
    print("No whisper available, will use even time splits", flush=True)
    return ""


def plan_clips_with_deepseek(
    api_key: str,
    transcript_text: str,
    speaker: str,
    speaker_context: str,
    segment_start: float,
    segment_end: float,
    min_seconds: int,
    max_seconds: int,
) -> List[dict]:
    """Ask DeepSeek to split the segment into natural clips."""

    duration = segment_end - segment_start

    if transcript_text:
        system_prompt = (
            "You are a video editor. Split the given interview segment into short clips. "
            "Return strict JSON only."
        )
        user_prompt = f"""Speaker: {speaker} ({speaker_context})
Segment: {segment_start:.1f}s - {segment_end:.1f}s (total {duration:.0f}s)

Transcript of this segment:
{transcript_text}

---

Split this segment into clips of {min_seconds}-{max_seconds} seconds each.
Each clip should:
- Start and end at natural sentence/topic boundaries
- Be self-contained enough to understand on its own
- Cover a distinct topic or point

Return JSON:
{{
  "clips": [
    {{
      "start": <absolute seconds from video start>,
      "end": <absolute seconds from video start>,
      "title": "<short Chinese title, 10-20 chars, describing the content>",
      "topic": "<brief English description>"
    }}
  ]
}}

Important: timestamps must be absolute (relative to video start, not segment start).
Ensure clips cover the full segment without gaps or overlaps.
"""
    else:
        # No transcript: even split
        system_prompt = "Return JSON only."
        num_clips = max(1, int(duration / ((min_seconds + max_seconds) / 2)))
        clip_dur = duration / num_clips
        clips = []
        for i in range(num_clips):
            clips.append({
                "start": segment_start + i * clip_dur,
                "end": segment_start + (i + 1) * clip_dur,
                "title": f"{speaker}片段{i+1}",
                "topic": f"Segment {i+1}",
            })
        return clips

    print(f"Asking DeepSeek to split {duration:.0f}s segment into {min_seconds}-{max_seconds}s clips...", flush=True)
    result = ask_deepseek(api_key, system_prompt, user_prompt)
    return result.get("clips", [])


def even_split_fallback(segment_start: float, segment_end: float, min_seconds: int, max_seconds: int, speaker: str) -> List[dict]:
    """Fallback: split evenly."""
    duration = segment_end - segment_start
    target = (min_seconds + max_seconds) / 2
    num_clips = max(1, round(duration / target))
    clip_dur = duration / num_clips
    clips = []
    for i in range(num_clips):
        clips.append({
            "start": segment_start + i * clip_dur,
            "end": segment_start + (i + 1) * clip_dur,
            "title": f"{speaker}片段{i+1}",
            "topic": f"Segment {i+1}",
        })
    return clips


def cut_clip(video_path: Path, output_path: Path, start: float, end: float) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    try:
        run([
            "ffmpeg", "-y",
            "-ss", ffmpeg_time(start),
            "-i", str(video_path),
            "-t", f"{duration:.3f}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(output_path),
        ], capture=True)
        return True
    except subprocess.CalledProcessError:
        # Retry with re-encode
        try:
            run([
                "ffmpeg", "-y",
                "-ss", ffmpeg_time(start),
                "-i", str(video_path),
                "-t", f"{duration:.3f}",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                str(output_path),
            ], capture=True)
            return True
        except subprocess.CalledProcessError:
            return False


def verify_clip(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    try:
        result = run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ], capture=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--speaker", type=str, required=True)
    parser.add_argument("--speaker-context", type=str, default="")
    parser.add_argument("--segment-start", type=float, required=True)
    parser.add_argument("--segment-end", type=float, required=True)
    parser.add_argument("--min-seconds", type=int, default=20)
    parser.add_argument("--max-seconds", type=int, default=90)
    parser.add_argument("--out-dir", type=Path, default=Path("clips"))
    parser.add_argument("--transcript", type=Path, help="Pre-existing transcript JSON (optional)")
    args = parser.parse_args()

    if not args.video.exists():
        raise SystemExit(f"Video not found: {args.video}")

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")

    work_dir = Path("work/cut_segment")
    segment_duration = args.segment_end - args.segment_start
    print(f"Speaker: {args.speaker} ({args.speaker_context})", flush=True)
    print(f"Segment: {args.segment_start:.1f}s - {args.segment_end:.1f}s ({segment_duration:.0f}s)", flush=True)

    # Get transcript for just this segment
    transcript_text = ""
    if args.transcript and args.transcript.exists():
        # Use pre-existing transcript, filter to segment range
        data = json.loads(args.transcript.read_text(encoding="utf-8"))
        segs = data.get("segments", [])
        lines = []
        for s in segs:
            if s["end"] >= args.segment_start and s["start"] <= args.segment_end:
                lines.append(f"{s['start']:.1f}s: {s['text'].strip()}")
        transcript_text = "\n".join(lines)
        print(f"Using pre-existing transcript ({len(lines)} segments in range)", flush=True)
    else:
        # Transcribe just this segment (fast: only ~8 min of audio with base model)
        transcript_text = extract_segment_audio_and_transcribe(
            args.video, args.segment_start, args.segment_end, work_dir, api_key
        )

    # Plan clips
    clips_plan = plan_clips_with_deepseek(
        api_key, transcript_text, args.speaker, args.speaker_context,
        args.segment_start, args.segment_end,
        args.min_seconds, args.max_seconds,
    )

    if not clips_plan:
        clips_plan = even_split_fallback(
            args.segment_start, args.segment_end,
            args.min_seconds, args.max_seconds, args.speaker,
        )
        print(f"Using even split fallback: {len(clips_plan)} clips", flush=True)

    # Cut clips
    print(f"\nCutting {len(clips_plan)} clips...", flush=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for idx, clip in enumerate(clips_plan, 1):
        start = float(clip["start"])
        end = float(clip["end"])
        title = clip.get("title", f"clip_{idx}")
        safe_title = re.sub(r'[^\w一-鿿]+', '_', title).strip('_')
        filename = f"{idx:02d}_{safe_title}.mp4"
        output_path = args.out_dir / filename
        duration = end - start

        print(f"[{idx}/{len(clips_plan)}] {start:.1f}s-{end:.1f}s ({duration:.1f}s) {title}", flush=True)

        if duration < 5:
            print(f"  Skipping: too short", flush=True)
            continue

        if cut_clip(args.video, output_path, start, end):
            actual_dur = verify_clip(output_path)
            if actual_dur:
                print(f"  OK: {filename} ({actual_dur:.1f}s)", flush=True)
                results.append({
                    "index": idx,
                    "file": filename,
                    "start": start,
                    "end": end,
                    "duration": actual_dur,
                    "title": title,
                    "topic": clip.get("topic", ""),
                })

    # Write manifest
    manifest = {
        "speaker": args.speaker,
        "speaker_context": args.speaker_context,
        "source_video": str(args.video),
        "segment_range": [args.segment_start, args.segment_end],
        "total_clips": len(results),
        "total_duration": sum(r["duration"] for r in results),
        "clips": results,
    }
    manifest_path = args.out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nDone! {len(results)} clips -> {args.out_dir}", flush=True)
    print(f"Total duration: {sum(r['duration'] for r in results):.1f}s", flush=True)


if __name__ == "__main__":
    main()
