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
  - Reports segment progress by actual completion order, so one slow early segment does not hide the rest of the active downloads.
  - Remuxes the local HLS playlist into MP4 with `ffmpeg -c copy`.

## Preferred Data Flow

1. Open the Bloomberg video page in Chrome with the proxy plugin enabled.
2. Use `activate_bloomberg_tab.applescript` to focus the target tab.
3. Use `chrome_media_probe.applescript` to extract `currentVideo.assetId` or equivalent media IDs from the page payload.
4. Fetch Bloomberg's own media metadata first:

```text
https://www.bloomberg.com/media-manifest/embed?id=<assetId>&variant=LOOP&streamType=HD
```

5. From that JSON, prefer `streams[].url` pointing to Bloomberg `media-manifest/videos/LOOP/HD/...m3u8`.
6. Fetch the LOOP/HD master manifest with `chrome_fetch_text.applescript` if terminal networking cannot reach `www.bloomberg.com`.
7. Select the highest useful Bloomberg/Fastly HLS variant, typically `FHD5000.m3u8`.
8. Fetch the proxy subscription into `tmp/`.
9. Run `proxy_hls_downloader.py` with `--chrome-doh`.
10. The downloader:
   - decodes proxy nodes,
   - asks Chrome to resolve proxy node DNS via DoH,
   - writes curl-only `resolve` mappings in temporary config files,
   - tests the proxy path,
   - downloads all `.ts` segments with a worker pool,
   - writes a local `.m3u8`,
   - remuxes to MP4.

## Fallback Data Flow

If Bloomberg's `embed` manifest does not expose a usable `streams[].url`, inspect `performance` resources from `chrome_media_probe.applescript`.

Treat `pubads.g.doubleclick.net/ondemand/hls/.../master.m3u8` as a fallback source only. It can be valid HLS, but it is a DAI path and may include ad segments, lower initial variants, or extra discontinuities. Prefer Bloomberg/Fastly `bbgvod-.../Thechinashow...FHD5000.m3u8` whenever available.

## Why The 2026-06-08 Run Was Slower

The 2026-06-05 run was straightforward because the page quickly exposed the Bloomberg LOOP/HD HLS URL in the browser resource list:

```text
https://www.bloomberg.com/media-manifest/videos/LOOP/HD/b5f7dc2a-2355-4c06-93a9-bc85306af160.m3u8?idType=BMMP
```

The 2026-06-08 page initially exposed a `pubads.g.doubleclick.net/ondemand/hls/...` DAI playlist in `performance` resources. That playlist was downloadable, but the first obvious media playlist was only `SD600`, and the 1080p DAI path could include ad-related segments. The correct optimization was to ignore that as the primary source, fetch Bloomberg's `media-manifest/embed` JSON by asset ID, and then use the direct Bloomberg LOOP/HD stream URL from `streams[]`.

The download itself also looked more stalled than it was because the downloader consumed futures with `pool.map`, which returns results in input order. One slow early segment could block progress printing even while later segments were already downloaded. This is now fixed by consuming futures with `as_completed`.

## Known Runs

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

Target page:

```text
https://www.bloomberg.com/news/videos/2026-06-08/the-china-show-6-8-2026-video
```

Asset ID:

```text
479ef363-55bb-4fa0-a489-b58aed39ceb7
```

Selected master manifest:

```text
https://www.bloomberg.com/media-manifest/videos/LOOP/HD/479ef363-55bb-4fa0-a489-b58aed39ceb7.m3u8?idType=BMMP
```

Selected 1080p HLS variant:

```text
https://bbgvod-s3-us-east1-zenko.global.ssl.fastly.net/vod/m/MTAyNTEyODM/Q2xvdWRfMTQxNjczMA/Thechinashow6826/Thechinashow6826FHD5000.m3u8
```

Final output:

```text
downloads/the_china_show_2026_06_08_1080p.mp4
```

Verified properties:

- Video: H.264, 1920x1080, 29.97 fps
- Audio: AAC
- Duration: 5682.04 seconds
- Size: about 3.4 GB

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

Fetch the preferred metadata manifest by asset ID:

```bash
osascript tools/chrome_fetch_text.applescript \
  'https://www.bloomberg.com/media-manifest/embed?id=479ef363-55bb-4fa0-a489-b58aed39ceb7&variant=LOOP&streamType=HD' \
  > tmp/master_fetch_2026_06_08_loop_hd.json
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

Download and remux the 2026-06-08 LOOP/HD source:

```bash
python3 tools/proxy_hls_downloader.py \
  --subscription tmp/proxy_sub.raw \
  --playlist-url 'https://bbgvod-s3-us-east1-zenko.global.ssl.fastly.net/vod/m/MTAyNTEyODM/Q2xvdWRfMTQxNjczMA/Thechinashow6826/Thechinashow6826FHD5000.m3u8' \
  --work-dir tmp/hls_work_2026_06_08_bbg \
  --output downloads/the_china_show_2026_06_08_1080p.mp4 \
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

- Progress output appears idle:
  - Older downloader revisions printed results in playlist order, so a slow early segment hid later completed segments.
  - Current downloader revisions report by completion order. If output is still quiet, inspect `tmp/<work-dir>/segments` for file growth.

- Page exposes a `pubads.g.doubleclick.net/ondemand/hls` URL first:
  - Do not assume it is the best source.
  - Use the asset ID to fetch Bloomberg `media-manifest/embed` and prefer direct LOOP/HD `bbgvod` sources.

## Extension Points

- Add support for more proxy subscription formats in `normalize_proxy`.
- Add automatic asset-ID extraction and LOOP/HD master-manifest selection.
- Add proxy speed benchmarking instead of using the first working proxy.
- Add automatic master-manifest parsing so the script can select the highest variant directly.
- Add subtitle playlist download and sidecar `.vtt` export if needed.
