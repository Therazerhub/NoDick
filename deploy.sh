#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "🖤 NoDick — Deploy"

# Create venv if needed
if [ ! -d .venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

# Install deps
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# Init DB
python -m nodick init-db

echo ""
echo "✅ NoDick ready"
echo "   Run: python -m nodick run"
