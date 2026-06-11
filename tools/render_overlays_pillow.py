#!/usr/bin/env python3
"""Render text overlay PNGs using Pillow (Linux replacement for render_text_overlays.swift).

Reads the same overlay_batch.json format as the Swift renderer and produces
identical transparent PNG outputs for ffmpeg compositing.

Usage: python3 render_overlays_pillow.py batch.json
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Colors
WHITE = (255, 255, 255, 255)
YELLOW = (255, 209, 51, 255)
SHADOW_COLOR = (0, 0, 0, 199)  # ~78% alpha

# Font paths (Ubuntu with fonts-noto-cjk installed)
FONT_PATHS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
    "/usr/share/fonts/noto/NotoSansCJKsc-Bold.otf",
    # macOS fallbacks for local testing
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
]

FONT_PATHS_REGULAR = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/noto/NotoSansCJKsc-Regular.otf",
    "/System/Library/Fonts/PingFang.ttc",
]


def find_font(paths: List[str]) -> str:
    for p in paths:
        if os.path.exists(p):
            return p
    # Last resort: try fc-match
    import subprocess
    try:
        result = subprocess.run(
            ["fc-match", "--format=%{file}", "Noto Sans CJK SC:style=Bold"],
            capture_output=True, text=True, check=True,
        )
        if result.stdout and os.path.exists(result.stdout):
            return result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    raise SystemExit(
        "No CJK font found. Install with: sudo apt-get install fonts-noto-cjk"
    )


def get_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    paths = FONT_PATHS if bold else FONT_PATHS_REGULAR
    font_path = find_font(paths)
    return ImageFont.truetype(font_path, size)


def text_bbox_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> Tuple[int, int]:
    """Get width and height of text."""
    bbox = draw.multiline_textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def fitting_font_size(
    text: str, max_width: int, max_height: int, max_size: int, min_size: int, bold: bool = True
) -> int:
    """Find largest font size that fits text within bounds."""
    tmp = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(tmp)
    for size in range(max_size, min_size - 1, -2):
        font = get_font(size, bold)
        w, h = text_bbox_size(draw, text, font)
        if w <= max_width and h <= max_height:
            return size
    return min_size


def draw_text_with_highlights(
    img: Image.Image,
    text: str,
    x: int,
    y: int,
    max_width: int,
    max_height: int,
    max_font: int,
    min_font: int,
    color: Tuple[int, int, int, int] = WHITE,
    highlights: Optional[List[str]] = None,
    bold: bool = True,
    shadow: bool = True,
    align: str = "center",
) -> None:
    """Draw text with optional yellow keyword highlights and shadow."""
    if not text.strip():
        return

    font_size = fitting_font_size(text, max_width, max_height, max_font, min_font, bold)
    font = get_font(font_size, bold)

    # Determine highlighted ranges
    highlight_ranges: List[Tuple[int, int]] = []
    if highlights:
        for phrase in highlights:
            phrase = phrase.strip()
            if not phrase:
                continue
            start = 0
            while True:
                idx = text.lower().find(phrase.lower(), start)
                if idx == -1:
                    break
                highlight_ranges.append((idx, idx + len(phrase)))
                start = idx + len(phrase)

    # Calculate text position for centering
    tmp = Image.new("RGBA", (1, 1))
    tmp_draw = ImageDraw.Draw(tmp)
    text_w, text_h = text_bbox_size(tmp_draw, text, font)

    if align == "center":
        text_x = x + (max_width - text_w) // 2
    else:
        text_x = x
    text_y = y

    # Draw shadow
    if shadow:
        shadow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_layer)
        shadow_draw.text((text_x, text_y + 2), text, font=font, fill=SHADOW_COLOR)
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=3))
        img.paste(Image.alpha_composite(Image.new("RGBA", img.size, (0, 0, 0, 0)), shadow_layer), (0, 0))

    # Draw text character by character for highlight support
    if highlight_ranges:
        draw = ImageDraw.Draw(img)
        current_x = text_x
        for i, char in enumerate(text):
            # Determine color for this character
            char_color = color
            for hs, he in highlight_ranges:
                if hs <= i < he:
                    char_color = YELLOW
                    break
            draw.text((current_x, text_y), char, font=font, fill=char_color)
            char_w = draw.textlength(char, font=font)
            current_x += int(char_w)
    else:
        draw = ImageDraw.Draw(img)
        draw.text((text_x, text_y), text, font=font, fill=color)


def render_static_overlay(job: dict) -> Image.Image:
    """Render static overlay: title + watermark + CTA."""
    width = job["width"]
    height = job["height"]
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    title = job.get("title", "")
    title_lines = [l for l in (job.get("titleLines") or []) if l.strip()]
    title_highlights = job.get("titleHighlights") or []
    watermark = job.get("watermark", "KC桌面")
    cta = job.get("cta", "更多宏观信息，关注公众号KC桌面")

    # Draw title
    if len(title_lines) >= 3:
        # 3-line large title layout
        draw_text_with_highlights(
            img, title_lines[0],
            x=28, y=250, max_width=1024, max_height=150,
            max_font=138, min_font=112,
            highlights=title_highlights,
        )
        draw_text_with_highlights(
            img, title_lines[1],
            x=36, y=408, max_width=1008, max_height=132,
            max_font=124, min_font=96,
            highlights=title_highlights,
        )
        draw_text_with_highlights(
            img, title_lines[2],
            x=36, y=544, max_width=1008, max_height=132,
            max_font=112, min_font=88,
            highlights=title_highlights,
        )
    elif len(title_lines) >= 2:
        draw_text_with_highlights(
            img, title_lines[0],
            x=70, y=112, max_width=940, max_height=78,
            max_font=72, min_font=58,
            highlights=title_highlights,
        )
        draw_text_with_highlights(
            img, title_lines[1],
            x=70, y=198, max_width=940, max_height=92,
            max_font=66, min_font=50,
            highlights=title_highlights,
        )
    elif title:
        draw_text_with_highlights(
            img, title,
            x=70, y=116, max_width=940, max_height=182,
            max_font=66, min_font=50,
            highlights=title_highlights,
        )

    # Watermark at bottom
    draw_text_with_highlights(
        img, watermark,
        x=0, y=1668, max_width=width, max_height=72,
        max_font=58, min_font=58,
        color=(209, 209, 209, 240),  # white 82% with 94% alpha
        bold=True, shadow=True,
    )

    # CTA above watermark
    draw_text_with_highlights(
        img, cta,
        x=44, y=1586, max_width=width - 88, max_height=52,
        max_font=34, min_font=34,
        color=(224, 224, 224, 245),  # white 88% with 96% alpha
        bold=False, shadow=True,
    )

    return img


def render_subtitle_overlay(job: dict) -> Image.Image:
    """Render subtitle overlay: Chinese text + English text."""
    width = job["width"]
    height = job["height"]
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    zh = job.get("zh", "")
    en = job.get("en", "")
    zh_highlights = job.get("zhHighlights") or []
    en_highlights = job.get("enHighlights") or []

    if zh:
        draw_text_with_highlights(
            img, zh,
            x=70, y=20, max_width=940, max_height=150,
            max_font=52, min_font=34,
            highlights=zh_highlights,
        )

    if en:
        draw_text_with_highlights(
            img, en,
            x=76, y=190, max_width=928, max_height=180,
            max_font=52, min_font=34,
            color=(255, 255, 255, 240),  # 94% alpha
            highlights=en_highlights,
        )

    return img


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: render_overlays_pillow.py <batch.json>")

    batch_path = Path(sys.argv[1])
    data = json.loads(batch_path.read_text(encoding="utf-8"))
    jobs = data.get("jobs", [])

    for job in jobs:
        kind = job.get("kind", "")
        output_path = Path(job["output"])
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if kind == "static":
            img = render_static_overlay(job)
        elif kind == "subtitle":
            img = render_subtitle_overlay(job)
        else:
            print(f"Unknown job kind: {kind}", file=sys.stderr)
            continue

        img.save(str(output_path), "PNG")
        print(f"  Wrote: {output_path}", flush=True)


if __name__ == "__main__":
    main()
