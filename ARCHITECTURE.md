# Architecture

This repository preserves the repeatable code path used to download a Bloomberg video whose media page is reachable in Chrome through a browser proxy plugin, while the terminal network is not directly able to reach Bloomberg/Fastly/Google.

## Scope

The workflow downloads public HLS media URLs that the Bloomberg page itself requests in Chrome. It does not bypass DRM, paywall checks, or encrypted streams. If a future page exposes only DRM-protected media, this workflow should stop rather than attempt circumvention.

Large media outputs and temporary proxy material are intentionally excluded from git.

## Components

- `tools/activate_bloomberg_tab.applescript`
  - Finds and activates an already-open Chrome tab by URL substring.
  - Used before page probing so AppleScript reads the intended Bloomberg tab.

- `tools/chrome_media_probe.applescript`
  - Runs JavaScript in the active Chrome tab.
  - Extracts page title, video element metadata, performance resource URLs, and media manifest clues.
  - Primary output is used to locate Bloomberg `media-manifest` and `.m3u8` URLs.

- `tools/chrome_fetch_text.applescript`
  - Fetches same-origin text from the active Bloomberg page context.
  - Used for Bloomberg URLs that the terminal cannot reach directly, such as the first master manifest.

- `tools/chrome_fetch_url.applescript`
  - Fetches arbitrary JSON/text via Chrome's current network path.
  - Used here for DNS-over-HTTPS lookups when system DNS cannot resolve proxy node hostnames.

- `tools/proxy_hls_downloader.py`
  - Reads a local proxy subscription file.
  - Decodes Ghelper-style base64 subscription entries.
  - Normalizes curl-compatible `https` and `socks5` proxy nodes.
  - Uses Chrome DNS-over-HTTPS results to add curl `resolve` mappings for proxy node hostnames.
  - Tests proxies with an HTTP endpoint.
  - Downloads HLS segments concurrently.
  - Remuxes the local HLS playlist into MP4 with `ffmpeg -c copy`.

## Data Flow

1. Open the Bloomberg video page in Chrome with the proxy plugin enabled.
2. Use `activate_bloomberg_tab.applescript` to focus the target tab.
3. Use `chrome_media_probe.applescript` to identify media manifest URLs already requested by the page.
4. Use `chrome_fetch_text.applescript` to fetch the master manifest when terminal networking cannot reach `www.bloomberg.com`.
5. Select the highest useful HLS variant from the master manifest.
6. Fetch the proxy subscription into `tmp/`.
7. Run `proxy_hls_downloader.py` with `--chrome-doh`.
8. The downloader:
   - decodes proxy nodes,
   - asks Chrome to resolve proxy node DNS via DoH,
   - writes curl-only `resolve` mappings in temporary config files,
   - tests the proxy path,
   - downloads all `.ts` segments with a worker pool,
   - writes a local `.m3u8`,
   - remuxes to MP4.

## Current Video Run

Target page:

```text
https://www.bloomberg.com/news/videos/2026-06-05/the-china-show-6-5-2026-video
```

Selected master manifest:

```text
https://www.bloomberg.com/media-manifest/videos/LOOP/HD/b5f7dc2a-2355-4c06-93a9-bc85306af160.m3u8?idType=BMMP
```

Selected 1080p HLS variant:

```text
https://bbgvod-s3-us-east1-zenko.global.ssl.fastly.net/vod/m/MTAyNDk0MjM/Q2xvdWRfMTQxNTEzNQ/Thechinashow562026/Thechinashow562026FHD5000.m3u8
```

Final output:

```text
downloads/the_china_show_2026_06_05_1080p.mp4
```

Verified properties:

- Video: H.264, 1920x1080, 29.97 fps
- Audio: AAC
- Duration: 5528.69 seconds
- Size: about 3.3 GB

## Operational Commands

Activate the page:

```bash
osascript tools/activate_bloomberg_tab.applescript 'the-china-show-6-5-2026-video'
```

Probe media URLs from the active Chrome tab:

```bash
osascript tools/chrome_media_probe.applescript > tmp/media_probe.json
```

Fetch a Bloomberg manifest via Chrome:

```bash
osascript tools/chrome_fetch_text.applescript 'https://www.bloomberg.com/media-manifest/videos/LOOP/HD/b5f7dc2a-2355-4c06-93a9-bc85306af160.m3u8?idType=BMMP' > tmp/master_fetch.json
```

Test a proxy subscription through Google 204:

```bash
python3 tools/proxy_hls_downloader.py \
  --subscription tmp/proxy_sub.raw \
  --playlist-url 'about:blank' \
  --proxy-test-url 'https://www.google.com/generate_204' \
  --work-dir tmp/google_proxy_test \
  --chrome-doh
```

Download and remux:

```bash
python3 tools/proxy_hls_downloader.py \
  --subscription tmp/proxy_sub.raw \
  --playlist-url 'https://bbgvod-s3-us-east1-zenko.global.ssl.fastly.net/vod/m/MTAyNDk0MjM/Q2xvdWRfMTQxNTEzNQ/Thechinashow562026/Thechinashow562026FHD5000.m3u8' \
  --work-dir tmp/hls_work \
  --output downloads/the_china_show_2026_06_05_1080p.mp4 \
  --workers 16 \
  --chrome-doh
```

Verify output:

```bash
ffprobe -v error \
  -show_entries format=duration,size,bit_rate \
  -show_entries stream=index,codec_type,codec_name,width,height,avg_frame_rate \
  -of json downloads/the_china_show_2026_06_05_1080p.mp4
```

## Security And Git Hygiene

- Keep proxy subscription files under `tmp/`.
- Keep downloaded media under `downloads/`.
- `.gitignore` excludes `tmp/`, `downloads/*.mp4`, `.DS_Store`, partial files, and logs.
- Do not paste or commit decoded subscription contents, proxy credentials, HLS segment caches, or full media files.
- Commit only helper scripts, documentation, and non-sensitive configuration.

## Failure Modes

- `curl` cannot resolve proxy hostnames:
  - Use `--chrome-doh`, which asks Chrome to resolve proxy hostnames and injects curl `resolve` entries.

- Terminal cannot reach Bloomberg/Fastly:
  - Use Chrome AppleScript fetches for page-origin manifest discovery.
  - Use the normalized proxy path for Fastly HLS segment downloads.

- HLS playlist has `#EXT-X-KEY`:
  - Stop and inspect. Do not attempt DRM or protected-content bypass.

- Some segments fail:
  - The downloader writes `segment_failures.json` under the work directory.
  - Re-running the same command reuses already downloaded non-empty segment files.

## Extension Points

- Add support for more proxy subscription formats in `normalize_proxy`.
- Add automatic master-manifest parsing so the script can select the highest variant directly.
- Add subtitle playlist download and sidecar `.vtt` export if needed.
