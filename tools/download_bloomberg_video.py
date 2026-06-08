#!/usr/bin/env python3
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
from urllib.parse import urljoin, urlsplit


ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parent

ACTIVATE_SCRIPT = TOOLS / "activate_bloomberg_tab.applescript"
PROBE_SCRIPT = TOOLS / "chrome_media_probe.applescript"
FETCH_TEXT_SCRIPT = TOOLS / "chrome_fetch_text.applescript"
DOWNLOADER = TOOLS / "proxy_hls_downloader.py"

DEFAULT_SUBSCRIPTION = ROOT / "tmp/proxy_sub.raw"
DEFAULT_SUBSCRIPTION_URL_FILE = ROOT / "tmp/proxy_subscription_url.txt"
DEFAULT_PROXY_CACHE = ROOT / "tmp/working_proxy.url"

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
HLS_URL_RE = re.compile(r"https?://[^\s'\"<>]+?\.m3u8(?:\?[^\s'\"<>]*)?", re.IGNORECASE)


class FetchError(RuntimeError):
    pass


@dataclass
class Variant:
    url: str
    width: int = 0
    height: int = 0
    bandwidth: int = 0
    average_bandwidth: int = 0
    source_url: str = ""
    source_kind: str = ""


def log(message: str) -> None:
    print(f"[auto] {message}", flush=True)


