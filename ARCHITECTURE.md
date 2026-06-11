# Architecture

This repository preserves the repeatable, direct-first, headless/background code path used to download Bloomberg videos. The normal path does not use the user's foreground Chrome browser and does not require the proxy subscription; proxy and Chrome paths remain explicit fallbacks for hostile network conditions or page-shape changes.

## Scope

The workflow downloads public HLS media URLs exposed by Bloomberg's own media manifests. It does not bypass DRM, paywall checks, or encrypted streams. If a future page exposes only DRM-protected media, this workflow should stop rather than attempt circumvention.

Large media outputs and temporary proxy material are intentionally excluded from git.

## Target Operating Mode

The target operator experience is:

```bash
python3 tools/download_bloomberg_video.py --url '<Bloomberg video URL>'
```

That single command should resolve the page, choose the best first-party Bloomberg HLS stream, download the MP4, verify it, and exit. The operator should not need to approve browser automation, proxy scans, manual manifest probing, or repeated shell commands during the normal path.

Default behavior is tuned around the fastest verified 2026-06-09 path:

1. Run fully in the background.
2. Reuse a cached URL-to-asset mapping if available.
3. Otherwise fetch the Bloomberg page metadata from the BRP background endpoint.
4. Fetch Bloomberg's `media-manifest/embed` JSON directly.
5. Expand the LOOP/HD master playlist to a first-party Bloomberg CDN media playlist.
6. Prefer `FHD5000.m3u8` from Bloomberg delivery hosts such as Fastly or Akamai.
7. Download with `yt-dlp` using 32 concurrent fragments.
8. Try direct CDN download before any proxy path.
9. Verify the output with `ffprobe`.

The workflow should interrupt the operator only when the URL cannot be classified, the selected stream is encrypted/DRM-protected, required runtime dependencies are missing, or the environment lacks network access to both Bloomberg and the configured fallback proxy.

## Components

- `tools/download_bloomberg_video.py`
  - The daily entry point.
  - Accepts a Bloomberg video page URL and orchestrates the full flow.
  - Reuses cached URL-to-asset mappings when available, tries Bloomberg's BRP background endpoint for page metadata, fetches Bloomberg's `embed` manifest directly first, selects the best HLS variant, prefers `yt-dlp` for HLS download/remux, falls back to proxy-backed download paths only when needed, verifies the MP4, and cleans temporary work files.
  - Uses `tmp/proxy_sub.raw` and `tmp/working_proxy.url` so repeated runs do not need subscription input or full proxy rescans.
  - Uses `tmp/download_strategy.json` as a local strategy cache. When the local network repeatedly times out on direct Bloomberg/`yt-dlp` paths, the one-command workflow can prefer the last successful route, currently pure proxy-mode `BRP via proxy` discovery plus the built-in segmented downloader.
  - Defaults to non-invasive discovery. It only uses the visible Chrome/AppleScript path when `--fetch-mode chrome` is explicitly supplied.
  - Defaults to 32 HLS fragment workers, matching the fastest verified local run.
  - For `yt-dlp`, reads `YTDLP_BIN`, `PATH`, common local install paths, or `python -m yt_dlp`. The repo records this dependency in `requirements.txt`.
  - Cleans failed `yt-dlp` partial files before falling back to the segmented downloader, so a near-complete failed run does not poison the next attempt.
  - Fetches small text resources such as embed manifests and HLS playlists through bounded `curl --max-time` requests before falling back to `urllib`, so a stalled terminal connection fails clearly instead of hanging the one-command flow.
  - If a cached or overridden `assetId` is available but direct embed-manifest fetch fails, automatically tries the same embed manifest through the cached/scanned proxy before falling back to page resource candidates.
  - Also accepts direct `.m3u8` URLs and Haystack mirror pages. Those paths skip Bloomberg page discovery and go straight to HLS selection/download.

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
  - The fallback downloader when `yt-dlp` is missing or fails.
  - Reads a local proxy subscription file.
  - Decodes Ghelper-style base64 subscription entries.
  - Normalizes curl-compatible `https` and `socks5` proxy nodes.
  - Uses Google DNS-over-HTTPS or Chrome DNS-over-HTTPS results to add curl `resolve` mappings for proxy node hostnames.
  - Tests proxies with an HTTP endpoint.
  - Reuses a cached working proxy when one is provided.
  - Downloads HLS segments concurrently.
  - Writes each segment to a temporary file, then atomically replaces the final segment path only after curl succeeds.
  - Marks completed segment files with `.ok` sidecars while still accepting older non-empty cached segments.
  - Stores segment caches under a playlist-URL hash, so an interrupted wrong-source run cannot be reused for a different HLS playlist.
  - Retries only missing or failed segments for multiple rounds before writing `segment_failures.json`.
  - Reports segment progress by actual completion order, so one slow early segment does not hide the rest of the active downloads.
  - Refuses encrypted HLS playlists containing `#EXT-X-KEY`.
  - Remuxes into a temporary MP4 first, then atomically replaces the final output after success.
  - Tries strict `ffmpeg -c copy` remux first, then retries with corrupt-packet discard for isolated bad AAC packets.

