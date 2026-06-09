# Bloomberg China Show Download

One-command workflow for downloading Bloomberg video pages through the direct-first background path, without using the foreground Chrome browser by default. The proxy subscription is only a fallback.

Install Python dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Daily use:

```bash
python3 tools/download_bloomberg_video.py \
  --url 'https://www.bloomberg.com/news/videos/2026-06-09/the-china-show-6-9-26-video'
```

The script resolves the Bloomberg asset ID, fetches Bloomberg's media manifest directly, selects the best non-ad HLS variant, downloads it with `yt-dlp` when available, falls back to the proxy-backed downloader only if needed, remuxes to MP4, verifies the output with `ffprobe`, and cleans the temporary work directory.

It also accepts direct HLS and Haystack mirror URLs:

```bash
python3 tools/download_bloomberg_video.py \
  --url 'https://d2ufudlfb4rsg4.cloudfront.net/bloomberg/EzhiMr9I/adaptive/EzhiMr9I_master.m3u8'

python3 tools/download_bloomberg_video.py \
  --url 'https://www.haystack.tv/v/ray-dalio-ai-bubble-burst-wealth-converts-money'
```

Default discovery is non-invasive: it reuses cached URL-to-asset mappings when available, otherwise uses Bloomberg's BRP background endpoint, then fetches the embed manifest and CDN playlist directly. The foreground Chrome browser is used only when explicitly requested with `--fetch-mode chrome`.

One-time local proxy setup:

```bash
mkdir -p tmp
printf '%s\n' '<proxy subscription URL>' > tmp/proxy_subscription_url.txt
chmod 600 tmp/proxy_subscription_url.txt
```

`tmp/` and `downloads/*.mp4` are ignored by git. Do not commit proxy subscriptions, decoded proxy nodes, segment caches, or downloaded video files.

Useful switches:

- `--dry-run`: discover and select the HLS URL without downloading.
- `--fetch-mode chrome`: manually fall back to the older visible-Chrome probe path.
- `--keep-tmp`: keep probe JSON, manifests, playlists, and segment work files.
- `--force`: replace an existing output file.
- `--workers 32`: control concurrent segment downloads.
- `--download-backend auto|yt-dlp|custom`: default `auto` tries `yt-dlp` first and falls back to the built-in downloader.
- `--yt-dlp-proxy-mode auto|never|always`: default `auto` tries direct CDN download first, then uses the cached proxy through a local credential-hiding forwarder if direct download fails.

Known downloaded outputs:

- `downloads/the_china_show_2026_06_05_1080p.mp4`
- `downloads/the_china_show_2026_06_04_1080p.mp4`
- `downloads/the_china_show_2026_06_08_1080p.mp4`
- `downloads/the_china_show_6_9_26_2026_06_09_1080p.mp4`
- `downloads/dalio_ai_bubble_to_burst_as_wealth_converts_to_money_2026_06_03.mp4`

The workflow only downloads public HLS media URLs exposed by Bloomberg's own media manifests. It does not bypass DRM, paywall checks, or encrypted streams.
