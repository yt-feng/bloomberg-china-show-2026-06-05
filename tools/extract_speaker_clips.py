#!/usr/bin/env python3
"""Extract clips of a specific speaker from a Bloomberg video.

Pipeline:
1. Transcribe the video with faster-whisper.
2. Use DeepSeek to identify segments where the target speaker appears.
3. Split those segments into short clips (default 20-90s).
4. Cut the clips with ffmpeg.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Clip:
    start: float
    end: float
    title: str
    index: int

    @property
    def duration(self) -> float:
        return self.end - self.start


def run(cmd: Sequence[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run(
        list(cmd),
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def require_binary(name: str) -> str:
    binary = shutil.which(name)
    if not binary:
        raise SystemExit(f"Missing required binary: {name}")
    return binary


def format_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:05.2f}"
    return f"{m:02d}:{s:05.2f}"


def ffmpeg_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# ─── Transcription ───────────────────────────────────────────────────────────


def transcribe(video_path: Path, transcript_path: Path, language: str, model_name: str, force: bool) -> List[Segment]:
    """Transcribe with faster-whisper."""
    if transcript_path.exists() and not force:
        print(f"Using cached transcript: {transcript_path}")
        return load_segments(transcript_path)

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SystemExit("faster-whisper not installed. Run: pip install faster-whisper") from exc

    print(f"Loading Whisper model: {model_name}", flush=True)
    # Retry model download in case of HuggingFace rate limits (429)
    model = None
    for attempt in range(5):
        try:
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
            break
        except Exception as exc:
            if "429" in str(exc) or "Too Many Requests" in str(exc):
                wait = 30 * (attempt + 1)
                print(f"HuggingFace rate limit hit, waiting {wait}s (attempt {attempt + 1}/5)...", flush=True)
                time.sleep(wait)
            else:
                raise
    if model is None:
        raise SystemExit("Failed to download Whisper model after 5 retries (HuggingFace rate limit)")

    print("Transcribing...", flush=True)
    segments_iter, info = model.transcribe(
        str(video_path),
        language=language,
        vad_filter=True,
        word_timestamps=False,
        beam_size=5,
        condition_on_previous_text=True,
    )

    segments: List[Segment] = []
    for seg in segments_iter:
        text = seg.text.strip()
        if text:
            segments.append(Segment(float(seg.start), float(seg.end), text))

    payload = {
        "model": model_name,
        "language": getattr(info, "language", language),
        "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in segments],
    }
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote transcript ({len(segments)} segments): {transcript_path}", flush=True)
    return segments


def load_segments(path: Path) -> List[Segment]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Segment(float(item["start"]), float(item["end"]), item["text"].strip())
        for item in data.get("segments", [])
        if item.get("text", "").strip()
    ]


# ─── Speaker identification via DeepSeek ─────────────────────────────────────


def transcript_for_prompt(segments: List[Segment]) -> str:
    lines = []
    for seg in segments:
        text = seg.text
        if len(text) > 200:
            text = text[:197] + "..."
        lines.append(f"{format_time(seg.start)}-{format_time(seg.end)} {text}")
    return "\n".join(lines)


def ask_deepseek(api_key: str, system_prompt: str, user_prompt: str) -> dict:
    """Call DeepSeek API and parse JSON response."""
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
        DEEPSEEK_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
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
                raise SystemExit(f"DeepSeek API failed after 3 attempts: {exc}")
    return {}


def find_speaker_segments(
    api_key: str,
    segments: List[Segment],
    speaker_name: str,
    speaker_context: str,
    min_seconds: int,
    max_seconds: int,
) -> List[dict]:
    """Use DeepSeek to identify segments where the target speaker is talking."""

    transcript_text = transcript_for_prompt(segments)

    system_prompt = (
        "You are a video editor analyzing a Bloomberg TV transcript. "
        "Your task is to identify all segments where a specific speaker is talking. "
        "Return strict JSON only."
    )

    # Generate name variants for fuzzy matching in ASR output
    name_parts = speaker_name.split()
    name_variants = [speaker_name]
    if len(name_parts) == 2:
        # Add reversed order (Wang Yi -> Yi Wang)
        name_variants.append(f"{name_parts[1]} {name_parts[0]}")
    name_variants_str = " / ".join(f'"{v}"' for v in name_variants)

    user_prompt = f"""Transcript:
{transcript_text}

