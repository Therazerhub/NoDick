"""Backfill video_metadata + videos.tags from StashDB for all library videos.

This is the metadata cache foundation — it makes "More Like This", Cast /
performer filmography, tag search and rich stats work without re-querying
StashDB on every view.

Usage:
    python -m nodick.backfill_metadata [--limit N] [--in-order] [--dry-run]

Local SQLite by default. For Render PG:
    DATABASE_URL='<external pg url>' python -m nodick.backfill_metadata

Notes:
- Idempotent + resumable: skips videos that already have a metadata row.
- --in-order processes by id; default is random order (better demo variety).
- Progress prints with \r — pipe through `tr '\\r' '\\n'` to read logs.
- Run as a background process (400+ videos ≈ 15-30 min); reruns finish the rest.
"""

import argparse
import sys
import time

from nodick.db import (
    get_video_metadata,
    init_db,
    pending_metadata_videos,
    save_enrichment,
)
from nodick.metadata.stash import process_video_caption_with_metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill StashDB metadata cache")
    parser.add_argument("--limit", type=int, default=100, help="max videos to process")
    parser.add_argument("--in-order", action="store_true", help="process by id (default: random)")
    parser.add_argument("--dry-run", action="store_true", help="show what would run, do nothing")
    args = parser.parse_args()

    init_db()
    videos = pending_metadata_videos(args.limit, random_order=not args.in_order)
    print(f"[backfill_metadata] {len(videos)} videos pending metadata (limit={args.limit})")

    if args.dry_run:
        for v in videos[:10]:
            print(f"  would enrich [{v['id']}] {str(v['title'])[:70]}")
        return

    ok = fail = skipped = 0
    t0 = time.time()
    for i, v in enumerate(videos, 1):
        # Resumable: skip anything another run already cached
        if get_video_metadata(v["id"]):
            skipped += 1
            continue
        try:
            _caption, source, meta = process_video_caption_with_metadata(v["title"])
            if save_enrichment(v["id"], source, meta):
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            print(f"  ERR [{v['id']}] {e}", file=sys.stderr)

        if i % 25 == 0 or i == len(videos):
            elapsed = time.time() - t0
            print(
                f"\r[{i}/{len(videos)}] ok={ok} fail={fail} skipped={skipped} "
                f"{elapsed:.0f}s ({elapsed / max(i, 1):.1f}s/video)",
                flush=True,
            )

    print(
        f"\nDone: {ok} cached, {fail} no-match/failed, {skipped} skipped "
        f"in {time.time() - t0:.0f}s"
    )


if __name__ == "__main__":
    sys.exit(main())
