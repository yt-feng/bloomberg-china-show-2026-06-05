#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urljoin, urlsplit, urlunsplit


SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_subscription(path: Path) -> list[str]:
    raw = path.read_bytes().strip()
    try:
        text = raw.decode("utf-8")
        if "://" not in text and re.fullmatch(r"[A-Za-z0-9+/_=\-\s]+", text):
            text = base64.b64decode(raw + b"=" * ((4 - len(raw) % 4) % 4)).decode("utf-8", "ignore")
    except UnicodeDecodeError:
        text = base64.b64decode(raw + b"=" * ((4 - len(raw) % 4) % 4)).decode("utf-8", "ignore")
    return [line.strip() for line in text.splitlines() if line.strip()]


def proxy_scheme(proxy: str) -> str:
    match = re.match(r"^([a-z0-9+.-]+)://", proxy.lower())
    return match.group(1) if match else ""


def strip_proxy_label(proxy: str) -> str:
    parts = urlsplit(proxy)
    if not parts.scheme:
        return proxy
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def b64decode_text(value: str) -> str | None:
    try:
        padded = value + "=" * ((4 - len(value) % 4) % 4)
        return base64.b64decode(padded).decode("utf-8", "ignore")
    except Exception:
        return None


def proxy_without_path(proxy: str) -> str:
    parts = urlsplit(proxy)
    if not parts.scheme or not parts.netloc:
        return proxy
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def normalize_proxy(node: str) -> str | None:
    scheme = proxy_scheme(node)
    if scheme == "https":
        parts = urlsplit(node)
        payload = node[len("https://") :]
        decoded = None
        if not parts.username and not parts.port and len(payload) > 80:
            decoded = b64decode_text(payload)
        if decoded:
            return proxy_without_path("https://" + decoded)
        return proxy_without_path(strip_proxy_label(node))
    if scheme in {"http", "socks5", "socks5h"}:
        proxy = proxy_without_path(strip_proxy_label(node))
        if scheme == "socks5":
            proxy = "socks5h://" + proxy[len("socks5://") :]
        return proxy
    return None


def curl_config(
    proxy: str,
    url: str,
    output: Path,
    extra: list[str] | None = None,
    resolve_entries: list[str] | None = None,
    referer: str = "https://www.bloomberg.com/",
) -> str:
    opts = [
        "location",
        "fail",
        "silent",
        "show-error",
        "connect-timeout = 10",
        "max-time = 60",
        "retry = 2",
        "retry-delay = 1",
        'user-agent = "Mozilla/5.0"',
        f'header = "Referer: {referer}"',
        f'proxy = "{proxy}"',
        f'url = "{url}"',
        f'output = "{output}"',
    ]
    for entry in resolve_entries or []:
        opts.append(f'resolve = "{entry}"')
    if extra:
        opts.extend(extra)
    return "\n".join(opts) + "\n"