## Preferred Data Flow

The preferred path is the one-command orchestrator:

```bash
python3 tools/download_bloomberg_video.py \
  --url 'https://www.bloomberg.com/news/videos/2026-06-09/the-china-show-6-9-26-video'
```

Internally, the script performs these steps:

1. Derive a stable output name from the input URL, for example `downloads/the_china_show_2026_06_08_1080p.mp4`.
2. If that MP4 already exists and `--force` is not set, verify it with `ffprobe` and exit without re-downloading.
3. If the input URL is already `.m3u8`, skip page discovery and evaluate that HLS playlist directly.
4. If the input URL is a Haystack video page, fetch the static HTML directly, extract the current page's Bloomberg Haystack media ID, derive the CloudFront HLS master URL, and evaluate that HLS playlist. The parser deliberately restricts itself to the page's Bloomberg video ID so recommended videos in the page payload cannot win variant selection.
5. For Bloomberg pages, reuse a cached `assetId` for the exact Bloomberg URL when one exists in `tmp/auto_*/media_probe.json`. This lets historical playlist items download without opening any browser.
6. If no cached mapping exists, attempt non-invasive discovery first through `https://brp-prod-bcc.bloomberg.com/...`. This backend route can return the same Next.js/RSC metadata while avoiding the normal `www.bloomberg.com` robot page from terminal/headless clients.
7. If the local strategy cache says the current machine should use `proxy-brp`, switch default `headless` mode to pure `proxy` mode and try BRP through the cached/scanned proxy before spending time on direct BRP timeouts or Chrome startup. Otherwise, if BRP discovery fails or is transiently unavailable, fall back to BRP-through-proxy, then the background proxy and isolated-headless paths. Visible Chrome is reserved for explicit `--fetch-mode chrome` fallback.
8. When Bloomberg page discovery is required, prefer a playlist item `assetID` whose `url` matches the requested Bloomberg path. This matters for older episode pages because Bloomberg can render a current/recommended video in `currentVideo` while the requested historical episode appears in `playlistItems`.
9. Fetch Bloomberg's own media metadata directly first, using a bounded manifest timeout. In the default headless/direct mode, a failed direct embed fetch is not repeated through the same direct fetcher. If an `assetId` is known and direct manifest fetch fails, the orchestrator tries the proxy fetcher before falling back to page resource candidates. Proxy and Chrome fetchers still get their normal second chance when explicitly selected.

```text
https://www.bloomberg.com/media-manifest/embed?id=<assetId>&variant=LOOP&streamType=HD
```

