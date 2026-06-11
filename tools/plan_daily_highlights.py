#!/usr/bin/env python3
"""Plan highlight clips for selected daily China Show speakers."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def slugify(value: str) -> str:
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "speaker"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--speakers", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--combined-plan", type=Path, required=True)
    parser.add_argument("--min-seconds", type=int, default=30)
    parser.add_argument("--max-seconds", type=int, default=150)
    parser.add_argument("--min-clips", type=int, default=3)
    parser.add_argument("--max-clips", type=int, default=5)
    args = parser.parse_args()

    speakers_data = json.loads(args.speakers.read_text(encoding="utf-8"))
    speakers = speakers_data.get("speakers", [])
    if not speakers:
        raise SystemExit("No selected speakers to plan")

    planner = Path(__file__).with_name("plan_speaker_highlights.py")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    combined_clips: list[dict[str, Any]] = []
    plan_files: list[str] = []
    skipped: list[dict[str, str]] = []

    for idx, speaker in enumerate(speakers, start=1):
        name = str(speaker["speaker"])
        context = str(speaker.get("speaker_context", ""))
        start = float(speaker["segment_start"])
        end = float(speaker["segment_end"])
        plan_path = args.out_dir / f"{idx:02d}_{slugify(name)}.json"
        print(f"Planning {name}: {start:.1f}-{end:.1f}", flush=True)
        command = [
            sys.executable,
            str(planner),
            "--transcript", str(args.transcript),
            "--speaker", name,
            "--speaker-context", context,
            "--segment-start", f"{start:.2f}",
            "--segment-end", f"{end:.2f}",
            "--min-seconds", str(args.min_seconds),
            "--max-seconds", str(args.max_seconds),
            "--min-clips", str(args.min_clips),
            "--max-clips", str(args.max_clips),
            "--out", str(plan_path),
            "--force",
        ]
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as exc:
            skipped.append({"speaker": name, "reason": f"planner failed with exit code {exc.returncode}"})
            continue

        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        clips = plan.get("clips", [])
        if not clips:
            skipped.append({"speaker": name, "reason": "planner returned no clips"})
            continue
        for clip in clips[: args.max_clips]:
            clip = dict(clip)
            clip["speaker"] = clip.get("speaker") or name
            clip["speaker_context"] = context
            clip["daily_speaker_index"] = idx
            combined_clips.append(clip)
        plan_files.append(str(plan_path))

    if not combined_clips:
        raise SystemExit("No clips planned for any speaker")

    combined = {
        "show_date": speakers_data.get("show_date", ""),
        "source_transcript": str(args.transcript),
        "source_speakers": str(args.speakers),
        "plan_files": plan_files,
        "skipped_speakers": skipped,
        "clips": combined_clips,
    }
    args.combined_plan.parent.mkdir(parents=True, exist_ok=True)
    args.combined_plan.write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote combined plan: {args.combined_plan} ({len(combined_clips)} clips)", flush=True)


if __name__ == "__main__":
    main()
