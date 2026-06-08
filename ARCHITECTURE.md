# Architecture

This repository preserves the repeatable code path used to download Bloomberg videos through a local proxy subscription. The current default path avoids using the user's foreground Chrome browser; the older Chrome/AppleScript path remains as an explicit fallback.

## Scope

The workflow downloads public HLS media URLs exposed by Bloomberg's own media manifests. It does not bypass DRM, paywall checks, or encrypted streams. If a future page exposes only DRM-protected media, this workflow should stop rather than attempt circumvention.

Large media outputs and temporary proxy material are intentionally excluded from git.

## Components

- `tools/download_bloomberg_video.py`
  - The daily entry point.
  - Accepts a Bloomberg video page URL and orchestrates the full flow.
  - Reuses cached URL-to-asset mappings when available, fetches Bloomberg's `embed` manifest through the proxy, selects the best HLS variant, refreshes or reuses the proxy subscription, calls the segmented downloader, verifies the MP4, and cleans temporary work files.
  - Uses `tmp/proxy_sub.raw` and `tmp/working_proxy.url` so repeated runs do not need subscription input or full proxy rescans.
  - Defaults to non-invasive discovery. It only uses the visible Chrome/AppleScript path when `--fetch-mode chrome` is explicitly supplied.

- `tools/activate_bloomberg_tab.applescript`
  - Finds and activates an already-open Chrome tab by URL substring.
  - Used before page probing so AppleScript reads the intended Bloomberg tab.

- `tools/chrome_media_probe.applescript`
  - Runs JavaScript in the active Chrome tab.
  - Extracts page title, video element metadata, performance resource URLs, media manifest clues, direct `.m3u8` URLs, and candidate `assetId` values.
  - Primary output is used by the one-command script to locate Bloomberg `media-manifest` and `.m3u8` URLs without manual page inspection.

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
  - Uses Google DNS-over-HTTPS or Chrome DNS-over-HTTPS results to add curl `resolve` mappings for proxy node hostnames.
  - Tests proxies with an HTTP endpoint.
  - Reuses a cached working proxy when one is provided.
  - Downloads HLS segments concurrently.
  - Stores segment caches under a playlist-URL hash, so an interrupted wrong-source run cannot be reused for a different HLS playlist.
  - Reports segment progress by actual completion order, so one slow early segment does not hide the rest of the active downloads.
  - Refuses encrypted HLS playlists containing `#EXT-X-KEY`.
  - Remuxes the local HLS playlist into MP4 with `ffmpeg -c copy`.

## Preferred Data Flow

The preferred path is the one-command orchestrator:

```bash
python3 tools/download_bloomberg_video.py \
  --url 'https://www.bloomberg.com/news/videos/2026-06-08/the-china-show-6-8-2026-video'
```

Internally, the script performs these steps:

1. Derive a stable output name from the Bloomberg URL, for example `downloads/the_china_show_2026_06_08_1080p.mp4`.
2. If that MP4 already exists and `--force` is not set, verify it with `ffprobe` and exit without re-downloading.
3. Reuse a cached `assetId` for the exact Bloomberg URL when one exists in `tmp/auto_*/media_probe.json`. This lets historical playlist items download without opening any browser.
4. If no cached mapping exists, attempt non-invasive discovery first. The script has background proxy and isolated-headless code paths; visible Chrome is reserved for explicit `--fetch-mode chrome` fallback.
5. When page discovery is required, prefer a playlist item `assetID` whose `url` matches the requested Bloomberg path. This matters for older episode pages because Bloomberg can render a current/recommended video in `currentVideo` while the requested historical episode appears in `playlistItems`.
6. Fetch Bloomberg's own media metadata first:

```text
https://www.bloomberg.com/media-manifest/embed?id=<assetId>&variant=LOOP&streamType=HD
```

7. From that JSON, prefer `streams[].url` pointing to Bloomberg `media-manifest/videos/LOOP/HD/...m3u8`.
8. Fetch the LOOP/HD master manifest through the cached proxy. The current default passes `--google-doh` so proxy-node DNS does not require Chrome.
9. Select the highest useful Bloomberg/Fastly HLS variant, typically `FHD5000.m3u8`, while deprioritizing `pubads.g.doubleclick.net` DAI playlists.
10. Reuse `tmp/proxy_sub.raw` if present, otherwise refresh it from `--subscription-url`, `BLOOMBERG_PROXY_SUBSCRIPTION_URL`, or `tmp/proxy_subscription_url.txt`.
11. Prime the run with `tmp/working_proxy.url` if available, so the downloader first tries the previously working proxy instead of scanning the full subscription.
12. Run `proxy_hls_downloader.py` with `--google-doh`, the selected HLS variant, and the original Bloomberg URL as the HTTP Referer.
13. The downloader:
   - decodes proxy nodes,
   - resolves proxy node DNS via DoH,
   - writes curl-only `resolve` mappings in temporary config files,
   - tries the cached working proxy first when available,
   - tests the proxy path,
   - downloads all `.ts` segments with a worker pool,
   - writes a local `.m3u8`,
   - remuxes to MP4.
