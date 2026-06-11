#!/usr/bin/env python3
"""Transcribe a video with faster-whisper and write transcript JSON."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", default="base")
    parser.add_argument("--language", default="en")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.out.exists() and not args.force:
        print(f"Using cached transcript: {args.out}", flush=True)
        return
    if not args.video.exists():
        raise SystemExit(f"Video not found: {args.video}")

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SystemExit("faster-whisper is required") from exc

    model = None
    for attempt in range(5):
        try:
            print(f"Loading Whisper model: {args.model} (attempt {attempt + 1}/5)", flush=True)
            model = WhisperModel(args.model, device="cpu", compute_type="int8")
            break
        except Exception as exc:
            if "429" in str(exc) or "Too Many Requests" in str(exc):
                wait = 30 * (attempt + 1)
                print(f"HuggingFace rate limit hit, waiting {wait}s...", flush=True)
                time.sleep(wait)
            else:
                raise
    if model is None:
        raise SystemExit("Failed to load Whisper model")

    print(f"Transcribing video: {args.video}", flush=True)
    segments_iter, info = model.transcribe(
        str(args.video),
        language=args.language,
        vad_filter=True,
        beam_size=5,
        condition_on_previous_text=True,
    )

    segments = []
    for seg in segments_iter:
        text = seg.text.strip()
        if not text:
            continue
        segments.append({
            "start": round(float(seg.start), 2),
            "end": round(float(seg.end), 2),
            "text": text,
        })

    payload = {
        "model": args.model,
        "language": getattr(info, "language", args.language),
        "duration": round(segments[-1]["end"], 2) if segments else 0,
        "segments": segments,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote transcript: {args.out} ({len(segments)} segments)", flush=True)


if __name__ == "__main__":
    main()
