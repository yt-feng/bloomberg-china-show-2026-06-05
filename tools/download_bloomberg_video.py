#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import select
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proxy_hls_downloader as hls_downloader  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parent

ACTIVATE_SCRIPT = TOOLS / "activate_bloomberg_tab.applescript"
PROBE_SCRIPT = TOOLS / "chrome_media_probe.applescript"
FETCH_TEXT_SCRIPT = TOOLS / "chrome_fetch_text.applescript"
DOWNLOADER = TOOLS / "proxy_hls_downloader.py"

DEFAULT_SUBSCRIPTION = ROOT / "tmp/proxy_sub.raw"
DEFAULT_SUBSCRIPTION_URL_FILE = ROOT / "tmp/proxy_subscription_url.txt"
DEFAULT_PROXY_CACHE = ROOT / "tmp/working_proxy.url"
DEFAULT_PROXY_TEST_URL = "https://www.google.com/generate_204"

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
HLS_URL_RE = re.compile(r"https?://[^\s'\"<>]+?\.m3u8(?:\?[^\s'\"<>]*)?", re.IGNORECASE)
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)
MANIFEST_FETCH_TIMEOUT = 35
HLS_FETCH_TIMEOUT = 45


class FetchError(RuntimeError):
    pass


class ProxyFetchError(FetchError):
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


def read_http_header(sock: socket.socket, limit: int = 1024 * 1024) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            raise OSError("HTTP header too large")
    return bytes(data)


def proxy_auth_header(proxy_url: str) -> str | None:
    parts = urlsplit(proxy_url)
    if parts.username is None:
        return None
    username = unquote(parts.username)
    password = unquote(parts.password or "")
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Proxy-Authorization: Basic {token}"


def rewrite_proxy_request(header: bytes, auth_header: str | None) -> bytes:
    text = header.decode("iso-8859-1", "replace")
    head, _, rest = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    output = [lines[0]]
    for line in lines[1:]:
        lower = line.lower()
        if lower.startswith("proxy-authorization:") or lower.startswith("proxy-connection:"):
            continue
        output.append(line)
    if auth_header:
        output.append(auth_header)
    return ("\r\n".join(output) + "\r\n\r\n" + rest).encode("iso-8859-1")


def tunnel_sockets(left: socket.socket, right: socket.socket, timeout: int = 120) -> None:
    sockets = [left, right]
    while sockets:
        readable, _, _ = select.select(sockets, [], [], timeout)
        if not readable:
            break
        for source in readable:
            try:
                data = source.recv(65536)
            except OSError:
                return
            if not data:
                return
            target = right if source is left else left
            try:
                target.sendall(data)
            except OSError:
                return


class LocalProxyServer:
    def __init__(self, upstream_proxy: str, *, google_doh: bool) -> None:
        self.upstream_proxy = upstream_proxy
        self.google_doh = google_doh
        self.server: socketserver.ThreadingTCPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> str:
        parent = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                parent.handle_client(self.request)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2)

    def connect_upstream(self) -> socket.socket:
        parts = urlsplit(self.upstream_proxy)
        if parts.scheme not in {"http", "https"}:
            raise OSError(f"Local forwarding only supports http/https proxies, got {parts.scheme}")
        if not parts.hostname or not parts.port:
            raise OSError("Proxy URL must include host and port")
        connect_host = parts.hostname
        if self.google_doh:
            connect_host = hls_downloader.google_doh_resolve(parts.hostname) or connect_host
        raw = socket.create_connection((connect_host, parts.port), timeout=30)
        if parts.scheme == "https":
            context = ssl.create_default_context()
            return context.wrap_socket(raw, server_hostname=parts.hostname)
        return raw

    def handle_client(self, client: socket.socket) -> None:
        client.settimeout(60)
        upstream: socket.socket | None = None
        try:
            request = read_http_header(client)
            if not request:
                return
            upstream = self.connect_upstream()
            upstream.settimeout(60)
            upstream.sendall(rewrite_proxy_request(request, proxy_auth_header(self.upstream_proxy)))
            response = read_http_header(upstream)
            if response:
                client.sendall(response)
            tunnel_sockets(client, upstream)
        except OSError:
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        finally:
            try:
                client.close()
            except OSError:
                pass
            if upstream:
                try:
                    upstream.close()
                except OSError:
                    pass