10. From that JSON, prefer `streams[].url` pointing to Bloomberg `media-manifest/videos/LOOP/HD/...m3u8`.
11. Fetch the LOOP/HD master manifest directly first and expand it to the final CDN media playlist. Bloomberg has used both Fastly and Akamai hosts; both `bbgvod-...fastly.net` and `bbgvod-...akamaized.net` are treated as first-party Bloomberg delivery.
12. Select the highest useful Bloomberg delivery variant, typically `FHD5000.m3u8`, while deprioritizing `pubads.g.doubleclick.net` DAI playlists.
13. Try `yt-dlp` first when available. The orchestrator passes the already-selected final CDN HLS variant, `--concurrent-fragments <workers>`, retry settings, Bloomberg Referer, browser User-Agent, and an MP4 output template. The default worker count is 32. In default `--yt-dlp-proxy-mode auto`, it tries direct CDN download first because the final HLS segments are often globally reachable. If `yt-dlp` exits non-zero or does not create the expected MP4, the orchestrator removes matching `.part` and `.ytdl` residues before trying the next backend.
14. If direct `yt-dlp` download fails and a cached working proxy is available with `http` or `https`, the orchestrator starts a local `127.0.0.1` proxy forwarder and passes that local URL to `yt-dlp`. This avoids exposing upstream proxy credentials in the `yt-dlp` process arguments.
15. If the local strategy cache says the current machine should use `custom`, skip `yt-dlp` in default `auto` mode and go straight to `proxy_hls_downloader.py`. Otherwise, if `yt-dlp` is unavailable or exits non-zero in `--download-backend auto` mode, fall back to `proxy_hls_downloader.py` with `--google-doh`, the selected HLS variant, and the original Bloomberg URL as the HTTP Referer.
16. The fallback downloader:
   - decodes proxy nodes,
   - resolves proxy node DNS via DoH,
   - writes curl-only `resolve` mappings in temporary config files,
   - tries the cached working proxy first when available,
   - tests the proxy path,
   - downloads all `.ts` segments with a worker pool,
   - records successful segments with `.ok` markers,
   - retries missing or failed segments for `--segment-rounds` rounds,
   - writes a local `.m3u8`,
   - remuxes to MP4 with a strict pass, then a corrupt-packet-tolerant pass if needed.
17. Verify the MP4 with `ffprobe`.
18. Delete the per-video work directory unless `--keep-tmp` is set. The final MP4, cached subscription, and cached working proxy remain local and ignored by git.

## Approval And Automation Model

The user-facing goal is one approval for one top-level command when Codex runs the workflow:

```bash
python3 tools/download_bloomberg_video.py --url '<Bloomberg video URL>'
```

That command owns cached asset lookup, BRP discovery, Bloomberg manifest fetches, direct `yt-dlp` downloads, optional proxy fallback, ffmpeg remux, ffprobe verification, and temporary cleanup. In Codex, approving the command prefix `python3 tools/download_bloomberg_video.py` is the practical way to avoid separate confirmations for every subprocess. After that prefix is approved, normal Bloomberg URL requests should be handled by running the orchestrator directly instead of asking for separate approval for `curl`, browser probing, `yt-dlp`, `ffmpeg`, or proxy test internals.

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
- `tmp/download_strategy.json` for the ignored local route cache; it contains route names only, not proxy credentials

## GitHub Actions Deployment Shape

The current resolver/downloader path is suitable for a GitHub Actions `workflow_dispatch` job that accepts one Bloomberg URL input. The job does not need a desktop browser on the fast path.

Recommended job shape:

1. Check out this private repo.
2. Install Python 3.10+ dependencies with `python3 -m pip install -r requirements.txt`.
3. Install `ffmpeg` so both `ffmpeg` and `ffprobe` are available.
4. Optionally expose `BLOOMBERG_PROXY_SUBSCRIPTION_URL` from a GitHub secret for fallback only.
5. Run:

```bash
python3 tools/download_bloomberg_video.py --url "$BLOOMBERG_URL"
```

6. Upload `downloads/*.mp4` to the chosen storage target.

GitHub Actions-specific notes:

