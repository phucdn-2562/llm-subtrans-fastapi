#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

if [ ! -d vendor/llm-subtrans/.git ]; then
  mkdir -p vendor
  git clone https://github.com/machinewrapped/llm-subtrans.git vendor/llm-subtrans
fi

python -m pip install -e "vendor/llm-subtrans[gemini]"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env. Add your GEMINI_API_KEY before starting."
fi

echo
echo "Setup complete."
echo "Edit .env, then run: ./scripts/run_local.sh"