def run_curl_with_config(config_text: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile("w", delete=False, prefix="curl-proxy-", suffix=".conf") as f:
        conf_path = Path(f.name)
        f.write(config_text)
    chmod_private(conf_path)
    try:
        return subprocess.run(
            ["curl", "--config", str(conf_path)],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    finally:
        try:
            conf_path.unlink()
        except OSError:
            pass


def chrome_doh_resolve(host: str, script: Path) -> str | None:
    url = f"https://dns.google/resolve?name={quote(host)}&type=A"
    proc = subprocess.run(
        ["osascript", str(script), url],
        text=True,
        capture_output=True,
        timeout=45,
    )
    if proc.returncode != 0:
        return None
    try:
        outer = json.loads(proc.stdout)
        inner = json.loads(outer.get("text") or "{}")
    except json.JSONDecodeError:
        return None
    for answer in inner.get("Answer") or []:
        if answer.get("type") == 1 and answer.get("data"):
            return str(answer["data"])
    return None


def google_doh_resolve(host: str) -> str | None:
    url = f"https://dns.google/resolve?name={quote(host)}&type=A"
    proc = subprocess.run(
        ["curl", "--location", "--fail", "--silent", "--show-error", "--max-time", "20", url],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    for answer in payload.get("Answer") or []:
        if answer.get("type") == 1 and answer.get("data"):
            return str(answer["data"])
    return None


def proxy_resolve_entry(proxy: str, out_dir: Path, chrome_doh: bool, google_doh: bool = False) -> list[str]:
    u = urlsplit(proxy)
    if not u.hostname or not u.port:
        return []
    cache_path = out_dir / "proxy_resolve.json"
    cache: dict[str, str] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            cache = {}
    key = f"{u.hostname}:{u.port}"
    ip = cache.get(key)
    if not ip and google_doh:
        ip = google_doh_resolve(u.hostname)
        if ip:
            cache[key] = ip
            cache_path.write_text(json.dumps(cache, indent=2))
            chmod_private(cache_path)
    if not ip and chrome_doh:
        script = Path(__file__).resolve().parent / "chrome_fetch_url.applescript"
        ip = chrome_doh_resolve(u.hostname, script)
        if ip:
            cache[key] = ip
            cache_path.write_text(json.dumps(cache, indent=2))
            chmod_private(cache_path)
    return [f"{key}:{ip}"] if ip else []


def test_proxies(
    subscription: Path,
    test_url: str,
    out_dir: Path,
    kind: str = "m3u8",
    chrome_doh: bool = False,
    google_doh: bool = False,
    referer: str = "https://www.bloomberg.com/",
) -> tuple[str, Path | None]:
    out_dir.mkdir(parents=True, exist_ok=True)
    nodes = load_subscription(subscription)
    candidates = []
    for node in nodes:
        proxy = normalize_proxy(node)
        if proxy and proxy_scheme(proxy) in SUPPORTED_PROXY_SCHEMES:
            candidates.append(proxy)
    if not candidates:
        raise SystemExit("No curl-compatible http/https/socks5 proxies found in subscription.")

    for index, proxy in enumerate(candidates, start=1):
        scheme = proxy_scheme(proxy)
        output = out_dir / f"proxy_test_{index:02d}.m3u8"
        started = time.time()
        extra = ['write-out = "%{http_code}"'] if kind == "http" else None
        resolve_entries = proxy_resolve_entry(proxy, out_dir, chrome_doh, google_doh)
        proc = run_curl_with_config(
            curl_config(proxy, test_url, output, extra=extra, resolve_entries=resolve_entries, referer=referer),
            timeout=75,
        )
        elapsed = time.time() - started
        size = output.stat().st_size if output.exists() else 0
        if kind == "http":
            ok = proc.returncode == 0 and proc.stdout.strip() in {"200", "204"}
        else:
            ok = proc.returncode == 0 and output.exists() and output.read_text(errors="ignore").startswith("#EXTM3U")
        status = "ok" if ok else f"fail:{proc.returncode}"
        print(f"[{index:02d}] {scheme:<6} {status:<8} {size:>7} bytes {elapsed:>5.1f}s", flush=True)
        if ok:
            proxy_path = out_dir / "working_proxy.url"
            proxy_path.write_text(proxy)
            chmod_private(proxy_path)
            if kind == "m3u8":
                selected_path = out_dir / "selected_variant.m3u8"
                selected_path.write_bytes(output.read_bytes())
                return proxy, selected_path
            return proxy, None

    raise SystemExit("No working curl-compatible proxy found.")


def fetch_playlist_with_proxy(
    proxy: str,
    playlist_url: str,
    out_dir: Path,
    chrome_doh: bool,
    google_doh: bool,
    referer: str,
) -> Path | None:
    selected_path = out_dir / "selected_variant.m3u8"
    resolve_entries = proxy_resolve_entry(proxy, out_dir, chrome_doh, google_doh)
    proc = run_curl_with_config(
        curl_config(proxy, playlist_url, selected_path, resolve_entries=resolve_entries, referer=referer),
        timeout=75,
    )
    if proc.returncode == 0 and selected_path.exists() and selected_path.read_text(errors="ignore").startswith("#EXTM3U"):
        return selected_path
    return None


def parse_playlist(text: str, base_url: str) -> tuple[list[str], list[str]]:
    lines = [line.strip() for line in text.splitlines()]
    output_lines: list[str] = []
    segment_urls: list[str] = []
    for line in lines:
        if not line or line.startswith("#"):
            if line.startswith("#EXT-X-MAP:") and 'URI="' in line:
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda m: f'URI="{urljoin(base_url, m.group(1))}"',
                    line,
                )
            output_lines.append(line)
            continue
        absolute = urljoin(base_url, line)
        segment_urls.append(absolute)
        output_lines.append(absolute)
    return output_lines, segment_urls


def playlist_cache_key(playlist_url: str) -> str:
    return hashlib.sha256(playlist_url.encode("utf-8")).hexdigest()[:12]


def segment_marker(output: Path) -> Path:
    return output.with_name(output.name + ".ok")


def segment_complete(output: Path) -> bool:
    return output.exists() and output.stat().st_size > 0


def cleanup_segment_artifacts(output: Path) -> None:
    for path in [output, segment_marker(output), *output.parent.glob(output.name + ".tmp.*")]:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def download_one(args: tuple[int, str, list[str], str, Path, str]) -> dict[str, object]:
    index, proxy, resolve_entries, url, output, referer = args
    marker = segment_marker(output)
    if segment_complete(output) and marker.exists():
        return {"index": index, "ok": True, "cached": True, "bytes": output.stat().st_size}
    if segment_complete(output):
        marker.write_text(str(output.stat().st_size))
        return {"index": index, "ok": True, "cached": True, "legacy_cached": True, "bytes": output.stat().st_size}

    temp_output = output.with_name(f"{output.name}.tmp.{os.getpid()}.{index}")
    try:
        temp_output.unlink()
    except FileNotFoundError:
        pass
    cfg = curl_config(
        proxy,
        url,
        temp_output,
        extra=["max-time = 180", "retry = 4", "retry-all-errors"],
        resolve_entries=resolve_entries,
        referer=referer,
    )
    try:
        proc = run_curl_with_config(cfg, timeout=220)
        ok = proc.returncode == 0 and temp_output.exists() and temp_output.stat().st_size > 0
    except subprocess.TimeoutExpired as exc:
        ok = False
        returncode: int | str = "timeout"
        stderr = f"curl timeout after {exc.timeout}s"
    except Exception as exc:
        ok = False
        returncode = exc.__class__.__name__
        stderr = repr(exc)
    else:
        returncode = proc.returncode
        stderr = proc.stderr[-300:] if proc.stderr else ""

    if ok:
        temp_output.replace(output)
        marker.write_text(str(output.stat().st_size))
    else:
        try:
            temp_output.unlink()
        except FileNotFoundError:
            pass
    return {
        "index": index,
        "ok": ok,
        "cached": False,
        "bytes": output.stat().st_size if output.exists() else 0,
        "returncode": returncode,
        "stderr": stderr,
    }


def download_segments(
    proxy: str,
    playlist_url: str,
    playlist_path: Path,
    out_dir: Path,
    workers: int,
    chrome_doh: bool,
    google_doh: bool,
    referer: str,
    rounds: int = 3,
) -> Path:
    text = playlist_path.read_text(errors="ignore")
    if "#EXT-X-KEY" in text.upper():
        raise SystemExit("Selected HLS playlist is encrypted (#EXT-X-KEY); refusing to download.")
    playlist_lines, urls = parse_playlist(text, playlist_url)
    if not urls:
        raise SystemExit("No media segments found in selected playlist.")

    cache_key = playlist_cache_key(playlist_url)
    segment_dir = out_dir / f"segments_{cache_key}"
    segment_dir.mkdir(parents=True, exist_ok=True)
    local_lines: list[str] = []
    tasks = []
    resolve_entries = proxy_resolve_entry(proxy, out_dir, chrome_doh, google_doh)
    for line in playlist_lines:
        if not line or line.startswith("#"):
            local_lines.append(line)
            continue
        index = len(tasks)
        ext = Path(urlsplit(line).path).suffix or ".ts"
        target = segment_dir / f"seg_{index:05d}{ext}"
        tasks.append((index, proxy, resolve_entries, line, target, referer))
        local_lines.append(str(target.resolve()))

    local_playlist = out_dir / f"local_video_{cache_key}.m3u8"
    local_playlist.write_text("\n".join(local_lines) + "\n")

    max_rounds = max(1, rounds)
    task_by_index = {task[0]: task for task in tasks}
    pending = tasks
    last_failures: list[dict[str, object]] = []
    for round_no in range(1, max_rounds + 1):
        print(
            f"Downloading {len(pending)} segments with {workers} workers "
            f"(round {round_no}/{max_rounds})",
            flush=True,
        )
        failures: list[dict[str, object]] = []
        completed = 0
        cached = 0
        total_bytes = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(download_one, task): task for task in pending}
            for future in concurrent.futures.as_completed(futures):
                task = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "index": task[0],
                        "ok": False,
                        "cached": False,
                        "bytes": 0,
                        "returncode": exc.__class__.__name__,
                        "stderr": repr(exc),
                    }
                    cleanup_segment_artifacts(task[4])
                completed += 1
                total_bytes += int(result.get("bytes") or 0)
                if result.get("cached"):
                    cached += 1
                if not result["ok"]:
                    failures.append(result)
                if completed % 25 == 0 or completed == len(pending):
                    print(
                        f"  {completed}/{len(pending)} segments, "
                        f"{total_bytes / 1024 / 1024:.1f} MiB, "
                        f"{cached} cached, {len(failures)} failed",
                        flush=True,
                    )

        missing = [task for task in tasks if not segment_complete(task[4])]
        if not failures and not missing:
            return local_playlist
        if not missing:
            for task in tasks:
                marker = segment_marker(task[4])
                if not marker.exists():
                    marker.write_text(str(task[4].stat().st_size))
            return local_playlist

        last_failures = failures or [
            {
                "index": task[0],
                "ok": False,
                "cached": False,
                "bytes": 0,
                "returncode": "missing",
                "stderr": "",
            }
            for task in missing
        ]
        for failure in last_failures:
            task = task_by_index.get(int(failure.get("index", -1)))
            if task:
                cleanup_segment_artifacts(task[4])
        pending = missing
        if round_no < max_rounds:
            time.sleep(min(2 * round_no, 8))

    fail_path = out_dir / "segment_failures.json"
    fail_path.write_text(json.dumps(last_failures, indent=2))
    raise SystemExit(f"{len(last_failures)} segment downloads failed after {max_rounds} rounds; see {fail_path}")