- The primary path is BRP metadata discovery plus direct Bloomberg CDN download through `yt-dlp`; it should not require Chrome, AppleScript, local macOS state, or the user's browser profile.
- Keep `--yt-dlp-proxy-mode auto` so the job tries direct CDN first and uses the proxy only after a direct CDN failure.
- Keep proxy subscription material in GitHub Secrets. Do not write decoded proxy nodes to logs.
- Large Bloomberg episodes are commonly 3-4 GiB. Artifact upload can be the simplest proof of concept, but durable automation should allow an object-storage or release-asset upload step if artifact storage is too constrained.
- A future scheduled "latest episode" workflow should resolve the newest Bloomberg China Show page URL first, then call the same one-URL command. That resolver should remain separate from the downloader so manual URL downloads stay predictable.

## yt-dlp Test Results And Cloud Shape

`yt-dlp` is the better default HLS downloader/remuxer, but it is not a complete replacement for Bloomberg page discovery in this environment.

Tested on 2026-06-09 against:

```text
https://www.bloomberg.com/news/videos/2026-06-03/dalio-ai-bubble-to-burst-as-wealth-converts-to-money-video
```

Observed behavior:

- Direct `yt-dlp` against the Bloomberg page timed out without proxy.
- Direct `yt-dlp` against the Bloomberg page through the cached proxy reached Bloomberg but got HTTP 403.
- `yt-dlp` does not support the Haystack mirror page URL as an extractor.
- Haystack's static HTML exposed the real HLS master:

```text
https://d2ufudlfb4rsg4.cloudfront.net/bloomberg/EzhiMr9I/adaptive/EzhiMr9I_master.m3u8
```

- `yt-dlp` downloaded that direct HLS URL successfully and produced a verified MP4.
- The integrated Haystack path selected:

```text
https://d2ufudlfb4rsg4.cloudfront.net/bloomberg/EzhiMr9I/adaptive/hls_manifests/EzhiMr9I_hd720.m3u8
```

- Verification output from the integrated path:
  - Downloader: `yt-dlp`, direct CDN, no proxy needed
  - Fragments: 89
  - Duration: 266.60 seconds
  - Size: 31.1 MiB
  - Video: H.264, 1280x720
  - Audio: AAC

Recommended cloud deployment shape:

- Keep this repo's Bloomberg/Haystack discovery code as the page-to-HLS resolver.
- Use `yt-dlp` as the first HLS downloader with `--concurrent-fragments`, retries, and MP4 remux. Prefer direct CDN download first, then proxy fallback.
- Keep `proxy_hls_downloader.py` as a fallback for environments where `yt-dlp` cannot use the available proxy or where per-segment curl diagnostics are needed.
- Use Python 3.10+ for the host image. The local Python 3.9 run works, but current `yt-dlp` warns that Python 3.9 support is deprecated.
- Install `ffmpeg` and `ffprobe` on the host image. Install Python dependencies with `python3 -m pip install -r requirements.txt`.
- Store proxy subscription material in environment variables or ignored runtime files, never in git.

## Fallback Data Flow

If Bloomberg's `embed` manifest does not expose a usable `streams[].url`, inspect `performance` resources from `chrome_media_probe.applescript`.

