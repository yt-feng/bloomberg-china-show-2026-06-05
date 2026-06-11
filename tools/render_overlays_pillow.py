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
from typing import List, Optional, Sequence, Tuple

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


IndexedChar = Tuple[str, int]


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    return float(draw.textlength(text, font=font))


def line_text(line: Sequence[IndexedChar]) -> str:
    return "".join(char for char, _ in line)


def line_width(draw: ImageDraw.ImageDraw, line: Sequence[IndexedChar], font: ImageFont.FreeTypeFont) -> float:
    return text_width(draw, line_text(line), font)


def trim_line(line: Sequence[IndexedChar]) -> list[IndexedChar]:
    result = list(line)
    while result and result[0][0].isspace():
        result.pop(0)
    while result and result[-1][0].isspace():
        result.pop()
    return result


def strip_leading_spaces(line: Sequence[IndexedChar]) -> list[IndexedChar]:
    result = list(line)
    while result and result[0][0].isspace():
        result.pop(0)
    return result


def tokenize_for_wrap(text: str) -> list[list[IndexedChar]]:
    """Split text into wrap units.

    English text wraps by word; CJK text wraps by character, matching
    AppKit's word-wrapping behavior closely enough for the vertical clips.
    """
    tokens: list[list[IndexedChar]] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char.isspace():
            j = i + 1
            while j < len(text) and text[j].isspace():
                j += 1
            tokens.append([(text[k], k) for k in range(i, j)])
            i = j
        elif ord(char) < 128:
            j = i + 1
            while j < len(text) and ord(text[j]) < 128 and not text[j].isspace():
                j += 1
            tokens.append([(text[k], k) for k in range(i, j)])
            i = j
        else:
            tokens.append([(char, i)])
            i += 1
    return tokens


def append_wrapped_token(
    lines: list[list[IndexedChar]],
    line: list[IndexedChar],
    token: list[IndexedChar],
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[IndexedChar]:
    candidate = line + token
    if not line or line_width(draw, candidate, font) <= max_width:
        return candidate

    trimmed = trim_line(line)
    if trimmed:
        lines.append(trimmed)

    next_line = strip_leading_spaces(token)
    if not next_line:
        return []

    # Rare fallback for a long English token or URL-like string.
    while next_line and line_width(draw, next_line, font) > max_width:
        current: list[IndexedChar] = []
        remaining = next_line
        for idx, item in enumerate(next_line):
            probe = current + [item]
            if current and line_width(draw, probe, font) > max_width:
                lines.append(trim_line(current))
                remaining = next_line[idx:]
                break
            current = probe
        else:
            remaining = []
            if current:
                lines.append(trim_line(current))
        next_line = remaining

    return next_line


def wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
) -> list[list[IndexedChar]]:
    lines: list[list[IndexedChar]] = []
    line: list[IndexedChar] = []
    for token in tokenize_for_wrap(text):
        line = append_wrapped_token(lines, line, token, draw, font, max_width)
    trimmed = trim_line(line)
    if trimmed:
        lines.append(trimmed)
    return lines


def measured_line_height(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
) -> int:
    bbox = draw.textbbox((0, 0), text or "国", font=font, anchor="lt")
    return max(1, int(bbox[3] - bbox[1]))


def measured_block_height(
    draw: ImageDraw.ImageDraw,
    lines: Sequence[Sequence[IndexedChar]],
    font: ImageFont.FreeTypeFont,
    line_spacing: int,
) -> int:
    if not lines:
        return 0
    heights = [measured_line_height(draw, line_text(line), font) for line in lines]
    return sum(heights) + line_spacing * (len(lines) - 1)


def fitting_layout(
    text: str,
    max_width: int,
    max_height: int,
    max_size: int,
    min_size: int,
    bold: bool,
    line_spacing: int,
) -> tuple[ImageFont.FreeTypeFont, list[list[IndexedChar]]]:
    tmp = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(tmp)
    last_font = get_font(min_size, bold)
    last_lines = wrap_text(text, last_font, max_width, draw)
    for size in range(max_size, min_size - 1, -2):
        font = get_font(size, bold)
        lines = wrap_text(text, font, max_width, draw)
        height = measured_block_height(draw, lines, font, line_spacing)
        if height <= max_height:
            return font, lines
        last_font, last_lines = font, lines
    return last_font, last_lines


def highlighted_ranges(text: str, highlights: Optional[List[str]]) -> list[Tuple[int, int]]:
    ranges: list[Tuple[int, int]] = []
    if not highlights:
        return ranges
    lowered = text.casefold()
    for raw_phrase in highlights:
        phrase = raw_phrase.strip()
        if not phrase:
            continue
        needle = phrase.casefold()
        start = 0
        while True:
            idx = lowered.find(needle, start)
            if idx == -1:
                break
            ranges.append((idx, idx + len(phrase)))
            start = idx + len(phrase)
    return ranges