def resolve_path(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    timeout: int | float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=capture,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"Missing required command: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"Command timed out: {cmd[0]}") from exc
    if check and proc.returncode != 0:
        detail = ""
        if capture:
            detail = (proc.stderr or proc.stdout or "").strip()
            if len(detail) > 800:
                detail = detail[-800:]
        raise SystemExit(f"Command failed ({proc.returncode}): {cmd[0]}\n{detail}")
    return proc


def safe_file_part(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-").lower()
    return value or "bloomberg_video"


def slug_from_url(url: str) -> str:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    slug = parts[-1] if parts else "bloomberg_video"
    slug = re.sub(r"-?video$", "", slug, flags=re.IGNORECASE)
    slug = safe_file_part(slug.replace("-", "_"))

    date_match = re.search(r"/news/videos/(\d{4})-(\d{2})-(\d{2})/", parsed.path)
    if date_match:
        date_text = "_".join(date_match.groups())
        slug = re.sub(r"_\d{1,2}_\d{1,2}_\d{4}$", "", slug)
        if date_text not in slug:
            slug = f"{slug}_{date_text}"
    return slug


def tab_needles(url: str) -> list[str]:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    needles = []
    if parts:
        needles.append(parts[-1])
    if parsed.path:
        needles.append(parsed.path)
    needles.append(url)
    return list(dict.fromkeys(needles))


def open_and_activate(url: str, *, no_open: bool, page_wait: float) -> str:
    if not no_open:
        log("Opening Bloomberg page in Chrome")
        run(["open", "-a", "Google Chrome", url], timeout=30)
        time.sleep(page_wait)

    last_error = ""
    for attempt in range(1, 7):
        for needle in tab_needles(url):
            proc = run(
                ["osascript", str(ACTIVATE_SCRIPT), needle],
                check=False,
                timeout=30,
            )
            output = (proc.stdout or "").strip()
            if proc.returncode == 0 and output and output != "NOT_FOUND":
                log(f"Activated Chrome tab: {output}")
                return output
            last_error = (proc.stderr or output or "").strip()
        time.sleep(2)
        log(f"Waiting for Chrome tab to become visible ({attempt}/6)")
    raise SystemExit(f"Could not activate target Chrome tab. {last_error}")


def parse_json_output(text: str, label: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        snippet = text.strip()[:300]
        raise SystemExit(f"Could not parse {label} JSON output: {snippet}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{label} JSON output was not an object.")
    return value


def probe_page(work_dir: Path, retries: int) -> dict:
    work_dir.mkdir(parents=True, exist_ok=True)
    last_stdout = ""
    for attempt in range(1, retries + 1):
        log(f"Probing active Chrome tab ({attempt}/{retries})")
        proc = run(["osascript", str(PROBE_SCRIPT)], check=False, timeout=60)
        last_stdout = proc.stdout or ""
        if proc.returncode == 0 and last_stdout.strip():
            probe = parse_json_output(last_stdout, "media probe")
            (work_dir / "media_probe.json").write_text(json.dumps(probe, indent=2))
            if extract_asset_ids(probe) or extract_hls_urls(probe) or attempt == retries:
                return probe
        time.sleep(3)
    raise SystemExit(f"Media probe failed: {last_stdout[:300]}")


def walk_strings(value: object):
    if isinstance(value, dict):
        for item in value.values():
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)
    elif isinstance(value, str):
        yield value


def normalize_probe_text(text: str) -> str:
    return text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")


def extract_asset_ids(probe: dict) -> list[str]:
    found: list[str] = []

    def add(value: str) -> None:
        value = value.lower()
        if re.fullmatch(UUID_RE, value, flags=re.IGNORECASE) and value not in found:
            found.append(value)

    for value in probe.get("assetIds") or []:
        if isinstance(value, str):
            add(value)

    patterns = [
        re.compile(rf"(?:assetId|assetID|asset_id)[^0-9a-fA-F]{{0,80}}({UUID_RE})", re.IGNORECASE),
        re.compile(rf"media-manifest[^\s'\"<>]*?({UUID_RE})\.m3u8", re.IGNORECASE),
        re.compile(rf"/(?:LOOP|HD|SD|LIVE|VOD)[^\s'\"<>]*?({UUID_RE})\.m3u8", re.IGNORECASE),
        re.compile(rf"[?&]id=({UUID_RE})", re.IGNORECASE),
        re.compile(rf"/vid/({UUID_RE})", re.IGNORECASE),
    ]
    for text in walk_strings(probe):
        text = normalize_probe_text(text)
        for pattern in patterns:
            for match in pattern.finditer(text):
                add(match.group(1))
    return found


def extract_hls_urls(value: object) -> list[str]:
    urls: list[str] = []

    def add(url: str) -> None:
        url = normalize_probe_text(url).rstrip("),.;")
        if url not in urls:
            urls.append(url)

    if isinstance(value, dict):
        for key in ("m3u8Urls", "manifestUrls"):
            for item in value.get(key) or []:
                if isinstance(item, str):
                    for match in HLS_URL_RE.finditer(normalize_probe_text(item)):
                        add(match.group(0))

    for text in walk_strings(value):
        for match in HLS_URL_RE.finditer(normalize_probe_text(text)):
            add(match.group(0))
    return urls


def fetch_text_via_chrome(url: str, work_dir: Path, label: str, timeout: int = 90) -> str:
    proc = run(
        ["osascript", str(FETCH_TEXT_SCRIPT), url],
        check=False,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise FetchError((proc.stderr or proc.stdout or "").strip())
    outer = parse_json_output(proc.stdout or "", f"Chrome fetch {label}")
    (work_dir / f"{safe_file_part(label)}_fetch.json").write_text(json.dumps(outer, indent=2))
    status = int(outer.get("status") or 0)
    text = outer.get("text") or ""
    if not (200 <= status < 300) or not text:
        raise FetchError(f"Chrome fetch returned status {status}")
    return str(text)


def fetch_embed_manifest(asset_id: str, work_dir: Path) -> tuple[dict, str]:
    url = f"https://www.bloomberg.com/media-manifest/embed?id={asset_id}&variant=LOOP&streamType=HD"
    log(f"Fetching Bloomberg embed manifest for asset {asset_id}")
    text = fetch_text_via_chrome(url, work_dir, "embed_manifest")
    (work_dir / "embed_manifest.json").write_text(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit("Bloomberg embed manifest was not valid JSON.") from exc
    if not isinstance(data, dict):
        raise SystemExit("Bloomberg embed manifest was not a JSON object.")
    return data, url


def hls_urls_from_manifest(data: dict) -> list[str]:
    urls: list[str] = []

    def add(url: str) -> None:
        if ".m3u8" in url and url not in urls:
            urls.append(url)

    for key in ("streams", "secureStreams", "downloadURLs"):
        value = data.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    for field in ("url", "streamUrl", "hlsUrl"):
                        url = item.get(field)
                        if isinstance(url, str):
                            add(url)
                elif isinstance(item, str):
                    add(item)
        elif isinstance(value, dict):
            for text in walk_strings(value):
                for match in HLS_URL_RE.finditer(text):
                    add(match.group(0))

    for text in walk_strings(data):
        for match in HLS_URL_RE.finditer(text):
            add(match.group(0))
    return urls


def parse_attrs(text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in re.finditer(r"([A-Z0-9-]+)=(\"[^\"]*\"|[^,]*)", text):
        value = match.group(2)
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[match.group(1)] = value
    return attrs


def int_attr(attrs: dict[str, str], key: str) -> int:
    try:
        return int(attrs.get(key) or 0)
    except ValueError:
        return 0


def quality_from_url(url: str) -> tuple[int, int, int]:
    path = urlsplit(url).path.lower()
    width = height = bandwidth = 0
    if re.search(r"(^|[^a-z])fhd", path) or "1080" in path:
        width, height = 1920, 1080
    elif re.search(r"(^|[^a-z])hd", path) or "720" in path:
        width, height = 1280, 720
    elif re.search(r"(^|[^a-z])sd", path) or "480" in path:
        width, height = 854, 480
    elif "360" in path:
        width, height = 640, 360

    match = re.search(r"(?:fhd|hd|sd)(\d{3,5})", path, re.IGNORECASE)
    if match:
        bandwidth = int(match.group(1)) * 1000
    return width, height, bandwidth


def parse_playlist_variants(text: str, base_url: str) -> list[Variant]:
    if "#EXT-X-KEY" in text.upper():
        raise SystemExit("HLS playlist is encrypted (#EXT-X-KEY); refusing to download.")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants: list[Variant] = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = parse_attrs(line.partition(":")[2])
        next_url = ""
        for candidate in lines[index + 1 :]:
            if not candidate.startswith("#"):
                next_url = candidate
                break
        if not next_url:
            continue
        width = height = 0
        resolution = attrs.get("RESOLUTION", "")
        match = re.match(r"(\d+)x(\d+)", resolution)
        if match:
            width, height = int(match.group(1)), int(match.group(2))
        fallback_width, fallback_height, fallback_bandwidth = quality_from_url(next_url)
        variants.append(
            Variant(
                url=urljoin(base_url, next_url),
                width=width or fallback_width,
                height=height or fallback_height,
                bandwidth=int_attr(attrs, "BANDWIDTH") or fallback_bandwidth,
                average_bandwidth=int_attr(attrs, "AVERAGE-BANDWIDTH"),
                source_url=base_url,
                source_kind="master",
            )
        )

    if variants:
        return variants

    has_media = any(line.startswith("#EXTINF") for line in lines) or any(
        line.startswith("#EXT-X-TARGETDURATION") for line in lines
    )
    if has_media:
        width, height, bandwidth = quality_from_url(base_url)
        return [
            Variant(
                url=base_url,
                width=width,
                height=height,
                bandwidth=bandwidth,
                source_url=base_url,
                source_kind="media",
            )
        ]
    return []


def looks_like_direct_variant(url: str) -> bool:
    path = urlsplit(url).path.lower()
    if path.endswith("/master.m3u8"):
        return False
    if "media-manifest/videos/" in path and re.search(UUID_RE, path, re.IGNORECASE):
        return False
    return any(token in path for token in ("fhd", "hd", "sd", "1080", "720", "480", "360"))


def discover_variants(urls: list[str], work_dir: Path) -> list[Variant]:
    variants: list[Variant] = []
    for index, url in enumerate(urls, start=1):
        try:
            log(f"Fetching HLS candidate {index}/{len(urls)}")
            text = fetch_text_via_chrome(url, work_dir, f"hls_{index:02d}", timeout=90)
        except FetchError as exc:
            if looks_like_direct_variant(url):
                width, height, bandwidth = quality_from_url(url)
                variants.append(
                    Variant(
                        url=url,
                        width=width,
                        height=height,
                        bandwidth=bandwidth,
                        source_url=url,
                        source_kind="unfetched-direct",
                    )
                )
                log(f"Keeping direct HLS candidate despite Chrome fetch failure: {exc}")
            else:
                log(f"Skipping HLS candidate after Chrome fetch failure: {exc}")
            continue

        if not text.lstrip().startswith("#EXTM3U"):
            log("Skipping HLS candidate because it is not an M3U8 playlist")
            continue
        (work_dir / f"hls_{index:02d}.m3u8").write_text(text)
        parsed = parse_playlist_variants(text, url)
        log(f"Found {len(parsed)} playlist variant(s)")
        variants.extend(parsed)
    return dedupe_variants(variants)


def dedupe_variants(variants: list[Variant]) -> list[Variant]:
    seen: set[str] = set()
    output: list[Variant] = []
    for variant in variants:
        if variant.url in seen:
            continue
        seen.add(variant.url)
        output.append(variant)
    return output


def is_pubads(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return "pubads.g.doubleclick.net" in host or "doubleclick.net" in host


def is_bloomberg_delivery(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return "bbgvod" in host or "fastly.net" in host or "bloomberg.com" in host


def select_best_variant(variants: list[Variant]) -> Variant:
    if not variants:
        raise SystemExit("No usable HLS variants were discovered.")

    pool = variants
    non_pubads = [variant for variant in pool if not is_pubads(variant.url)]
    if non_pubads:
        pool = non_pubads
    bloomberg_delivery = [variant for variant in pool if is_bloomberg_delivery(variant.url)]
    if bloomberg_delivery:
        pool = bloomberg_delivery

    def rank(variant: Variant) -> tuple[int, int, int, int, int]:
        pixels = variant.width * variant.height
        bitrate = variant.average_bandwidth or variant.bandwidth
        source_bonus = 1 if is_bloomberg_delivery(variant.url) else 0
        return (pixels, variant.height, bitrate, source_bonus, len(variant.url))

    return max(pool, key=rank)


def variant_summary(variant: Variant) -> str:
    resolution = f"{variant.width}x{variant.height}" if variant.width and variant.height else "unknown resolution"
    bitrate = variant.average_bandwidth or variant.bandwidth
    bitrate_text = f", {bitrate / 1000:.0f} kbps" if bitrate else ""
    return f"{resolution}{bitrate_text}"


def subscription_url_from_args(args: argparse.Namespace) -> str:
    if args.subscription_url:
        return args.subscription_url.strip()
    env_value = os.environ.get("BLOOMBERG_PROXY_SUBSCRIPTION_URL", "").strip()
    if env_value:
        return env_value
    url_file = resolve_path(args.subscription_url_file)
    if url_file.exists():
        return url_file.read_text().strip()
    return ""


def ensure_subscription(args: argparse.Namespace) -> Path:
    subscription = resolve_path(args.subscription)
    if subscription.exists() and subscription.stat().st_size > 0 and not args.refresh_subscription:
        log(f"Using cached proxy subscription: {rel(subscription)}")
        chmod_private(subscription)
        return subscription

    subscription_url = subscription_url_from_args(args)
    if not subscription_url:
        raise SystemExit(
            "Missing proxy subscription. Put the subscription URL in "
            f"{rel(resolve_path(args.subscription_url_file))} or pass --subscription-url once."
        )

    subscription.parent.mkdir(parents=True, exist_ok=True)
    log(f"Refreshing proxy subscription: {rel(subscription)}")
    run(
        [
            "curl",
            "--location",
            "--fail",
            "--silent",
            "--show-error",
            "--output",
            str(subscription),
            subscription_url,
        ],
        timeout=90,
    )
    chmod_private(subscription)
    return subscription


def prime_proxy_cache(work_dir: Path, cache_path: Path) -> None:
    cache_path = resolve_path(cache_path)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / "working_proxy.url"
        shutil.copy2(cache_path, target)
        chmod_private(target)
        log(f"Primed cached working proxy from {rel(cache_path)}")


def save_proxy_cache(work_dir: Path, cache_path: Path) -> None:
    source = work_dir / "working_proxy.url"
    cache_path = resolve_path(cache_path)
    if source.exists() and source.stat().st_size > 0:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, cache_path)
        chmod_private(cache_path)
        log(f"Updated working proxy cache: {rel(cache_path)}")


def run_downloader(args: argparse.Namespace, variant: Variant, subscription: Path, work_dir: Path, output: Path) -> None:
    prime_proxy_cache(work_dir, args.proxy_cache)
    cmd = [
        sys.executable,
        str(DOWNLOADER),
        "--subscription",
        str(subscription),
        "--playlist-url",
        variant.url,
        "--work-dir",
        str(work_dir),
        "--output",
        str(output),
        "--workers",
        str(args.workers),
        "--chrome-doh",
        "--referer",
        args.url,
    ]
    log("Starting segmented HLS download")
    run(cmd, capture=False)
    save_proxy_cache(work_dir, args.proxy_cache)


def verify_output(output: Path) -> None:
    if not output.exists():
        raise SystemExit(f"Expected output was not created: {rel(output)}")
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,size,bit_rate",
                "-show_entries",
                "stream=index,codec_type,codec_name,width,height,avg_frame_rate",
                "-of",
                "json",
                str(output),
            ],
            text=True,
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError:
        log(f"Wrote {rel(output)}; ffprobe is not installed, so verification was skipped")
        return
    if proc.returncode != 0:
        log(f"Wrote {rel(output)}; ffprobe verification failed")
        return
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log(f"Wrote {rel(output)}; ffprobe output was not JSON")
        return

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    duration = float(fmt.get("duration") or 0)
    size = int(fmt.get("size") or output.stat().st_size)
    video_text = ""
    if video:
        video_text = f", video {video.get('codec_name')} {video.get('width')}x{video.get('height')}"
    audio_text = f", audio {audio.get('codec_name')}" if audio else ""
    log(f"Verified {rel(output)}: {duration:.2f}s, {size / 1024 / 1024:.1f} MiB{video_text}{audio_text}")


def write_plan(work_dir: Path, url: str, asset_id: str | None, candidates: list[str], selected: Variant, output: Path) -> None:
    plan = {
        "url": url,
        "asset_id": asset_id,
        "candidate_count": len(candidates),
        "selected_variant": {
            "url": selected.url,
            "width": selected.width,
            "height": selected.height,
            "bandwidth": selected.bandwidth,
            "average_bandwidth": selected.average_bandwidth,
            "source_url": selected.source_url,
            "source_kind": selected.source_kind,
        },
        "output": str(output),
    }
    (work_dir / "download_plan.json").write_text(json.dumps(plan, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download a Bloomberg video URL through the established Chrome/proxy workflow.")
    parser.add_argument("--url", required=True, help="Bloomberg video page URL.")
    parser.add_argument("--subscription", type=Path, default=DEFAULT_SUBSCRIPTION)
    parser.add_argument("--subscription-url", help="Proxy subscription URL. Prefer env/file for normal use.")
    parser.add_argument("--subscription-url-file", type=Path, default=DEFAULT_SUBSCRIPTION_URL_FILE)
    parser.add_argument("--refresh-subscription", action="store_true")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "downloads")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--proxy-cache", type=Path, default=DEFAULT_PROXY_CACHE)
    parser.add_argument("--keep-tmp", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Discover and select the HLS variant, but do not download.")
    parser.add_argument("--force", action="store_true", help="Replace an existing output file.")
    parser.add_argument("--no-open", action="store_true", help="Use the current Chrome tab instead of opening the URL.")
    parser.add_argument("--page-wait", type=float, default=6.0)
    parser.add_argument("--probe-retries", type=int, default=4)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    slug = slug_from_url(args.url)
    output_dir = resolve_path(args.output_dir)
    output = resolve_path(args.output) if args.output else output_dir / f"{slug}_1080p.mp4"
    work_dir = resolve_path(args.work_dir) if args.work_dir else ROOT / "tmp" / f"auto_{slug}"

    if output.exists() and not args.force and not args.dry_run:
        log(f"Output already exists; skipping download: {rel(output)}")
        verify_output(output)
        return 0
    if output.exists() and args.force and not args.dry_run:
        output.unlink()

    success = False
    try:
        open_and_activate(args.url, no_open=args.no_open, page_wait=args.page_wait)
        probe = probe_page(work_dir, args.probe_retries)
        asset_ids = extract_asset_ids(probe)
        asset_id = asset_ids[0] if asset_ids else None
        if asset_ids:
            log(f"Found assetId: {asset_id}")
        else:
            log("No assetId found; falling back to probed HLS URLs")

        candidates: list[str] = []
        if asset_id:
            try:
                manifest, _manifest_url = fetch_embed_manifest(asset_id, work_dir)
                candidates.extend(hls_urls_from_manifest(manifest))
            except FetchError as exc:
                log(f"Embed manifest fetch failed; falling back to page resources: {exc}")
        candidates.extend(extract_hls_urls(probe))
        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            raise SystemExit("No HLS candidates found from Bloomberg page or embed manifest.")

        log(f"Evaluating {len(candidates)} HLS candidate(s)")
        variants = discover_variants(candidates, work_dir)
        selected = select_best_variant(variants)
        log(f"Selected HLS variant: {variant_summary(selected)}")
        log(selected.url)
        write_plan(work_dir, args.url, asset_id, candidates, selected, output)

        if args.dry_run:
            log(f"Dry run complete; planned output: {rel(output)}")
            success = True
            return 0

        subscription = ensure_subscription(args)
        run_downloader(args, selected, subscription, work_dir, output)
        verify_output(output)
        success = True
        return 0
    finally:
        if success and not args.keep_tmp and work_dir.exists():
            shutil.rmtree(work_dir)
            log(f"Cleaned temporary work dir: {rel(work_dir)}")


if __name__ == "__main__":
    raise SystemExit(main())