def chrome_binary() -> str:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "google-chrome",
        "chromium",
    ]
    for candidate in candidates:
        if candidate.startswith("/") and Path(candidate).exists():
            return candidate
        if not candidate.startswith("/") and shutil.which(candidate):
            return candidate
    raise SystemExit("No Chrome/Chromium binary found for headless mode.")


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


def is_hls_url(url: str) -> bool:
    return ".m3u8" in urlsplit(url).path.lower()


def is_haystack_url(url: str) -> bool:
    return "haystack.tv" in urlsplit(url).netloc.lower()


def is_bloomberg_url(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host.endswith("bloomberg.com") or host.endswith("bcc.bloomberg.com")


def bloomberg_brp_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.netloc.lower().endswith("bloomberg.com"):
        return url
    return parsed._replace(scheme="https", netloc="brp-prod-bcc.bloomberg.com").geturl()


def fetch_text_with_curl(url: str, timeout: int = 90) -> str:
    try:
        proc = subprocess.run(
            [
                "curl",
                "--location",
                "--fail",
                "--silent",
                "--show-error",
                "--http1.1",
                "--compressed",
                "--retry",
                "2",
                "--retry-delay",
                "1",
                "--retry-max-time",
                str(timeout),
                "--connect-timeout",
                str(min(15, max(5, timeout // 3))),
                "--max-time",
                str(timeout),
                "--speed-time",
                str(min(20, max(5, timeout // 2))),
                "--speed-limit",
                "1",
                "--user-agent",
                BROWSER_UA,
                "--header",
                "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/vnd.apple.mpegurl,application/x-mpegURL,application/json,text/plain,*/*;q=0.8",
                "--header",
                "Accept-Language: en-US,en;q=0.9",
                url,
            ],
            text=True,
            capture_output=True,
            timeout=timeout + 10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise FetchError(f"curl fetch failed: {exc}") from exc
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or "").strip()
        raise FetchError(f"curl fetch failed: {detail or proc.returncode}")
    return proc.stdout


def fetch_text_direct(url: str, timeout: int = 90) -> str:
    curl_error = ""
    try:
        return fetch_text_with_curl(url, timeout=timeout)
    except FetchError as curl_exc:
        curl_error = str(curl_exc)
        curl_message = curl_error.lower()
        if "timed out" in curl_message or "timeout" in curl_message:
            raise FetchError(f"Direct fetch timed out: {curl_exc}") from curl_exc

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/vnd.apple.mpegurl,application/x-mpegURL,text/plain,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return payload.decode(charset, "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"Direct fetch failed: {curl_error}; urllib failed: {exc}") from exc


def haystack_ids_from_html(text: str) -> list[str]:
    ids: list[str] = []

    def add(value: str) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]{6,}", value) and value not in ids:
            ids.append(value)

    head = text.split("</head>", 1)[0]
    primary_patterns = [
        r"haystackTV://playVideo\?index=http://haystack\.tv/id/([A-Za-z0-9_-]+)",
        r"haystack-thumbnails/bloomberg/([A-Za-z0-9_-]+)/",
        r"cloudfront\.net/bloomberg/([A-Za-z0-9_-]+)/",
    ]
    for pattern in primary_patterns:
        for match in re.finditer(pattern, head):
            add(match.group(1))
    if ids:
        return ids

    patterns = [
        r"haystack-thumbnails/bloomberg/([A-Za-z0-9_-]+)/",
        r"cloudfront\.net/bloomberg/([A-Za-z0-9_-]+)/",
        r"haystackTV://playVideo\?index=http://haystack\.tv/id/([A-Za-z0-9_-]+)",
        r"haystack\.tv/id/([A-Za-z0-9_-]+)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            add(match.group(1))
    return ids


def haystack_hls_candidates_from_html(text: str) -> list[str]:
    urls: list[str] = []
    media_ids = haystack_ids_from_html(text)
    for media_id in media_ids:
        token = f"/bloomberg/{media_id}/"
        urls.extend(url for url in extract_hls_urls(text) if token in url)
        urls.append(f"https://d2ufudlfb4rsg4.cloudfront.net/bloomberg/{media_id}/adaptive/{media_id}_master.m3u8")
    return list(dict.fromkeys(urls))


def discover_haystack_candidates(url: str, work_dir: Path) -> list[str]:
    log("Fetching Haystack page directly")
    text = fetch_text_direct(url, timeout=90)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "haystack_page.html").write_text(text)
    candidates = haystack_hls_candidates_from_html(text)
    if not candidates:
        raise SystemExit("No Haystack HLS candidates found.")
    log(f"Found {len(candidates)} Haystack HLS candidate(s)")
    return candidates


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


def normalize_embedded_script_text(text: str) -> str:
    return html.unescape(normalize_probe_text(text)).replace('\\"', '"')


def probe_from_html(url: str, text: str, ready: str) -> dict:
    probe = {
        "url": url,
        "title": html_title(text),
        "ready": ready,
        "textSample": html_text_sample(text),
        "scripts": script_texts_from_html(text),
        "links": re.findall(r'''(?i)(?:href|src)=["']([^"']+)["']''', text),
        "videos": [],
        "sources": [],
        "performance": [],
        "globals": [],
        "assetIds": [],
        "manifestUrls": [],
        "m3u8Urls": [],
    }
    probe["assetIds"] = extract_asset_ids(probe)
    probe["m3u8Urls"] = extract_hls_urls(probe)
    return probe


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


def extract_url_bound_asset_ids(probe: dict, page_url: str) -> list[str]:
    target_path = urlsplit(page_url).path
    if not target_path:
        return []

    found: list[str] = []

    def add(value: str) -> None:
        value = value.lower()
        if re.fullmatch(UUID_RE, value, flags=re.IGNORECASE) and value not in found:
            found.append(value)

    object_pattern = re.compile(r"\{[^{}]{0,2600}\}", re.DOTALL)
    asset_pattern = re.compile(rf'assetI[Dd]"\s*:\s*"({UUID_RE})"', re.IGNORECASE)
    page_id_pattern = re.compile(
        rf'pageId"\s*:\s*"({UUID_RE})".{{0,1200}}"name"\s*:\s*"parsely-link"\s*,\s*"content"\s*:\s*"[^"]*{re.escape(target_path)}"',
        re.IGNORECASE | re.DOTALL,
    )

    for text in walk_strings(probe):
        text = normalize_embedded_script_text(text)
        if target_path not in text:
            continue
        for object_match in object_pattern.finditer(text):
            item = object_match.group(0)
            if target_path not in item:
                continue
            asset_match = asset_pattern.search(item)
            if asset_match:
                add(asset_match.group(1))
        for match in page_id_pattern.finditer(text):
            add(match.group(1))
    return found


def choose_asset_id(probe: dict, page_url: str, candidates: list[str], override: str | None = None) -> str | None:
    if override:
        override = override.strip().lower()
        if not re.fullmatch(UUID_RE, override, flags=re.IGNORECASE):
            raise SystemExit(f"--asset-id is not a valid UUID: {override}")
        return override

    url_bound = extract_url_bound_asset_ids(probe, page_url)
    if url_bound:
        return url_bound[0]
    return candidates[0] if candidates else None


def cached_asset_id_for_url(page_url: str) -> str | None:
    tmp_dir = ROOT / "tmp"
    if not tmp_dir.exists():
        return None
    for path in sorted(tmp_dir.glob("auto_*/media_probe.json"), reverse=True):
        try:
            probe = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        matches = extract_url_bound_asset_ids(probe, page_url)
        if matches:
            return matches[0]
    return None


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


def fetch_text_via_proxy(
    url: str,
    work_dir: Path,
    label: str,
    proxy: str,
    *,
    google_doh: bool,
    referer: str,
    timeout: int = 90,
) -> str:
    work_dir.mkdir(parents=True, exist_ok=True)
    output = work_dir / f"{safe_file_part(label)}.txt"
    resolve_entries = hls_downloader.proxy_resolve_entry(
        proxy,
        work_dir,
        chrome_doh=False,
        google_doh=google_doh,
    )
    config = hls_downloader.curl_config(
        proxy,
        url,
        output,
        extra=[
            "http1.1",
            "compressed",
            f"max-time = {timeout}",
            f'user-agent = "{BROWSER_UA}"',
            'header = "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/vnd.apple.mpegurl,application/x-mpegURL,text/plain,*/*;q=0.8"',
            'header = "Accept-Language: en-US,en;q=0.9"',
            'header = "Cache-Control: no-cache"',
            'header = "Pragma: no-cache"',
            'header = "Sec-Fetch-Dest: document"',
            'header = "Sec-Fetch-Mode: navigate"',
            'header = "Sec-Fetch-Site: none"',
            'header = "Upgrade-Insecure-Requests: 1"',
            'write-out = "%{http_code}"',
        ],
        resolve_entries=resolve_entries,
        referer=referer,
    )
    proc = hls_downloader.run_curl_with_config(config, timeout=timeout)
    status = (proc.stdout or "").strip()
    if proc.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        detail = (proc.stderr or status or "").strip()
        raise ProxyFetchError(f"Proxy fetch failed for {label}: {detail}")
    if status and status not in {"200", "204"}:
        raise ProxyFetchError(f"Proxy fetch returned HTTP {status} for {label}")
    return output.read_text(errors="ignore")


def html_title(text: str) -> str:
    for pattern in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']parsely-title["\'][^>]+content=["\']([^"\']+)["\']',
        r"<title[^>]*>(.*?)</title>",
    ):
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            return html.unescape(re.sub(r"\s+", " ", match.group(1)).strip())
    return ""


def html_text_sample(text: str, limit: int = 1200) -> str:
    body = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    body = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html.unescape(body)
    return re.sub(r"\s+", " ", body).strip()[:limit]


def script_texts_from_html(text: str) -> list[str]:
    scripts = []
    for match in re.finditer(r"(?is)<script[^>]*>(.*?)</script>", text):
        content = match.group(1)
        if content and re.search(r"assetI[Dd]|media-manifest|m3u8|playlistItems|currentVideo", content):
            scripts.append(content)
    if not scripts:
        scripts.append(text)
    return scripts


def probe_page_via_proxy(url: str, work_dir: Path, fetcher) -> dict:
    log("Fetching Bloomberg page in background via proxy")
    text = fetcher(url, "page_html", timeout=120)
    probe = probe_from_html(url, text, "proxy-fetch")
    (work_dir / "media_probe.json").write_text(json.dumps(probe, indent=2))
    return probe


def probe_page_via_brp(url: str, work_dir: Path) -> dict:
    brp_url = bloomberg_brp_url(url)
    log("Fetching Bloomberg page from BRP background endpoint")
    text = fetch_text_direct(brp_url, timeout=120)
    if "Are you a robot" in text and "Bloomberg" in text:
        raise FetchError("BRP endpoint returned the Bloomberg robot page")
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "page_brp.html").write_text(text)
    probe = probe_from_html(url, text, "brp-direct")
    (work_dir / "media_probe.json").write_text(json.dumps(probe, indent=2))
    return probe


def probe_page_via_headless(url: str, work_dir: Path, proxy: str, *, google_doh: bool) -> dict:
    parts = urlsplit(proxy)
    if parts.scheme not in {"http", "https"}:
        raise SystemExit("Headless mode currently requires an http/https proxy node.")
    profile_dir = work_dir / "headless_chrome_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    log("Fetching Bloomberg page with isolated headless Chrome")
    target_url = f"view-source:{url}"
    with LocalProxyServer(proxy, google_doh=google_doh) as local_proxy:
        proc = run(
            [
                chrome_binary(),
                "--headless=new",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-javascript",
                "--disable-sync",
                "--disable-translate",
                "--hide-scrollbars",
                "--blink-settings=imagesEnabled=false",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={profile_dir}",
                f"--proxy-server={local_proxy}",
                "--proxy-bypass-list=<-loopback>",
                "--window-size=1440,1200",
                "--timeout=30000",
                "--virtual-time-budget=3000",
                "--dump-dom",
                target_url,
            ],
            check=False,
            capture=True,
            timeout=45,
        )
    text = proc.stdout or ""
    if proc.returncode != 0 or not text.strip():
        detail = (proc.stderr or text or "").strip()
        raise ProxyFetchError(f"Headless Chrome failed to fetch page: {detail[-800:]}")
    if "line-content" in text and "&lt;" in text:
        fragments = re.findall(r'<span class="line-content">(.*?)</span>', text, flags=re.DOTALL)
        if fragments:
            text = "\n".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)) for fragment in fragments)
    (work_dir / "page_headless.html").write_text(text)
    probe = probe_from_html(url, text, "headless-chrome")
    (work_dir / "media_probe.json").write_text(json.dumps(probe, indent=2))
    return probe


def fetch_embed_manifest_direct(asset_id: str, work_dir: Path) -> tuple[dict, str]:
    url = f"https://www.bloomberg.com/media-manifest/embed?id={asset_id}&variant=LOOP&streamType=HD"
    log(f"Fetching Bloomberg embed manifest for asset {asset_id}")
    text = fetch_text_direct(url, timeout=MANIFEST_FETCH_TIMEOUT)
    (work_dir / "embed_manifest.json").write_text(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchError("Direct Bloomberg embed manifest was not valid JSON.") from exc
    if not isinstance(data, dict):
        raise FetchError("Direct Bloomberg embed manifest was not a JSON object.")
    return data, url


def fetch_embed_manifest(
    asset_id: str,
    work_dir: Path,
    fetcher,
    fetcher_kind: str,
    *,
    try_direct_first: bool = True,
) -> tuple[dict, str]:
    url = f"https://www.bloomberg.com/media-manifest/embed?id={asset_id}&variant=LOOP&streamType=HD"
    if try_direct_first:
        try:
            return fetch_embed_manifest_direct(asset_id, work_dir)
        except FetchError as direct_exc:
            if fetcher_kind == "direct":
                raise
            log(f"Direct embed manifest fetch failed; trying current fetcher: {direct_exc}")
    text = fetcher(url, "embed_manifest", timeout=MANIFEST_FETCH_TIMEOUT)
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


def discover_variants(urls: list[str], work_dir: Path, fetcher) -> list[Variant]:
    work_dir.mkdir(parents=True, exist_ok=True)
    variants: list[Variant] = []
    for index, url in enumerate(urls, start=1):
        try:
            log(f"Fetching HLS candidate {index}/{len(urls)}")
            text = fetcher(url, f"hls_{index:02d}", timeout=HLS_FETCH_TIMEOUT)
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
    return "bbgvod" in host or "fastly.net" in host or "akamaized.net" in host or "bloomberg.com" in host


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


def cached_proxy(args: argparse.Namespace, work_dir: Path) -> str | None:
    cache_path = resolve_path(args.proxy_cache)
    if not cache_path.exists() or cache_path.stat().st_size == 0:
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    proxy = cache_path.read_text().strip()
    if not proxy:
        return None
    target = work_dir / "working_proxy.url"
    target.write_text(proxy)
    chmod_private(target)
    log(f"Using cached working proxy from {rel(cache_path)}")
    return proxy


def scan_working_proxy(args: argparse.Namespace, subscription: Path, work_dir: Path) -> str:
    log("Scanning proxy subscription in background")
    proxy, _ = hls_downloader.test_proxies(
        subscription,
        args.proxy_test_url or DEFAULT_PROXY_TEST_URL,
        work_dir,
        kind="http",
        chrome_doh=False,
        google_doh=args.google_doh,
        referer=args.url,
    )
    save_proxy_cache(work_dir, args.proxy_cache)
    return proxy


def build_proxy_fetcher(args: argparse.Namespace, subscription: Path, work_dir: Path):
    proxy = cached_proxy(args, work_dir)
    if not proxy:
        proxy = scan_working_proxy(args, subscription, work_dir)

    def fetcher(url: str, label: str, timeout: int = 90) -> str:
        return fetch_text_via_proxy(
            url,
            work_dir,
            label,
            proxy,
            google_doh=args.google_doh,
            referer=args.url,
            timeout=timeout,
        )

    return fetcher, proxy


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


def yt_dlp_base_command(args: argparse.Namespace) -> list[str] | None:
    explicit = (args.yt_dlp_bin or os.environ.get("YTDLP_BIN") or "").strip()
    if explicit:
        return [explicit]

    binary = shutil.which("yt-dlp")
    if binary:
        return [binary]

    for candidate in [
        ROOT / ".venv/bin/yt-dlp",
        Path.home() / ".local/bin/yt-dlp",
        Path("/opt/homebrew/bin/yt-dlp"),
        Path("/usr/local/bin/yt-dlp"),
        Path("/private/tmp/bbg-ytdlp-venv/bin/yt-dlp"),
    ]:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return [str(candidate)]

    try:
        probe = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            text=True,
            capture_output=True,
            timeout=20,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if probe.returncode == 0:
        return [sys.executable, "-m", "yt_dlp"]
    return None


def yt_dlp_output_template(output: Path) -> str:
    if output.suffix:
        return str(output.with_suffix(".%(ext)s"))
    return str(output) + ".%(ext)s"


def cleanup_ytdlp_partials(output: Path) -> None:
    for pattern in [
        output.name + ".part",
        output.name + ".part-*",
        output.name + ".ytdl",
        output.with_suffix(".part").name,
        output.with_suffix(".part").name + "-*",
    ]:
        for path in output.parent.glob(pattern):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def cached_proxy_for_download(args: argparse.Namespace, work_dir: Path) -> str | None:
    paths = [work_dir / "working_proxy.url", resolve_path(args.proxy_cache)]
    for path in paths:
        if path.exists() and path.stat().st_size > 0:
            proxy = path.read_text().strip()
            if proxy:
                return proxy
    return None


def run_ytdlp_process(cmd: list[str], *, output: Path) -> bool:
    try:
        proc = subprocess.run(cmd, text=True)
    except FileNotFoundError:
        log("yt-dlp command was not found")
        return False
    if proc.returncode != 0:
        log(f"yt-dlp exited with status {proc.returncode}")
        cleanup_ytdlp_partials(output)
        return False
    if output.exists() and output.stat().st_size > 0:
        return True
    log(f"yt-dlp finished but expected output was not found: {rel(output)}")
    cleanup_ytdlp_partials(output)
    return False


def run_ytdlp_downloader(args: argparse.Namespace, variant: Variant, work_dir: Path, output: Path) -> bool:
    base_command = yt_dlp_base_command(args)
    if not base_command:
        log("yt-dlp is not installed; falling back to the built-in segmented downloader")
        return False

    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        *base_command,
        "--no-warnings",
        "--newline",
        "--no-playlist",
        "--hls-prefer-native",
        "--concurrent-fragments",
        str(max(1, args.workers)),
        "--retries",
        "10",
        "--fragment-retries",
        "10",
        "--merge-output-format",
        "mp4",
        "--remux-video",
        "mp4",
        "--referer",
        args.url,
        "--user-agent",
        BROWSER_UA,
        "-f",
        "best",
        "-o",
        yt_dlp_output_template(output),
        variant.url,
    ]

    tried_direct = False
    if args.yt_dlp_proxy_mode in {"auto", "never"}:
        log("Starting yt-dlp HLS download without proxy")
        tried_direct = True
        if run_ytdlp_process(cmd, output=output):
            return True
        if args.yt_dlp_proxy_mode == "never":
            return False
        log("Direct yt-dlp download failed; trying cached proxy")

    proxy = cached_proxy_for_download(args, work_dir)
    if not proxy:
        if args.yt_dlp_proxy_mode == "always":
            raise SystemExit("yt-dlp proxy mode is 'always', but no cached working proxy is available.")
        if tried_direct:
            log("No cached working proxy for yt-dlp")
            return False
        log("No cached working proxy for yt-dlp; trying direct HLS download")
        return run_ytdlp_process(cmd, output=output)

    scheme = hls_downloader.proxy_scheme(proxy)
    if scheme not in {"http", "https"}:
        if args.yt_dlp_proxy_mode == "always":
            raise SystemExit("yt-dlp proxy mode 'always' requires an http/https proxy for credential-safe forwarding.")
        if tried_direct:
            log(f"Cached proxy scheme {scheme or 'unknown'} cannot be forwarded safely")
            return False
        log(f"Cached proxy scheme {scheme or 'unknown'} cannot be forwarded safely; trying yt-dlp without proxy")
        return run_ytdlp_process(cmd, output=output)

    log("Starting yt-dlp HLS download through a local proxy forwarder")
    with LocalProxyServer(proxy, google_doh=args.google_doh) as local_proxy:
        proxied_cmd = [*cmd[:-1], "--proxy", local_proxy, cmd[-1]]
        return run_ytdlp_process(proxied_cmd, output=output)


def run_segmented_downloader(
    args: argparse.Namespace,
    variant: Variant,
    subscription: Path,
    work_dir: Path,
    output: Path,
) -> None:
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
        "--segment-rounds",
        str(args.segment_rounds),
        "--google-doh",
        "--referer",
        args.url,
    ]
    log("Starting segmented HLS download")
    run(cmd, capture=False)
    save_proxy_cache(work_dir, args.proxy_cache)


def run_downloader(args: argparse.Namespace, variant: Variant, subscription: Path, work_dir: Path, output: Path) -> None:
    if args.download_backend in {"auto", "yt-dlp"}:
        if run_ytdlp_downloader(args, variant, work_dir, output):
            save_proxy_cache(work_dir, args.proxy_cache)
            return
        if args.download_backend == "yt-dlp":
            raise SystemExit("yt-dlp download failed.")
        log("Falling back to built-in segmented HLS downloader")

    if not subscription.exists():
        subscription = ensure_subscription(args)
    run_segmented_downloader(args, variant, subscription, work_dir, output)


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
    parser = argparse.ArgumentParser(description="Download a Bloomberg video URL through the direct-first background workflow.")
    parser.add_argument("--url", required=True, help="Bloomberg video page URL.")
    parser.add_argument("--subscription", type=Path, default=DEFAULT_SUBSCRIPTION)
    parser.add_argument("--subscription-url", help="Proxy subscription URL. Prefer env/file for normal use.")
    parser.add_argument("--subscription-url-file", type=Path, default=DEFAULT_SUBSCRIPTION_URL_FILE)
    parser.add_argument("--refresh-subscription", action="store_true")
    parser.add_argument("--fetch-mode", choices=("headless", "proxy", "chrome"), default="headless")
    parser.add_argument("--proxy-test-url", help=f"URL used when scanning subscription nodes. Defaults to {DEFAULT_PROXY_TEST_URL}.")
    parser.add_argument("--google-doh", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--segment-rounds", type=int, default=3, help="Fallback downloader retry rounds for missing or failed HLS segments.")
    parser.add_argument("--download-backend", choices=("auto", "yt-dlp", "custom"), default="auto")
    parser.add_argument("--yt-dlp-bin", help="Path to yt-dlp. Defaults to YTDLP_BIN, PATH, then python -m yt_dlp.")
    parser.add_argument("--yt-dlp-proxy-mode", choices=("auto", "never", "always"), default="auto")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "downloads")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--proxy-cache", type=Path, default=DEFAULT_PROXY_CACHE)
    parser.add_argument("--keep-tmp", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Discover and select the HLS variant, but do not download.")
    parser.add_argument("--force", action="store_true", help="Replace an existing output file.")
    parser.add_argument("--asset-id", help="Debug override for Bloomberg assetId.")
    parser.add_argument("--no-open", action="store_true", help="Chrome mode only: use the current Chrome tab instead of opening the URL.")
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
        subscription: Path | None = None
        if is_hls_url(args.url):
            candidates = [args.url]

            def fetcher(url: str, label: str, timeout: int = 90) -> str:
                del label
                return fetch_text_direct(url, timeout=timeout)

            log("Input URL is an HLS playlist; skipping page discovery")
            log(f"Evaluating {len(candidates)} HLS candidate(s)")
            variants = discover_variants(candidates, work_dir, fetcher)
            selected = select_best_variant(variants)
            log(f"Selected HLS variant: {variant_summary(selected)}")
            log(selected.url)
            write_plan(work_dir, args.url, None, candidates, selected, output)
            if args.dry_run:
                log(f"Dry run complete; planned output: {rel(output)}")
                success = True
                return 0
            if args.download_backend == "custom":
                subscription = ensure_subscription(args)
            else:
                subscription = resolve_path(args.subscription)
            run_downloader(args, selected, subscription, work_dir, output)
            verify_output(output)
            success = True
            return 0

        if is_haystack_url(args.url):
            candidates = discover_haystack_candidates(args.url, work_dir)

            def fetcher(url: str, label: str, timeout: int = 90) -> str:
                del label
                return fetch_text_direct(url, timeout=timeout)

            log(f"Evaluating {len(candidates)} HLS candidate(s)")
            variants = discover_variants(candidates, work_dir, fetcher)
            selected = select_best_variant(variants)
            log(f"Selected HLS variant: {variant_summary(selected)}")
            log(selected.url)
            write_plan(work_dir, args.url, None, candidates, selected, output)
            if args.dry_run:
                log(f"Dry run complete; planned output: {rel(output)}")
                success = True
                return 0
            if args.download_backend == "custom":
                subscription = ensure_subscription(args)
            else:
                subscription = resolve_path(args.subscription)
            run_downloader(args, selected, subscription, work_dir, output)
            verify_output(output)
            success = True
            return 0

        probe: dict | None = None
        cached_asset_id = None if args.asset_id else cached_asset_id_for_url(args.url)
        asset_id = args.asset_id or cached_asset_id

        fetcher_kind = "direct"

        def fetcher(url: str, label: str, timeout: int = 90) -> str:
            del label
            return fetch_text_direct(url, timeout=timeout)

        if not asset_id and is_bloomberg_url(args.url):
            try:
                probe = probe_page_via_brp(args.url, work_dir)
            except FetchError as exc:
                log(f"BRP background discovery failed: {exc}")
            else:
                brp_asset_ids = extract_asset_ids(probe)
                asset_id = choose_asset_id(probe, args.url, brp_asset_ids, None)

        if not asset_id:
            if args.fetch_mode in {"headless", "proxy"}:
                subscription = ensure_subscription(args)
                fetcher, proxy = build_proxy_fetcher(args, subscription, work_dir)
                fetcher_kind = "proxy"
                try:
                    if args.fetch_mode == "headless":
                        probe = probe_page_via_headless(args.url, work_dir, proxy, google_doh=args.google_doh)
                    else:
                        probe = probe_page_via_proxy(args.url, work_dir, fetcher)
                except ProxyFetchError as exc:
                    if cached_proxy(args, work_dir):
                        log(f"Cached proxy failed during discovery: {exc}")
                        proxy = scan_working_proxy(args, subscription, work_dir)

                        def fetcher(url: str, label: str, timeout: int = 90) -> str:
                            return fetch_text_via_proxy(
                                url,
                                work_dir,
                                label,
                                proxy,
                                google_doh=args.google_doh,
                                referer=args.url,
                                timeout=timeout,
                            )

                        fetcher_kind = "proxy"
                        if args.fetch_mode == "headless":
                            probe = probe_page_via_headless(args.url, work_dir, proxy, google_doh=args.google_doh)
                        else:
                            probe = probe_page_via_proxy(args.url, work_dir, fetcher)
                    else:
                        raise
            else:
                open_and_activate(args.url, no_open=args.no_open, page_wait=args.page_wait)
                probe = probe_page(work_dir, args.probe_retries)

                def fetcher(url: str, label: str, timeout: int = 90) -> str:
                    return fetch_text_via_chrome(url, work_dir, label, timeout=timeout)

                fetcher_kind = "chrome"

        if probe is None:
            probe = {
                "url": args.url,
                "title": "",
                "ready": "asset-id-cache",
                "scripts": [],
                "links": [],
                "videos": [],
                "sources": [],
                "performance": [],
                "globals": [],
                "assetIds": [asset_id] if asset_id else [],
                "manifestUrls": [],
                "m3u8Urls": [],
            }
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "media_probe.json").write_text(json.dumps(probe, indent=2))

        asset_ids = extract_asset_ids(probe)
        if not asset_id:
            asset_id = choose_asset_id(probe, args.url, asset_ids, args.asset_id)
        url_bound_asset_ids = extract_url_bound_asset_ids(probe, args.url)
        if asset_id:
            if args.asset_id:
                log(f"Using overridden assetId: {asset_id}")
            elif cached_asset_id:
                log(f"Using cached assetId for requested URL: {asset_id}")
            elif url_bound_asset_ids:
                log(f"Matched assetId to requested URL: {asset_id}")
            else:
                log(f"Found assetId: {asset_id}")
        else:
            log("No assetId found; falling back to probed HLS URLs")

        candidates: list[str] = []
        if asset_id:
            try:
                manifest, _manifest_url = fetch_embed_manifest(asset_id, work_dir, fetcher, fetcher_kind)
                candidates.extend(hls_urls_from_manifest(manifest))
            except FetchError as exc:
                log(f"Embed manifest fetch failed; falling back to page resources: {exc}")
                if fetcher_kind == "direct" and args.fetch_mode in {"headless", "proxy"}:
                    try:
                        log("Trying embed manifest through proxy fallback")
                        subscription = ensure_subscription(args)
                        fetcher, _proxy = build_proxy_fetcher(args, subscription, work_dir)
                        fetcher_kind = "proxy"
                        manifest, _manifest_url = fetch_embed_manifest(
                            asset_id,
                            work_dir,
                            fetcher,
                            fetcher_kind,
                            try_direct_first=False,
                        )
                        candidates.extend(hls_urls_from_manifest(manifest))
                    except (FetchError, SystemExit) as proxy_exc:
                        log(f"Proxy embed manifest fallback failed; falling back to page resources: {proxy_exc}")
        candidates.extend(extract_hls_urls(probe))
        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            raise SystemExit("No HLS candidates found from Bloomberg page or embed manifest.")

        log(f"Evaluating {len(candidates)} HLS candidate(s)")
        variants = discover_variants(candidates, work_dir, fetcher)
        selected = select_best_variant(variants)
        log(f"Selected HLS variant: {variant_summary(selected)}")
        log(selected.url)
        write_plan(work_dir, args.url, asset_id, candidates, selected, output)

        if args.dry_run:
            log(f"Dry run complete; planned output: {rel(output)}")
            success = True
            return 0

        if subscription is None:
            if args.download_backend == "custom":
                subscription = ensure_subscription(args)
            else:
                subscription = resolve_path(args.subscription)
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
