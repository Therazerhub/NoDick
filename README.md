<![CDATA[<p align="center">
  <img src="https://img.shields.io/badge/NoDick-v1.0-black?style=for-the-badge&logo=python"/>
  <img src="https://img.shields.io/badge/Telegram-Bot-2CA5E0?style=for-the-badge&logo=telegram"/>
  <img src="https://img.shields.io/badge/StashDB-Enabled-8A2BE2?style=for-the-badge"/>
</p>

<h1 align="center">🖤 NoDick</h1>
<p align="center"><i>Your fucking stash index. Cleaner than your browser history.</i></p>

<p align="center">
  <b>Telegram stash bot</b> — with StashDB enrichment, silent Telethon scanning, and a personality.
  <br>
  Built from the ashes of <a href="https://github.com/Therazerhub/moye-bot">moye-bot</a> and <a href="https://github.com/Therazerhub/BlackSite">BlackSite</a>.
  <br><br>
  <sub>Because your video collection deserves better than a spaghetti-coded bot.</sub>
</p>

---

## ✨ Features

| What | How |
|------|-----|
| 🎲 **Random** — surprise video with enriched caption | `/random` |
| 🔍 **Search** — find anything with pagination | `/search <keyword>` |
| 📁 **Categories** — browse by studio/source | `/categories` |
| ⭐ **Favorites** — save your keepers | tap 💦 on any video |
| 🎭 **Performer lookup** — StashDB performer search | `/performer <name>` |
| 📏 **Threshold** — set match confidence (80%+) | `/threshold 80` |
| 🌐 **StashDB enrichment** — auto-matches titles, performers, tags, studio | requires API key |
| 📥 **Silent import** — Telethon bot-token scanner, zero forwards | `/import_scan <channel> <id>` |
| 📡 **Auto-detect channel** — forward a video, tap Import | forward → 📥 Import from here |
| ⚙️ **Settings** — toggle action buttons, etc | `/settings` |
| 📊 **Stats** — flex your collection size | `/stats` |

## 🚀 Quick Start

```bash
# Clone
git clone https://github.com/Therazerhub/NoDick.git
cd NoDick

# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Fill in BOT_TOKEN, ADMIN_ID, API_ID, API_HASH
# Optionally set STASHDB_API_KEY for metadata enrichment

# Initialize DB
python -m nodick init-db

# Run
python -m nodick run
```

### 📥 Silent Import (no user session)

```bash
# No session login needed for import_scan —
# just uses Telethon with your bot token
# Forward the last video from your channel to the bot
# Tap "Import from here"
# Or use:
#   /import_scan -1001234567890 10542
```

### 📥 Channel Import (Telethon user session)

```bash
# Only needed for full channel history import via /import
python -m nodick session-login
python -m nodick import -1001234567890
```

## 📏 Threshold System

```
/threshold 80   → only show matches with ≥80% confidence
/threshold 0    → show everything (default)
/threshold 100  → perfectionist mode
```

Threshold controls how confident StashDB has to be before it uses the match. At 80%, only strong matches get the 🌐 treatment — weak ones fall back to 📁 local parsing.

## 🔧 Commands

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

## 🏗️ Project Structure

```
NoDick/
├── nodick/
│   ├── __main__.py          # CLI — run, import, session-login
│   ├── config.py             # Settings from .env
│   ├── db.py                 # Database schema + CRUD
│   ├── utils.py              # Title cleaning, category extraction
│   ├── telegram/
│   │   ├── app.py            # All 35 handlers merged
│   │   └── keyboards.py      # Inline keyboard layouts
│   ├── metadata/
│   │   ├── stash.py          # StashDB/FansDB integration
│   │   ├── matching.py       # Fuzzy + phonetic matching
│   │   ├── rename.py         # Auto-rename logic
│   │   └── performer_db.py   # Local performer DB
│   └── services/
│       ├── importer.py       # Telethon channel import
│       ├── message_importer.py  # Silent Telethon batch scanner
│       └── session.py        # Session login CLI
├── .env.example
├── requirements.txt
└── deploy.sh
```

## 🔑 StashDB Setup

1. Get your API key from [stashdb.org](https://stashdb.org)
2. Set it in `.env`:
   ```env
   STASHDB_API_KEY=your_jwt_token_here
   ```
3. Restart the bot
4. Use `/threshold 80` for clean matches

Without a key, everything falls back to 📁 local filename parsing (still works, just less juicy).

## 🧠 The Tech

- **python-telegram-bot v21** — Bot API framework
- **Telethon** — MTProto client for silent history scanning (bot token, no user session)
- **RapidFuzz** — fuzzy string matching for title/performer matching
- **Jellyfish** — phonetic matching (soundex, metaphone)
- **Requests** — GraphQL queries to StashDB/FansDB

The import scanner works like VJ-FILTER-BOT: batch-fetches message IDs via MTProto with just a bot token. No forwarding, no flashing, no user session needed.

---

<p align="center">
  <sub>Built with 🖤 by <a href="https://github.com/Therazerhub">Razer</a></sub>
  <br>
  <sub><i>"Sweet when needed, savage when deserved"</i></sub>
</p>
]]>