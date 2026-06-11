#!/usr/bin/env python3
"""Resolve the Bloomberg China Show URL for a Beijing-date run."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def default_show_date() -> str:
    now_bj = datetime.now(ZoneInfo("Asia/Shanghai"))
    return (now_bj.date() - timedelta(days=1)).isoformat()


def build_url(show_date: str) -> str:
    year_s, month_s, day_s = show_date.split("-")
    year = int(year_s)
    month = int(month_s)
    day = int(day_s)
    return (
        f"https://www.bloomberg.com/news/videos/{show_date}/"
        f"the-china-show-{month}-{day}-{year}-video"
    )


def append_env(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for key, value in values.items():
            fh.write(f"{key}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show-date", default="", help="YYYY-MM-DD. Defaults to yesterday in Asia/Shanghai.")
    parser.add_argument("--url", default="", help="Override Bloomberg video URL.")
    parser.add_argument("--github-env", type=Path, default=None)
    parser.add_argument("--metadata", type=Path, default=None)
    args = parser.parse_args()

    show_date = args.show_date.strip() or default_show_date()
    url = args.url.strip() or build_url(show_date)
    output_dir = f"rendered-clips/{show_date}"

    values = {
        "SHOW_DATE": show_date,
        "SHOW_URL": url,
        "OUTPUT_DIR": output_dir,
    }

    print(f"SHOW_DATE={show_date}", flush=True)
    print(f"SHOW_URL={url}", flush=True)
    print(f"OUTPUT_DIR={output_dir}", flush=True)

    if args.github_env:
        append_env(args.github_env, values)
    elif os.environ.get("GITHUB_ENV"):
        append_env(Path(os.environ["GITHUB_ENV"]), values)

    if args.metadata:
        args.metadata.parent.mkdir(parents=True, exist_ok=True)
        args.metadata.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