---

Target speaker: {speaker_name}
Name variants in transcript (ASR may use different order/spelling): {name_variants_str}
Context: {speaker_context}

Task: Find ALL continuous segments where {speaker_name} (or any name variant listed above) is speaking or being interviewed.
The transcript is from automatic speech recognition, so the speaker's name may appear as any of the variants above, or with slight misspellings.
Include the host's questions that are directly part of the interview exchange with this speaker.
Do NOT include segments where the anchor is talking to a different guest or reading market updates.

For each segment found, split it into clips of {min_seconds}-{max_seconds} seconds each.
Each clip should:
- Start at a natural sentence/topic boundary
- End at a natural pause or topic transition
- Be self-contained enough to understand on its own

Return JSON:
{{
  "speaker_found": true/false,
  "total_speaking_time_seconds": <number>,
  "clips": [
    {{
      "start": <seconds from video start>,
      "end": <seconds from video start>,
      "title": "<short Chinese title describing the clip content, 10-20 chars>",
      "topic": "<brief English description of what's discussed>"
    }}
  ]
}}

If the speaker is not found in the transcript, return {{"speaker_found": false, "clips": []}}.
Timestamps must align with the transcript timestamps provided.
"""

    print(f"Asking DeepSeek to identify {speaker_name} segments...", flush=True)
    result = ask_deepseek(api_key, system_prompt, user_prompt)

    if not result.get("speaker_found", False):
        print(f"Speaker '{speaker_name}' not found in transcript.", flush=True)
        return []

    clips = result.get("clips", [])
    print(f"Found {len(clips)} clips, total speaking time: {result.get('total_speaking_time_seconds', 0):.0f}s", flush=True)
    return clips


# ─── Video cutting ───────────────────────────────────────────────────────────


def cut_clip(video_path: Path, output_path: Path, start: float, end: float) -> bool:
    """Cut a single clip from the source video."""
    require_binary("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    duration = end - start
    cmd = [
        "ffmpeg",
        "-y",
        "-ss", ffmpeg_time(start),
        "-i", str(video_path),
        "-t", f"{duration:.3f}",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        str(output_path),
    ]
    try:
        run(cmd, capture=True)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"ffmpeg failed for {output_path.name}: {exc}", flush=True)
        # Retry with re-encode if stream copy fails
        cmd_reencode = [
            "ffmpeg",
            "-y",
            "-ss", ffmpeg_time(start),
            "-i", str(video_path),
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
            str(output_path),
        ]
        try:
            run(cmd_reencode, capture=True)
            return True
        except subprocess.CalledProcessError:
            return False


def verify_clip(clip_path: Path) -> Optional[float]:
    """Verify a clip exists and return its duration."""
    if not clip_path.exists():
        return None
    try:
        result = run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(clip_path)],
            capture=True,
        )
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


# ─── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True, help="Source video MP4 path")
    parser.add_argument("--speaker", type=str, required=True, help="Speaker name to extract (e.g. 'Wang Yi')")
    parser.add_argument("--speaker-context", type=str, default="",
                        help="Additional context about the speaker (e.g. 'Goldman Sachs China strategist')")
    parser.add_argument("--out-dir", type=Path, default=Path("clips"), help="Output directory for clips")
    parser.add_argument("--work-dir", type=Path, default=Path("work"), help="Working directory for intermediate files")
    parser.add_argument("--min-seconds", type=int, default=20, help="Minimum clip duration")
    parser.add_argument("--max-seconds", type=int, default=90, help="Maximum clip duration")
    parser.add_argument("--whisper-model", type=str, default="medium", help="Whisper model size")
    parser.add_argument("--language", type=str, default="en", help="Transcription language")
    parser.add_argument("--force-transcribe", action="store_true", help="Re-transcribe even if cached")
    parser.add_argument("--force-plan", action="store_true", help="Re-plan even if cached")
    args = parser.parse_args()

    if not args.video.exists():
        raise SystemExit(f"Video not found: {args.video}")

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY environment variable is required")

    # Setup paths
    work_dir = args.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = work_dir / "transcript.json"
    plan_path = work_dir / "speaker_plan.json"

    # Step 1: Transcribe
    print("=" * 60, flush=True)
    print("Step 1: Transcription", flush=True)
    print("=" * 60, flush=True)
    segments = transcribe(args.video, transcript_path, args.language, args.whisper_model, args.force_transcribe)
    print(f"Transcript: {len(segments)} segments, duration: {format_time(segments[-1].end)}", flush=True)

    # Step 2: Find speaker segments
    print("\n" + "=" * 60, flush=True)
    print(f"Step 2: Finding {args.speaker} segments", flush=True)
    print("=" * 60, flush=True)

    if plan_path.exists() and not args.force_plan:
        print(f"Using cached plan: {plan_path}", flush=True)
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
        clips_plan = plan_data.get("clips", [])
    else:
        speaker_context = args.speaker_context or f"{args.speaker} from Goldman Sachs"
        clips_plan = find_speaker_segments(
            api_key, segments, args.speaker, speaker_context,
            args.min_seconds, args.max_seconds,
        )
        plan_data = {
            "speaker": args.speaker,
            "speaker_context": args.speaker_context,
            "video": str(args.video),
            "clips": clips_plan,
        }
        plan_path.write_text(json.dumps(plan_data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote plan: {plan_path}", flush=True)

    if not clips_plan:
        print(f"No clips found for speaker: {args.speaker}", flush=True)
        # Write empty manifest so downstream knows it ran
        manifest = {"speaker": args.speaker, "clips": [], "status": "no_segments_found"}
        (args.out_dir / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return

    # Step 3: Cut clips
    print("\n" + "=" * 60, flush=True)
    print(f"Step 3: Cutting {len(clips_plan)} clips", flush=True)
    print("=" * 60, flush=True)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for idx, clip_info in enumerate(clips_plan, 1):
        start = float(clip_info["start"])
        end = float(clip_info["end"])
        title = clip_info.get("title", f"clip_{idx}")
        # Sanitize title for filename
        safe_title = re.sub(r'[^\w一-鿿]+', '_', title).strip('_')
        filename = f"{idx:02d}_{safe_title}.mp4"
        output_path = args.out_dir / filename

        duration = end - start
        print(f"\n[{idx}/{len(clips_plan)}] {format_time(start)} -> {format_time(end)} ({duration:.1f}s) {title}", flush=True)

        if duration < 5:
            print(f"  Skipping: too short ({duration:.1f}s)", flush=True)
            continue
        if duration > args.max_seconds * 1.5:
            print(f"  Warning: clip is {duration:.1f}s, exceeds max by 50%", flush=True)

        success = cut_clip(args.video, output_path, start, end)
        if success:
            actual_dur = verify_clip(output_path)
            if actual_dur:
                print(f"  OK: {filename} ({actual_dur:.1f}s)", flush=True)
                results.append({
                    "index": idx,
                    "file": filename,
                    "start": start,
                    "end": end,
                    "planned_duration": duration,
                    "actual_duration": actual_dur,
                    "title": title,
                    "topic": clip_info.get("topic", ""),
                })
            else:
                print(f"  FAILED: verification failed for {filename}", flush=True)
        else:
            print(f"  FAILED: could not cut {filename}", flush=True)

    # Write manifest
    manifest = {
        "speaker": args.speaker,
        "source_video": str(args.video),
        "total_clips": len(results),
        "total_duration": sum(r["actual_duration"] for r in results),
        "clips": results,
    }
    manifest_path = args.out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60, flush=True)
    print(f"Done! {len(results)} clips extracted to {args.out_dir}", flush=True)
    print(f"Total duration: {sum(r['actual_duration'] for r in results):.1f}s", flush=True)
    print(f"Manifest: {manifest_path}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