Treat `pubads.g.doubleclick.net/ondemand/hls/.../master.m3u8` as a fallback source only. It can be valid HLS, but it is a DAI path and may include ad segments, lower initial variants, or extra discontinuities. Prefer Bloomberg delivery playlists such as `bbgvod-...fastly.net/...FHD5000.m3u8` or `bbgvod-...akamaized.net/...FHD5000.m3u8` whenever available.

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
https://www.bloomberg.com/news/videos/2026-06-10/the-china-show-6-10-2026-video
```

Asset ID:

```text
bb052e15-6e0f-414e-9b25-12f4a46c3a20
```

Selected 1080p HLS variant:

```text
https://bbgvod-s3-us-east1-zenko.global.ssl.fastly.net/vod/m/MTAyNTM1MTA/Q2xvdWRfMTQxODMwNQ/Thechinashow61026/Thechinashow61026FHD5000.m3u8
```

Final output:

```text
downloads/the_china_show_2026_06_10_1080p.mp4
```

Download notes:

- Direct BRP discovery timed out locally.
- Pure proxy-mode BRP discovery found the matching asset ID.
- Direct and proxied `yt-dlp` timed out at the HLS manifest fetch step.
- The built-in segmented downloader completed 621 segments with 32 workers and produced the verified MP4.
- The local strategy cache now prefers proxy-mode BRP discovery plus the built-in segmented downloader for Bloomberg pages on this machine.

Verified properties:

- Video: H.264, 1920x1080, 29.97 fps
- Audio: AAC
- Duration: 6139.33 seconds
- Size: 3,915,689,156 bytes, about 3.6 GiB
- Bit rate: 5,102,429 bps

Target page:

```text
https://www.bloomberg.com/news/videos/2026-05-27/the-china-show-5-27-2026-video
```

Asset ID:

```text
d75c72e1-0001-49ce-8ee9-4ce59c629e52
```

Selected 1080p HLS variant:

```text
https://bbgvod-s3-us-east1-zenko.global.ssl.fastly.net/vod/m/MTAyMzk2Nzk/Q2xvdWRfMTQwNzQzMw/TCS05272026v2/TCS05272026v2FHD5000.m3u8
```

Final output:

```text
downloads/the_china_show_2026_05_27_1080p.mp4
```

Download notes:

- `yt-dlp` direct Fastly download reached the tail of the file and then failed with connection errors and skipped fragments.
- The built-in fallback downloader completed all 559 segments after retrying failures.
- Strict remux hit an isolated corrupt AAC packet; the corrupt-packet-tolerant remux path produced the final MP4.

Verified properties:

- Video: H.264, 1920x1080
- Audio: AAC
- Duration: 5513.31 seconds
- Size: 3,515,773,239 bytes, about 3.3 GiB
- Bit rate: 5,101,508 bps

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

Target page:

```text
https://www.bloomberg.com/news/videos/2026-06-09/the-china-show-6-9-26-video
```

Asset ID:

```text
b13d3699-89f2-401b-bf43-4fd85f10f9dc
```

Embed manifest:

```text
https://www.bloomberg.com/media-manifest/embed?id=b13d3699-89f2-401b-bf43-4fd85f10f9dc&variant=LOOP&streamType=HD
```

Selected 1080p HLS variant:

```text
https://bbgvod-s3-us-east1-zenko.akamaized.net/vod/m/MTAyNTI0MDQ/Q2xvdWRfMTQxNzU1Nw/TheChinashow6926/TheChinashow6926FHD5000.m3u8
```

Final output:

```text
downloads/the_china_show_6_9_26_2026_06_09_1080p.mp4
```

Download properties:

- Downloader: `yt-dlp`, direct Akamai CDN, no proxy needed
- Workers: 32 concurrent fragments
- Fragments: 568
- Total download time: 4 minutes 53 seconds
- Average transfer rate: 11.94 MiB/s

Verified properties:

- Video: H.264, 1920x1080, 29.97 fps
- Audio: AAC
- Duration: 5599.29 seconds
- Size: 3,571,427,767 bytes, about 3.4 GiB
- Bit rate: 5,102,683 bps

## Operational Commands

Daily download:

```bash
python3 tools/download_bloomberg_video.py \
  --url 'https://www.bloomberg.com/news/videos/2026-06-09/the-china-show-6-9-26-video'
```

Discovery-only dry run:

```bash
python3 tools/download_bloomberg_video.py \
  --url 'https://www.bloomberg.com/news/videos/2026-06-09/the-china-show-6-9-26-video' \
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
  --url 'https://www.bloomberg.com/news/videos/2026-06-09/the-china-show-6-9-26-video' \
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
  'https://www.bloomberg.com/media-manifest/embed?id=b13d3699-89f2-401b-bf43-4fd85f10f9dc&variant=LOOP&streamType=HD' \
  > tmp/master_fetch_2026_06_09_loop_hd.json
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
- `.gitignore` excludes `tmp/`, `downloads/*.mp4`, `.DS_Store`, `yt-dlp` partials, remux temp files, and logs.
- Do not paste or commit decoded subscription contents, proxy credentials, HLS segment caches, or full media files.
- Commit only helper scripts, documentation, and non-sensitive configuration.

