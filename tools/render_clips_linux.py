#!/usr/bin/env python3
"""Render vertical bilingual highlight clips on Linux using Pillow overlays + ffmpeg.

Port of vid_cut/render_highlight_clips.py for GitHub Actions (Linux).
Uses render_overlays_pillow.py instead of Swift, libx264 instead of VideoToolbox.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


OUT_W = 1080
OUT_H = 1920
CAPTION_W = 1080
CAPTION_H = 430
MAIN_Y = 690
PANEL_Y = 1110
CAPTION_Y = 1125
SOURCE_TOP_CROP = 130
SOURCE_BOTTOM_CROP = 210
BG_DEDUPE_FILTER = "eq=brightness=-0.28:contrast=1.02:saturation=0.62:gamma=1.002,noise=alls=0.8:allf=t+u"
MAIN_DEDUPE_FILTER = "eq=brightness=0.006:contrast=1.018:saturation=1.012:gamma=1.002,unsharp=5:5:0.12"
AUDIO_DEDUPE_FILTER = (
    "highpass=f=72,lowpass=f=18500,"
    "acompressor=threshold=-20dB:ratio=1.08:attack=12:release=160,"
    "equalizer=f=3200:t=q:w=1.2:g=0.25,volume=1.012"
)
SENSITIVE_ZH_TERMS = [
    "投资",
    "革命",
]
PARAPHRASE_ZH_TERMS = [
    ("投资者", "市场参与者"),
    ("投资主题", "主线"),
    ("中国股票", "中国市场"),
    ("中国股", "中国市场"),
    ("A 股", "内地市场"),
    ("A股", "内地市场"),
    ("港股", "香港市场"),
    ("美股", "美国市场"),
    ("股市", "市场"),
    ("股票", "权益资产"),
    ("个股", "单家公司"),
    ("股", "权益"),
]


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Source video MP4")
    parser.add_argument("--plan", type=Path, required=True, help="highlight_plan.json")
    parser.add_argument("--out-dir", type=Path, default=Path("clips_final"))
    parser.add_argument("--work-dir", type=Path, default=Path("work/render"))
    parser.add_argument("--only", help="Render specific clip indexes (e.g. 1,2 or 1-3)")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if not args.source.exists():
        raise SystemExit(f"Source video not found: {args.source}")
    if not args.plan.exists():
        raise SystemExit(f"Plan not found: {args.plan}")

    payload = json.loads(args.plan.read_text(encoding="utf-8"))
    clips = payload.get("clips", [])
    if not clips:
        raise SystemExit("No clips in plan")

    selected = parse_only(args.only, len(clips))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)

    renderer = Path(__file__).with_name("render_overlays_pillow.py")
    if not renderer.exists():
        raise SystemExit(f"Pillow renderer not found: {renderer}")

    for one_based in selected:
        clip = clips[one_based - 1]
        output = args.out_dir / output_name(one_based, clip)
        if output.exists() and not args.force:
            print(f"[{one_based}/{len(clips)}] Exists, skipping: {output}", flush=True)
            continue

        clip_dir = args.work_dir / f"clip_{one_based:02d}"
        clip_dir.mkdir(parents=True, exist_ok=True)

        # Generate overlay PNGs
        static_png, subtitle_pngs = render_overlay_images(renderer, clip_dir, clip)

        duration = duration_of(clip)
        print(
            f"[{one_based}/{len(clips)}] Rendering: {clip.get('title', '')} "
            f"({duration:.1f}s, {len(subtitle_pngs)} subtitles)",
            flush=True,
        )

        # Composite with ffmpeg
        render_clip(
            source=args.source,
            clip=clip,
            static_png=static_png,
            subtitle_pngs=subtitle_pngs,
            output=output,
            threads=args.threads,
        )
        print(f"[{one_based}/{len(clips)}] Wrote: {output}", flush=True)

    print(f"\nDone. {len(selected)} clips rendered to {args.out_dir}", flush=True)


def parse_only(value: str | None, total: int) -> list[int]:
    if not value:
        return list(range(1, total + 1))
    indexes: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            left, right = part.split("-", 1)
            indexes.update(range(int(left), int(right) + 1))
        elif part:
            indexes.add(int(part))
    result = sorted(indexes)
    for idx in result:
        if idx < 1 or idx > total:
            raise SystemExit(f"--only index out of range: {idx}; plan has {total} clip(s)")
    return result


def render_overlay_images(
    renderer: Path,
    clip_dir: Path,
    clip: dict[str, Any],
) -> tuple[Path, list[tuple[Path, float, float]]]:
    """Generate overlay PNGs using the Pillow renderer."""
    static_png = clip_dir / "static.png"
    jobs: list[dict[str, Any]] = [
        {
            "kind": "static",
            "output": str(static_png),
            "width": OUT_W,
            "height": OUT_H,
            "title": safe_zh_text(str(clip.get("title", ""))),
            "titleLines": [safe_zh_text(line) for line in title_lines_for_clip(clip)],
            "titleHighlights": [safe_zh_text(str(item)) for item in clip.get("title_highlights", [])],
            "watermark": "KC桌面",
            "cta": "更多宏观信息，关注公众号KC桌面",
        }
    ]

    subtitle_pngs: list[tuple[Path, float, float]] = []
    for subtitle in clip.get("subtitles", []):
        start, end = relative_times(clip, subtitle)
        if end <= start:
            continue
        zh_source = subtitle.get("zh_filtered") or subtitle.get("zh", "")
        png = clip_dir / f"sub_{int(subtitle.get('index', len(subtitle_pngs) + 1)):03d}.png"
        jobs.append({
            "kind": "subtitle",
            "output": str(png),
            "width": CAPTION_W,
            "height": CAPTION_H,
            "zh": safe_zh_text(str(zh_source)),
            "en": clean_display_text(str(subtitle.get("en", ""))),
            "zhHighlights": [safe_zh_text(str(item)) for item in subtitle.get("zh_highlights", [])],
            "enHighlights": subtitle.get("en_highlights", []),
        })
        subtitle_pngs.append((png, start, end))

    batch_path = clip_dir / "overlay_batch.json"
    batch_path.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2), encoding="utf-8")
    subprocess.run([sys.executable, str(renderer), str(batch_path)], check=True)
    return static_png, subtitle_pngs


def relative_times(clip: dict[str, Any], subtitle: dict[str, Any]) -> tuple[float, float]:
    if "relative_start" in subtitle and "relative_end" in subtitle:
        start = float(subtitle["relative_start"])
        end = float(subtitle["relative_end"])
    else:
        start = float(subtitle["start"]) - float(clip["start"])
        end = float(subtitle["end"]) - float(clip["start"])
    duration = duration_of(clip)
    return max(0.0, start), min(duration, end)


def render_clip(
    *,
    source: Path,
    clip: dict[str, Any],
    static_png: Path,
    subtitle_pngs: list[tuple[Path, float, float]],
    output: Path,
    threads: int,
) -> None:
    command = build_ffmpeg_command(
        source=source,
        clip=clip,
        static_png=static_png,
        subtitle_pngs=subtitle_pngs,
        output=output,
        threads=threads,
    )
    subprocess.run(command, check=True)


def build_ffmpeg_command(
    *,
    source: Path,
    clip: dict[str, Any],
    static_png: Path,
    subtitle_pngs: list[tuple[Path, float, float]],
    output: Path,
    threads: int,
) -> list[str]:
    duration = duration_of(clip)
    image_inputs: list[str] = []
    for png in [static_png] + [item[0] for item in subtitle_pngs]:
        image_inputs.extend(["-loop", "1", "-framerate", "30000/1001", "-t", f"{duration:.3f}", "-i", str(png)])

    filter_complex = build_filter_complex(duration, subtitle_pngs)
    return [
        "ffmpeg",
        "-hide_banner", "-y", "-nostdin", "-loglevel", "error", "-nostats",
        "-filter_threads", str(threads),
        "-filter_complex_threads", str(threads),
        "-ss", f"{float(clip['start']):.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(source),
        *image_inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-threads", str(threads), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart",
        "-shortest",
        str(output),
    ]


def build_filter_complex(duration: float, subtitle_pngs: list[tuple[Path, float, float]]) -> str:
    parts = [
        "[0:v]setpts=PTS-STARTPTS,split=2[bgsrc][mainsrc]",
        (
            f"[bgsrc]crop=iw:ih-{SOURCE_TOP_CROP + SOURCE_BOTTOM_CROP}:0:{SOURCE_TOP_CROP},"
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,{BG_DEDUPE_FILTER}[bg]"
        ),
        (
            f"[mainsrc]crop=iw:ih-{SOURCE_TOP_CROP + SOURCE_BOTTOM_CROP}:0:{SOURCE_TOP_CROP},"
            f"scale=1080:-2,{MAIN_DEDUPE_FILTER}[main]"
        ),
        (
            f"[bg][main]overlay=0:{MAIN_Y}:format=auto,"
            f"drawbox=x=0:y={PANEL_Y}:w=1080:h={OUT_H - PANEL_Y}:color=black@0.88:t=fill[base]"
        ),
        "[base][1:v]overlay=0:0:format=auto[v1]",
    ]

    previous = "v1"
    for idx, (_, start, end) in enumerate(subtitle_pngs, start=2):
        label = f"v{idx}"
        expr = f"between(t\\,{start:.3f}\\,{end:.3f})"
        parts.append(f"[{previous}][{idx}:v]overlay=0:{CAPTION_Y}:format=auto:enable={expr}[{label}]")
        previous = label

    parts.append(f"[{previous}]format=yuv420p[vout]")

    fade_out_start = max(0.0, duration - 5.0)
    fade_out_duration = min(5.0, duration)
    parts.append(
        f"[0:a]asetpts=PTS-STARTPTS,{AUDIO_DEDUPE_FILTER},"
        "afade=t=in:st=0:d=3,"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade_out_duration:.3f}[aout]"
    )
    return ";".join(parts)


def title_lines_for_clip(clip: dict[str, Any]) -> list[str]:
    lines = clip.get("title_lines")
    if isinstance(lines, list) and len(lines) >= 3:
        cleaned = [
            clean_display_text(str(item)).replace("：", "").replace(":", "")
            for item in lines[:3]
        ]
        if all(cleaned):
            return cleaned
    if isinstance(lines, list) and len(lines) >= 2:
        cleaned = [clean_display_text(str(item)).replace("：", "").replace(":", "") for item in lines[:2]]
        if all(cleaned):
            return [cleaned[0], *split_title_topic(cleaned[1])]

    title = clean_display_text(str(clip.get("title", "")))
    if "：" in title:
        left, right = title.split("：", 1)
        return [left.strip(), *split_title_topic(right.strip())]
    if ":" in title:
        left, right = title.split(":", 1)
        return [left.strip(), *split_title_topic(right.strip())]
    return [title, ""]


def split_title_topic(topic: str) -> list[str]:
    topic = clean_display_text(topic)
    if len(topic) <= 9:
        return [topic, ""]

    if topic.startswith("市场已提前定价"):
        return ["市场已提前定价", topic.removeprefix("市场已提前定价").strip()]

    candidates = []
    for marker in ["危机", "预期", "央行", "回调后", "估值", "可能是", "已提前", "城市更新", "房价"]:
        idx = topic.find(marker)
        if idx > 0:
            candidates.append(idx + len(marker))

    if candidates:
        split_at = min(candidates, key=lambda pos: abs(pos - len(topic) / 2))
    else:
        split_at = len(topic) // 2
        for pos in range(max(4, split_at - 4), min(len(topic) - 3, split_at + 5)):
            left = topic[:pos]
            right = topic[pos:]
            if not left.endswith(("的", "是", "和", "与")) and not right.startswith(("的", "是", "和", "与")):
                split_at = pos
                break

    return [topic[:split_at].strip(), topic[split_at:].strip()]


def output_name(index: int, clip: dict[str, Any]) -> str:
    title = str(clip.get("title", f"clip_{index:02d}"))
    title = title.replace("：", "_").replace(":", "_")
    title = title.replace("（", "(").replace("）", ")")
    title = re.sub(r"\((\d+)/(\d+)\)", r"_\1-\2", title)
    title = re.sub(r"[\\/:*?\"<>|]", "_", title)
    title = re.sub(r"\s+", "_", title).strip("._ ")
    title = re.sub(r"_+", "_", title)
    return f"{index:02d}_{title}.mp4"


def duration_of(clip: dict[str, Any]) -> float:
    return float(clip["end"]) - float(clip["start"])


def filter_sensitive_zh(text: str) -> str:
    for term in sorted(SENSITIVE_ZH_TERMS, key=len, reverse=True):
        text = text.replace(term, "**")
    return text


def paraphrase_sensitive_zh(text: str) -> str:
    for old, new in PARAPHRASE_ZH_TERMS:
        text = text.replace(old, new)
    return text


def safe_zh_text(text: str) -> str:
    return filter_sensitive_zh(paraphrase_sensitive_zh(clean_display_text(text)))


def clean_display_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    fixes = {
        "straighter hummus": "Strait of Hormuz",
        "street of Ramuz": "Strait of Hormuz",
        "strait of hummus": "Strait of Hormuz",
        "Richard Minman": "Richard Koo",
    }
    for bad, good in fixes.items():
        text = re.sub(re.escape(bad), good, text, flags=re.IGNORECASE)
    return text


if __name__ == "__main__":
    main()
