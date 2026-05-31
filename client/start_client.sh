#!/bin/bash
set -e

# Marmot Agent Client - start script
# Records on right-option/alt hotkey, sends to local Marmot server

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🐹 Marmot Agent client setup..."

if [ ! -d "venv" ]; then
    echo "Creating Python venv..."
    python3 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip -q
pip install -r code/requirements.txt

echo "✅ Ready."
cd code
python3 client.py "$@"
