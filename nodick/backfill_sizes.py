"""Backfill file_size for videos missing it (channel_ref imports).

Most library entries came from the Telethon scanner which stored
``channel_ref:channel:message`` references WITHOUT capturing the file size.
This script batch-fetches the source messages (100 ids per MTProto call),
reads the document size, and fills it into the DB — no forwarding, no copying.

Usage:
    python -m nodick.backfill_sizes                  # backfill everything
    python -m nodick.backfill_sizes --limit 500      # first 500 missing only
    python -m nodick.backfill_sizes --channel -1001234567890
    python -m nodick.backfill_sizes --dry-run        # count what would be fixed

Runs against SQLite by default (local DB). To backfill PostgreSQL (Render),
set DATABASE_URL to the external URL:
    DATABASE_URL='postgresql://...' python -m nodick.backfill_sizes

Uses the same Telethon bot-token client as /import_scan (nodick_scan_bot.session).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from nodick.db import (
    _fetchall,
    _fetchone,
    _using_pg,
    bulk_update_video_sizes,
)
from nodick.services.message_importer import _batch_fetch, _get_telethon_client


async def backfill(
    limit: int = 0,
    channel: str = "",
    dry_run: bool = False,
) -> dict:
    ph = "%s" if _using_pg else "?"
    sql = "SELECT id, file_id FROM videos WHERE file_size IS NULL OR file_size <= 0"
    params: list = []
    if channel:
        sql += f" AND file_id LIKE {ph}"
        params.append(f"channel_ref:{channel}:%")
    if limit:
        sql += f" LIMIT {ph}"
        params.append(limit)

    rows = _fetchall(sql, tuple(params))

    # Group channel_ref videos by source channel
    by_channel: dict[int, list[tuple[int, int]]] = {}
    for row in rows:
        ref = row["file_id"]
        if not ref.startswith("channel_ref:"):
            continue
        try:
            _, ch, mid = ref.split(":", 2)
        except ValueError:
            continue
        by_channel.setdefault(int(ch), []).append((row["id"], int(mid)))

    total = sum(len(v) for v in by_channel.values())
    print(f"📦 Missing sizes: {len(rows)} candidates, {total} channel_ref to backfill "
          f"across {len(by_channel)} channel(s).")

    if not total:
        return {"total": 0, "fixed": 0, "missing": 0, "dry_run": dry_run}

    client = await _get_telethon_client()
    fixed = 0
    missing = 0
    processed = 0
    start = time.monotonic()

    for ch, items in by_channel.items():
        for i in range(0, len(items), 100):
            batch = items[i:i + 100]
            msgs = await _batch_fetch(client, ch, [m for _, m in batch])
            id_to_msg = {getattr(m, "id", 0): m for m in (msgs or [])}
            to_update: list[tuple[int, int]] = []
            for vid, mid in batch:
                msg = id_to_msg.get(mid)
                doc = getattr(msg, "document", None) if msg else None
                doc = doc or (getattr(msg, "video", None) if msg else None)
                size = getattr(doc, "size", None) if doc else None
                if size:
                    fixed += 1
                    to_update.append((vid, int(size)))
                else:
                    missing += 1
            if to_update and not dry_run:
                bulk_update_video_sizes(to_update)
            processed += len(batch)

            elapsed = time.monotonic() - start
            rate = processed / elapsed if elapsed > 0 else 0
            eta = (total - processed) / rate if rate > 0 else 0
            print(f"\r  ⏳ {processed}/{total} | ✅ fixed: {fixed} | ❓ missing: {missing}"
                  f" | ETA {eta:.0f}s", end="", flush=True)
            await asyncio.sleep(0.05)

    print()
    return {"total": total, "fixed": fixed, "missing": missing, "dry_run": dry_run}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill file_size for NoDick videos")
    parser.add_argument("--limit", type=int, default=0,
                        help="only process the first N missing videos")
    parser.add_argument("--channel", type=str, default="",
                        help="only process one source channel (e.g. -1001234567890)")
    parser.add_argument("--dry-run", action="store_true",
                        help="count what would be fixed without writing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    stats = asyncio.run(backfill(args.limit, args.channel, args.dry_run))

    action = "would fix" if args.dry_run else "fixed"
    print(f"✅ Done: {stats['fixed']} sizes {action}, "
          f"{stats['missing']} still unknown (deleted/no video).")

    if not args.dry_run:
        row = _fetchone(
            "SELECT COUNT(*) AS cnt FROM videos WHERE file_size IS NULL OR file_size <= 0"
        )
        remaining = row["cnt"] if _using_pg else row[0]
        print(f"   📉 Remaining missing: {remaining}")


if __name__ == "__main__":
    main()
