<p align="center">
  <img src="https://img.shields.io/badge/NoDick-v1.0-black?style=for-the-badge&logo=python"/>
  <img src="https://img.shields.io/badge/Telegram-Bot-2CA5E0?style=for-the-badge&logo=telegram"/>
  <img src="https://img.shields.io/badge/StashDB-Enabled-8A2BE2?style=for-the-badge"/>
  <img src="https://img.shields.io/badge/License-MIT-red?style=for-the-badge"/>
</p>

<h1 align="center">🖤 NoDick</h1>
<h3 align="center"><i>Your fucking stash index. Cleaner than your browser history.</i></h3>

<p align="center">
  Telegram stash bot with <b>StashDB enrichment</b>, <b>silent Telethon scanning</b>, <br>
  and more personality than your average bot.
</p>

---

## 🎮 Commands

| Command | Description |
|---------|-------------|
| `/start` | Main menu |
| `/random` | 🎲 Surprise video with enriched caption |
| `/search <q>` | 🔍 Search stash |
| `/categories` | 📁 Browse by studio |
| `/favorites` | ⭐ Your saved |
| `/stats` | 📊 Collection stats |
| `/performer <name>` | 🎭 StashDB performer lookup |
| `/threshold <0-100>` | 📏 Match confidence |
| `/settings` | ⚙️ Admin panel |
| `/import <channel>` | 📥 Channel import |
| `/import_scan <channel> <id>` | 📡 Silent scan (no forwards) |
| `/import_status` | 📊 Import progress |

## ✨ Features

**📡 Silent Import** — Forward a video from your channel, tap "Import from here". No messages flash in your chat. Uses Telethon with just your bot token — batch-fetches 100 IDs at a time like VJ-FILTER-BOT.

**🌐 StashDB Enrichment** — Auto-matches titles, performers, studio, and tags. Set confidence threshold with `/threshold 80` so only strong matches get through.

**📏 Threshold System**
```
/threshold 80   → only strong matches (≥80% confidence)
/threshold 0    → show everything (default)
/threshold 100  → perfectionist mode
```

**🎭 Performer Search** — Query StashDB's performer database directly from Telegram.

**📥 Dual Import Modes**
- `/import_scan` — Telethon bot-token mode, silent, no user session needed
- `/import` — Full channel history import via Telethon user session (requires session-login)

## 🚀 Quick Start

```bash
git clone https://github.com/Therazerhub/NoDick.git
cd NoDick
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in: BOT_TOKEN, ADMIN_ID, TELEGRAM_API_ID, TELEGRAM_API_HASH
python -m nodick init-db
python -m nodick run
```

### Import Without User Session
```
/import_scan -1001234567890 10542
```
Or forward the last video from your channel and tap 📥 Import from here.

### Import With Telethon Session
```bash
python -m nodick session-login  # one-time auth
# Then use: /import -1001234567890
```

## 📁 Structure

```
nodick/
├── __main__.py              # CLI: run, import, session-login
├── config.py                # Settings from .env
├── db.py                    # Schema + CRUD
├── utils.py                 # Title cleaning, categories
├── telegram/
│   ├── app.py               # 35 handlers — all merged
│   └── keyboards.py         # Inline keyboards
├── metadata/
│   ├── stash.py             # StashDB/FansDB integration
│   ├── matching.py          # Fuzzy + phonetic matching
│   ├── rename.py            # Auto-rename
│   └── performer_db.py      # Local performer cache
└── services/
    ├── importer.py          # Telethon channel import
    ├── message_importer.py  # Silent batch scanner
    └── session.py           # Session login
```

## 🔧 Tech Stack

| Tool | Purpose |
|------|---------|
| **python-telegram-bot v21** | Bot API framework |
| **Telethon** | MTProto client for silent scanning |
| **RapidFuzz** | Fuzzy title matching |
| **Jellyfish** | Phonetic performer matching |
| **Requests** | GraphQL → StashDB/FansDB |

## 🔑 StashDB Setup

1. Get an API key from [stashdb.org](https://stashdb.org)
2. Add to `.env`: `STASHDB_API_KEY=your_jwt_here`
3. Restart and run `/threshold 80`

Without a key, everything falls back to local filename parsing — still works, just less juicy.

---

<p align="center">
  <sub>Built by <a href="https://github.com/Therazerhub">Razer</a></sub>
  <br>
  <sub><i>"Sweet when needed, savage when deserved."</i></sub>
</p>
