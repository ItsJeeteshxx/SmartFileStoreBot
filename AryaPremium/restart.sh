#!/bin/bash
echo "Killing all old Bot, API, and Screen sessions..."
pkill -9 -f main.py
pkill -9 -f uvicorn
pkill -9 screen
screen -wipe

echo "Pulling latest updates..."
# Assuming script is run from AryaPremium
git pull --recurse-submodules origin main
cd ..
git pull origin main

echo "Starting Delivery Bot in background..."
screen -dmS delivery_bot bash -c 'cd ~/bot/TryAryaForwardBot && python3 main.py'

echo "Starting Premium Ecosystem Bot in background..."
screen -dmS arya_bot bash -c 'cd ~/bot/TryAryaForwardBot/AryaPremium && source venv/bin/activate && python3 main.py'

echo "Starting API in background..."
screen -dmS arya_api bash -c 'cd ~/bot/TryAryaForwardBot/AryaPremium && source venv/bin/activate && python3 -m uvicorn mini_app_api:app --host 0.0.0.0 --port 8000 --workers 1'

echo "Ecosystem successfully started! Type 'screen -ls' to see the running bots."