def remux(local_playlist: Path, output_mp4: Path) -> None:
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output_mp4.with_name(output_mp4.name + ".remuxing.mp4")
    base = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-allowed_extensions",
        "ALL",
        "-protocol_whitelist",
        "file,http,https,tcp,tls,crypto",
    ]
    attempts = [
        [
            *base,
            "-i",
            str(local_playlist),
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-movflags",
            "+faststart",
            str(temp_output),
        ],
        [
            *base,
            "-fflags",
            "+genpts+discardcorrupt",
            "-err_detect",
            "ignore_err",
            "-i",
            str(local_playlist),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(temp_output),
        ],
    ]
    last_error = ""
    for index, cmd in enumerate(attempts, start=1):
        try:
            temp_output.unlink()
        except FileNotFoundError:
            pass
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode == 0 and temp_output.exists() and temp_output.stat().st_size > 0:
            temp_output.replace(output_mp4)
            return
        last_error = (proc.stderr or proc.stdout or "").strip()
        if index == 1:
            print("Strict remux failed; retrying with corrupt packet discard", flush=True)
    raise SystemExit(f"ffmpeg remux failed: {last_error[-800:]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription", type=Path, default=Path("tmp/proxy_sub.raw"))
    parser.add_argument("--playlist-url", required=True)
    parser.add_argument("--proxy-test-url")
    parser.add_argument("--work-dir", type=Path, default=Path("tmp/hls_work"))
    parser.add_argument("--output", type=Path, default=Path("downloads/the_china_show_2026_06_05.mp4"))
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--segment-rounds", type=int, default=3)
    parser.add_argument("--test-only", action="store_true")
    parser.add_argument("--chrome-doh", action="store_true")
    parser.add_argument("--google-doh", action="store_true")
    parser.add_argument("--referer", default="https://www.bloomberg.com/")
    args = parser.parse_args()

    proxy_file = args.work_dir / "working_proxy.url"
    selected_playlist = args.work_dir / "selected_variant.m3u8"
    if args.proxy_test_url:
        test_proxies(
            args.subscription,
            args.proxy_test_url,
            args.work_dir,
            kind="http",
            chrome_doh=args.chrome_doh,
            google_doh=args.google_doh,
            referer=args.referer,
        )
        return 0
    proxy = proxy_file.read_text().strip() if proxy_file.exists() else ""
    if proxy:
        selected_playlist = fetch_playlist_with_proxy(
            proxy,
            args.playlist_url,
            args.work_dir,
            args.chrome_doh,
            args.google_doh,
            args.referer,
        )
        if selected_playlist:
            print("Using cached working proxy", flush=True)
        else:
            print("Cached working proxy failed; scanning subscription", flush=True)
            proxy = ""
    if not proxy:
        proxy, selected_playlist = test_proxies(
            args.subscription,
            args.playlist_url,
            args.work_dir,
            chrome_doh=args.chrome_doh,
            google_doh=args.google_doh,
            referer=args.referer,
        )

    if args.test_only:
        return 0

    local_playlist = download_segments(
        proxy,
        args.playlist_url,
        selected_playlist,
        args.work_dir,
        args.workers,
        args.chrome_doh,
        args.google_doh,
        args.referer,
        args.segment_rounds,
    )
    remux(local_playlist, args.output)
    print(f"Wrote {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
