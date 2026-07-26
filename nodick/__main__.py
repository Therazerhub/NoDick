"""NoDick — CLI entry point"""

from __future__ import annotations

import argparse
import asyncio

from nodick.db import create_import_job, init_db
from nodick.services.importer import TelegramImporter
from nodick.services.session import login
from nodick.telegram.app import run


def main():
    parser = argparse.ArgumentParser(prog="nodick", description="NoDick Telegram stash bot")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("run", help="run the Telegram bot")
    sub.add_parser("init-db", help="initialize / migrate the database")
    sub.add_parser("session-login", help="login Telegram user session for history imports")

    p_import = sub.add_parser("import", help="index videos from a Telegram channel")
    p_import.add_argument("channel_id", help="channel ID or username")
    p_import.add_argument("--limit", type=int, default=None, help="max messages to scan")

    args = parser.parse_args()

    if args.cmd in (None, "run"):
        run()
    elif args.cmd == "init-db":
        init_db()
        print("✅ NoDick database initialized")
    elif args.cmd == "session-login":
        asyncio.run(login())
    elif args.cmd == "import":
        init_db()
        async def go():
            job_id = create_import_job(args.channel_id, 0)
            importer = TelegramImporter()
            try:
                async def progress(stats):
                    print(
                        f"job={job_id} "
                        f"status={stats['status']} "
                        f"checked={stats['total_checked']} "
                        f"videos={stats['videos_found']} "
                        f"saved={stats['saved']} "
                        f"skipped={stats['skipped']}"
                    )
                await importer.import_channel(
                    args.channel_id, job_id=job_id, progress=progress, limit=args.limit
                )
            finally:
                await importer.disconnect()
        asyncio.run(go())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