14. Verify the MP4 with `ffprobe`.
15. Delete the per-video work directory unless `--keep-tmp` is set. The final MP4, cached subscription, and cached working proxy remain local and ignored by git.

## Approval And Automation Model

The user-facing goal is one approval for one top-level command when Codex runs the workflow:

```bash
python3 tools/download_bloomberg_video.py --url '<Bloomberg video URL>'
```

That command owns cached asset lookup, background proxy fetches, Bloomberg manifest fetches, proxy subscription refresh, segmented curl downloads, ffmpeg remux, ffprobe verification, and temporary cleanup. In Codex, approving the command prefix `python3 tools/download_bloomberg_video.py` is the practical way to avoid separate confirmations for every subprocess.

The visible Chrome path is not part of default operation anymore. Use it only as an explicit fallback:

```bash
python3 tools/download_bloomberg_video.py \
  --url '<Bloomberg video URL>' \
  --fetch-mode chrome
```

The proxy subscription URL should not be committed. For normal local use, store it in one of these ignored/local locations:

- `tmp/proxy_subscription_url.txt`
- `BLOOMBERG_PROXY_SUBSCRIPTION_URL`
- `tmp/proxy_sub.raw` if the subscription has already been fetched

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
https://www.bloomberg.com/news/videos/2026-06-04/the-china-show-6-4-2026-video
```

Asset ID:

```text
60917b5e-c2fc-4063-8e1e-7021324686ea
```

Selected 1080p HLS variant:

```text
https://bbgvod-s3-us-east1-zenko.global.ssl.fastly.net/vod/m/MTAyNDgyNDA/Q2xvdWRfMTQxNDEzMw/1870f065-3f58-431f-a613-b8e675c5155d/1870f065-3f58-431f-a613-b8e675c5155dFHD5000.m3u8
```

Final output:

```text
downloads/the_china_show_2026_06_04_1080p.mp4
```

Verified properties:

- Video: H.264, 1920x1080, 29.97 fps
- Audio: AAC
- Duration: 5507.87 seconds
- Size: about 3.3 GB

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

Daily download:

```bash
python3 tools/download_bloomberg_video.py \
  --url 'https://www.bloomberg.com/news/videos/2026-06-08/the-china-show-6-8-2026-video'
```

Discovery-only dry run:

```bash
python3 tools/download_bloomberg_video.py \
  --url 'https://www.bloomberg.com/news/videos/2026-06-08/the-china-show-6-8-2026-video' \
  --dry-run \
  --keep-tmp
```

One-time local proxy subscription setup:

```bash
mkdir -p tmp
printf '%s\n' '<proxy subscription URL>' > tmp/proxy_subscription_url.txt
chmod 600 tmp/proxy_subscription_url.txt
```

Manual debug commands remain useful when the orchestrator cannot classify a new Bloomberg page shape.

Explicit visible-Chrome fallback:

```bash
python3 tools/download_bloomberg_video.py \
  --url 'https://www.bloomberg.com/news/videos/2026-06-08/the-china-show-6-8-2026-video' \
  --fetch-mode chrome \
  --dry-run \
  --keep-tmp
```

Activate the page manually:

```bash
osascript tools/activate_bloomberg_tab.applescript 'the-china-show-6-5-2026-video'
```

Probe media URLs manually from the active Chrome tab:

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
  --google-doh
```

Download and remux a known HLS variant manually:

```bash
python3 tools/proxy_hls_downloader.py \
  --subscription tmp/proxy_sub.raw \
  --playlist-url 'https://bbgvod-s3-us-east1-zenko.global.ssl.fastly.net/vod/m/MTAyNDk0MjM/Q2xvdWRfMTQxNTEzNQ/Thechinashow562026/Thechinashow562026FHD5000.m3u8' \
  --work-dir tmp/hls_work \
  --output downloads/the_china_show_2026_06_05_1080p.mp4 \
  --workers 16 \
  --google-doh \
  --referer 'https://www.bloomberg.com/news/videos/2026-06-05/the-china-show-6-5-2026-video'
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
  - Use `--google-doh` first. It resolves proxy hostnames through DNS-over-HTTPS and injects curl `resolve` entries without using Chrome.
  - Use `--chrome-doh` only as a manual legacy fallback.

- Terminal cannot reach Bloomberg/Fastly:
  - Prefer cached URL-to-asset mappings plus Bloomberg `media-manifest/embed`.
  - Use `--fetch-mode chrome` only when page discovery is required and the background paths cannot classify the URL.
  - Use the normalized proxy path for Fastly HLS segment downloads.

- Historical Bloomberg page resolves to the wrong episode:
  - Do not trust the first `currentVideo.assetId`.
  - Prefer a playlist item `assetID` whose `url` exactly matches the requested Bloomberg path.
  - Segment caches are keyed by selected playlist URL, so a stopped wrong-source run cannot be remuxed as another episode.

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
- Add proxy speed benchmarking instead of using the first working proxy.
- Add subtitle playlist download and sidecar `.vtt` export if needed.
- Add a scheduled "latest episode" resolver if the workflow needs to discover the newest Bloomberg China Show URL automatically instead of receiving the URL from the user.