## Failure Modes

- `curl` cannot resolve proxy hostnames:
  - Use `--google-doh` first. It resolves proxy hostnames through DNS-over-HTTPS and injects curl `resolve` entries without using Chrome.
  - Use `--chrome-doh` only as a manual legacy fallback.

- Terminal cannot reach Bloomberg or its CDN:
  - Prefer cached URL-to-asset mappings plus Bloomberg `media-manifest/embed`.
  - Use `--fetch-mode chrome` only when page discovery is required and the background paths cannot classify the URL.
  - Use the normalized proxy path for Bloomberg CDN HLS segment downloads only after direct CDN download fails.
  - Small manifest and playlist fetches use bounded `curl --max-time` requests, so this failure should surface as a `FetchError` rather than an indefinitely quiet command.
  - If direct embed-manifest fetch fails while an `assetId` is already known, the orchestrator tries the proxy manifest path automatically.

- `yt-dlp` fails near the tail of a large HLS download:
  - The orchestrator removes matching `.part` and `.ytdl` files before trying the next backend.
  - In `--download-backend auto`, it falls back to the built-in segmented downloader instead of asking for manual cleanup.
  - Keep `--yt-dlp-proxy-mode auto`; direct CDN is still the fastest path when it succeeds, and proxy mode remains a fallback.

- Historical Bloomberg page resolves to the wrong episode:
  - Do not trust the first `currentVideo.assetId`.
  - Prefer a playlist item `assetID` whose `url` exactly matches the requested Bloomberg path.
  - Segment caches are keyed by selected playlist URL, so a stopped wrong-source run cannot be remuxed as another episode.

- HLS playlist has `#EXT-X-KEY`:
  - Stop and inspect. Do not attempt DRM or protected-content bypass.

- Some segments fail:
  - Each segment downloads to a temp file and is moved into place only after curl succeeds.
  - The fallback downloader retries only missing or failed segments for `--segment-rounds` rounds. The default is 3.
  - If all rounds fail, the downloader writes `segment_failures.json` under the work directory.
  - Re-running the same command reuses already downloaded non-empty segment files and adds `.ok` markers for older caches.

- Remux fails on an isolated corrupt AAC packet:
  - The fallback downloader first tries strict stream copy remux.
  - If strict remux fails, it retries with `-fflags +genpts+discardcorrupt -err_detect ignore_err`.
  - Both attempts write to `<output>.remuxing.mp4` and replace the final MP4 only after success.

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

## Speaker Clip Rendering Pipeline

This repo also supports extracting specific speaker segments from Bloomberg episodes and rendering them as vertical short videos with bilingual subtitles, styled identically to the local `vid_cut` pipeline.

### Pipeline Overview

```text
Download video (download_bloomberg_video.py)
    ↓
Transcribe speaker segment only (faster-whisper, base model, CPU)
    ↓
Plan highlights with DeepSeek (plan_speaker_highlights.py)
    ↓
Generate overlay PNGs (render_overlays_pillow.py)
    ↓
Composite with ffmpeg (render_clips_linux.py)
    ↓
Upload final clips as artifact
```

### GitHub Actions Workflow

Workflow: `.github/workflows/cut-speaker.yml` ("Cut Speaker Clips (full render)")

Inputs:
- `url`: Bloomberg video page URL
- `speaker`: Speaker name (e.g. "Wang Yi")
- `speaker_context`: Role/affiliation (e.g. "Goldman Sachs, head of China real estate research")
- `segment_start` / `segment_end`: Known time range (seconds) where the speaker appears
- `min_clip_seconds` / `max_clip_seconds`: Target clip duration bounds (default 20-90s)