def is_highlighted(index: int, ranges: Sequence[Tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in ranges)


def draw_line_segments(
    draw: ImageDraw.ImageDraw,
    line: Sequence[IndexedChar],
    x: float,
    y: float,
    font: ImageFont.FreeTypeFont,
    base_color: Tuple[int, int, int, int],
    ranges: Sequence[Tuple[int, int]],
) -> None:
    segment: list[str] = []
    segment_color: Tuple[int, int, int, int] | None = None
    cursor = x

    def flush() -> None:
        nonlocal segment, segment_color, cursor
        if not segment or segment_color is None:
            return
        text = "".join(segment)
        draw.text((cursor, y), text, font=font, fill=segment_color, anchor="lt")
        cursor += text_width(draw, text, font)
        segment = []

    for char, original_index in line:
        color = YELLOW if is_highlighted(original_index, ranges) else base_color
        if segment_color is not None and color != segment_color:
            flush()
        segment_color = color
        segment.append(char)
    flush()


def draw_wrapped_text_with_highlights(
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
    line_spacing: int = 4,
    shadow_alpha: float = 0.78,
    shadow_blur: float = 5.0,
) -> None:
    """Draw AppKit-style wrapped text with optional yellow keyword highlights."""
    if not text.strip():
        return

    font, lines = fitting_layout(
        text=text,
        max_width=max_width,
        max_height=max_height,
        max_size=max_font,
        min_size=min_font,
        bold=bold,
        line_spacing=line_spacing,
    )
    tmp = Image.new("RGBA", (1, 1))
    measure_draw = ImageDraw.Draw(tmp)
    ranges = highlighted_ranges(text, highlights)

    if shadow:
        shadow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_layer)
        current_y = float(y + 2)
        shadow_color = (0, 0, 0, int(255 * shadow_alpha))
        for line in lines:
            text_line = line_text(line)
            width = line_width(measure_draw, line, font)
            line_x = x + (max_width - width) / 2 if align == "center" else x
            shadow_draw.text((line_x, current_y), text_line, font=font, fill=shadow_color, anchor="lt")
            current_y += measured_line_height(measure_draw, text_line, font) + line_spacing
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=shadow_blur))
        img.alpha_composite(shadow_layer)

    draw = ImageDraw.Draw(img)
    current_y = float(y)
    for line in lines:
        text_line = line_text(line)
        width = line_width(measure_draw, line, font)
        line_x = x + (max_width - width) / 2 if align == "center" else x
        draw_line_segments(draw, line, line_x, current_y, font, color, ranges)
        current_y += measured_line_height(measure_draw, text_line, font) + line_spacing

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
        draw_wrapped_text_with_highlights(
            img, title_lines[0],
            x=28, y=250, max_width=1024, max_height=150,
            max_font=138, min_font=112,
            line_spacing=0,
            highlights=title_highlights,
        )
        draw_wrapped_text_with_highlights(
            img, title_lines[1],
            x=36, y=408, max_width=1008, max_height=132,
            max_font=124, min_font=96,
            line_spacing=0,
            highlights=title_highlights,
        )
        draw_wrapped_text_with_highlights(
            img, title_lines[2],
            x=36, y=544, max_width=1008, max_height=132,
            max_font=112, min_font=88,
            line_spacing=0,
            highlights=title_highlights,
        )
    elif len(title_lines) >= 2:
        draw_wrapped_text_with_highlights(
            img, title_lines[0],
            x=70, y=112, max_width=940, max_height=78,
            max_font=72, min_font=58,
            line_spacing=0,
            highlights=title_highlights,
        )
        draw_wrapped_text_with_highlights(
            img, title_lines[1],
            x=70, y=198, max_width=940, max_height=92,
            max_font=66, min_font=50,
            line_spacing=4,
            highlights=title_highlights,
        )
    elif title:
        draw_wrapped_text_with_highlights(
            img, title,
            x=70, y=116, max_width=940, max_height=182,
            max_font=66, min_font=50,
            line_spacing=8,
            highlights=title_highlights,
        )

    # Watermark at bottom
    draw_wrapped_text_with_highlights(
        img, watermark,
        x=0, y=1668, max_width=width, max_height=72,
        max_font=58, min_font=58,
        color=(209, 209, 209, 240),  # white 82% with 94% alpha
        bold=True, shadow=True, line_spacing=0,
        shadow_alpha=0.65, shadow_blur=5.0,
    )

    # CTA above watermark
    draw_wrapped_text_with_highlights(
        img, cta,
        x=44, y=1586, max_width=width - 88, max_height=52,
        max_font=34, min_font=34,
        color=(224, 224, 224, 245),  # white 88% with 96% alpha
        bold=True, shadow=True, line_spacing=0,
        shadow_alpha=0.70, shadow_blur=4.0,
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
        draw_wrapped_text_with_highlights(
            img, zh,
            x=70, y=20, max_width=940, max_height=150,
            max_font=52, min_font=34,
            line_spacing=6,
            highlights=zh_highlights,
        )

    if en:
        draw_wrapped_text_with_highlights(
            img, en,
            x=76, y=190, max_width=928, max_height=180,
            max_font=52, min_font=34,
            color=(255, 255, 255, 240),  # 94% alpha
            line_spacing=6,
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
