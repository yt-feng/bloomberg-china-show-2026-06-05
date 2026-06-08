# Bloomberg China Show Download

One-command local workflow for downloading Bloomberg video pages that are reachable in Chrome through the browser proxy plugin.

Daily use:

```bash
python3 tools/download_bloomberg_video.py \
  --url 'https://www.bloomberg.com/news/videos/2026-06-08/the-china-show-6-8-2026-video'
```

The script opens Chrome, probes the page, fetches Bloomberg's media manifest, selects the best non-ad HLS variant, downloads segments concurrently, remuxes to MP4, verifies the output with `ffprobe`, and cleans the temporary work directory.

One-time local proxy setup:

```bash
mkdir -p tmp
printf '%s\n' '<proxy subscription URL>' > tmp/proxy_subscription_url.txt
chmod 600 tmp/proxy_subscription_url.txt
```

`tmp/` and `downloads/*.mp4` are ignored by git. Do not commit proxy subscriptions, decoded proxy nodes, segment caches, or downloaded video files.

Useful switches:

- `--dry-run`: discover and select the HLS URL without downloading.
- `--keep-tmp`: keep probe JSON, manifests, playlists, and segment work files.
- `--force`: replace an existing output file.
- `--workers 16`: control concurrent segment downloads.

Known downloaded outputs:

- `downloads/the_china_show_2026_06_05_1080p.mp4`
- `downloads/the_china_show_2026_06_08_1080p.mp4`

The workflow only downloads public HLS media URLs exposed by the Bloomberg page in Chrome. It does not bypass DRM, paywall checks, or encrypted streams.
