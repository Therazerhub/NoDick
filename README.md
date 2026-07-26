# 🖤 NoDick

Your fucking stash index. Cleaner than your browser history.

Telegram stash bot with StashDB enrichment, silent Telethon scanning, and a personality.

---

## Features

- **🎲 /random** — surprise video with enriched caption
- **🔍 /search** — find anything with pagination
- **📁 /categories** — browse by studio/source
- **⭐ /favorites** — save your keepers
- **🎭 /performer** — StashDB performer lookup
- **📏 /threshold 80** — set match confidence (80%+)
- **🌐 StashDB enrichment** — auto-matches titles, performers, tags, studio
- **📥 Silent import** — batch-scans via Telethon bot token, zero forwards
- **📡 Auto-detect channel** — forward a video, tap Import from here

## Quick Start

```
git clone https://github.com/Therazerhub/NoDick.git
cd NoDick
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in BOT_TOKEN, ADMIN_ID, API_ID, API_HASH
python -m nodick init-db
python -m nodick run
```

### Silent Import (no user session)

Forward the last video from your channel to the bot → tap **Import from here**.

Or use `/import_scan <channel_id> <start_id>`:
```
/import_scan -1001234567890 10542
```

Uses Telethon with **just your bot token** — no user session, no forwarded messages flashing in your chat. Batch-fetches 100 IDs at a time like VJ-FILTER-BOT.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Main menu |
| `/random` | Surprise video |
| `/search <q>` | Search stash |
| `/categories` | Browse by studio |
| `/favorites` | Your saved |
| `/stats` | Collection stats |
| `/performer <name>` | StashDB performer search |
| `/threshold <0-100>` | Match confidence filter |
| `/settings` | Admin panel |
| `/import <channel>` | Telethon channel import |
| `/import_scan <channel> <id>` | Silent scan (no forwards) |
| `/import_status` | Check import progress |

## Threshold

```
/threshold 80   → only show matches with ≥80% confidence
/threshold 0    → show everything (default)
/threshold 100  → perfectionist mode
```

At 80, only strong StashDB matches get the 🌐 treatment. Weak ones fall back to 📁 local parsing.

## StashDB Setup

1. Get your API key from [stashdb.org](https://stashdb.org)
2. Set it in `.env`: `STASHDB_API_KEY=your_jwt_token_here`
3. Restart bot

Without a key, everything falls back to local filename parsing — still works, just less juicy.

## Project Structure

```
nodick/
├── __main__.py              # CLI entry point
├── config.py                # Settings from .env
├── db.py                    # Database schema + CRUD
├── utils.py                 # Title cleaning, category extraction
├── telegram/
│   ├── app.py               # All 35 handlers merged
│   └── keyboards.py         # Inline keyboard layouts
├── metadata/
│   ├── stash.py             # StashDB/FansDB integration
│   ├── matching.py          # Fuzzy + phonetic matching
│   ├── rename.py            # Auto-rename logic
│   └── performer_db.py      # Local performer DB
└── services/
    ├── importer.py          # Telethon channel import
    ├── message_importer.py  # Silent Telethon batch scanner
    └── session.py           # Session login CLI
```

## Tech

- **python-telegram-bot v21** — Bot API framework
- **Telethon** — MTProto client for silent history scanning
- **RapidFuzz** — fuzzy string matching
- **Jellyfish** — phonetic matching (soundex, metaphone)
- **Requests** — GraphQL queries to StashDB/FansDB

---

Built by Razer.