Secrets required:
- `DEEPSEEK_API_KEY`: For highlight planning and subtitle translation
- `BLOOMBERG_PROXY_SUBSCRIPTION_URL` (optional): Fallback for video download
- `HF_TOKEN` (optional): Higher HuggingFace rate limit for Whisper model download

### Rendering Components

- `tools/render_overlays_pillow.py`
  - Linux replacement for the macOS Swift renderer (`render_text_overlays.swift` in vid_cut).
  - Reads the same `overlay_batch.json` format: a list of jobs with kind `static` or `subtitle`.
  - Produces transparent PNG overlays using Pillow.
  - Requires `fonts-noto-cjk` on Ubuntu for CJK text rendering.
  - Static overlay (1080×1920): 3-line title with yellow keyword highlights, "KC桌面" watermark, CTA text.
  - Subtitle overlays (1080×430): Chinese text + English text with per-phrase yellow highlights.
  - Text shadow: black with gaussian blur, matching the Swift renderer's visual output.

- `tools/plan_speaker_highlights.py`
  - Generates `highlight_plan.json` in the exact format expected by the renderer.
  - Uses DeepSeek to split the speaker segment into clips and produce bilingual subtitles.
  - Output format per clip: `start`, `end`, `title`, `title_lines[3]`, `title_highlights`, `subtitles[]`.
  - Each subtitle: `start`, `end`, `relative_start`, `relative_end`, `zh`, `en`, `zh_highlights`, `en_highlights`.
  - Applies sensitive-word filtering (投资 → **, 股票 → 权益资产, etc.) in the prompt.

- `tools/render_clips_linux.py`
  - Port of `vid_cut/render_highlight_clips.py` for Linux.
  - Calls `render_overlays_pillow.py` to generate PNGs, then composites with ffmpeg.
  - Uses `libx264` encoder (no VideoToolbox dependency).
  - Same `filter_complex` structure as vid_cut:
    - Source split → blurred/darkened background (1080×1920) + cropped main panel
    - Main panel at y=690, black subtitle panel at y=1110
    - Static overlay (title/watermark/CTA) composited full-frame
    - Subtitle PNGs composited at y=1125 with `enable=between(t,start,end)`
    - Audio: highpass/lowpass, compression, EQ, fade in/out
  - No throttling (SIGSTOP/SIGCONT) since Actions runners have no thermal concern.

- `tools/extract_speaker_clips.py`
  - Full-auto mode: transcribes entire video, uses DeepSeek to find speaker segments, cuts raw clips.
  - Useful for discovery when segment timestamps are not yet known.
  - Name variant matching: handles ASR name order differences (Wang Yi ↔ Yi Wang).

- `tools/cut_speaker_segment.py`
  - Lightweight mode: takes known segment timestamps, transcribes only that segment, cuts clips.
  - Does not produce styled overlays (raw cuts only).

### Visual Output Specification

Final clips match the vid_cut竖版 format:

- Resolution: 1080×1920 (9:16 vertical)
- Background: source video cropped/scaled/darkened with noise
- Main video: cropped (no Bloomberg top/bottom chrome), positioned at y=690
- Title: 3-line bold white text with yellow highlights, positioned at y=250-676
- Subtitle panel: black semi-transparent bar from y=1110 to bottom
- Chinese subtitle: bold, up to 52px, yellow keyword highlights
- English subtitle: bold, up to 52px, 94% alpha
- Watermark: "KC桌面" at y=1668
- CTA: "更多宏观信息，关注公众号KC桌面" at y=1586
- Audio: fade in 3s, fade out 5s, light compression/EQ

### Whisper Model Caching

The workflow caches the Whisper model at `~/.cache/huggingface/hub/models--Systran--faster-whisper-base` using `actions/cache@v4`. First run downloads ~150MB; subsequent runs restore from cache instantly. The script also retries on HuggingFace 429 rate limits with exponential backoff.
