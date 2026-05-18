#!/bin/bash
echo "Stopping old processes..."
pkill -9 -f main.py
pkill -9 -f uvicorn

echo "Pulling latest updates..."
cd ~/bot/TryAryaForwardBot/AryaPremium
git pull --recurse-submodules origin main

echo "Starting Bot in background..."
screen -dmS arya_bot bash -c 'cd ~/bot/TryAryaForwardBot/AryaPremium && source venv/bin/activate && python3 main.py'

echo "Starting API in background..."
screen -dmS arya_api bash -c 'cd ~/bot/TryAryaForwardBot/AryaPremium && source venv/bin/activate && python3 -m uvicorn mini_app_api:app --host 0.0.0.0 --port 8000 --workers 1'

echo "Ecosystem successfully started!"
