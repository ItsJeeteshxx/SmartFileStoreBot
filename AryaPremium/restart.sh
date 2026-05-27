#!/bin/bash
echo "Killing all old Bot, API, and Screen sessions..."
pkill -9 -f main.py
pkill -9 -f uvicorn
pkill -9 screen
screen -wipe
echo "Cleaning up corrupted session files..."
rm -f ~/bot/TryAryaForwardBot/main_bot_session.session*
rm -f ~/bot/TryAryaForwardBot/AryaPremium/mgmt_bot.session*

# Auto-repair SQLite database schema issues in all session files
python3 -c "
import glob, sqlite3, os
path = os.path.expanduser('~/bot/TryAryaForwardBot/**/*.session')
for f in glob.glob(path, recursive=True):
    try:
        conn = sqlite3.connect(f)
        conn.cursor().execute('DROP TABLE IF EXISTS update_state;')
        conn.commit()
        conn.close()
        print('Repaired session state for:', f)
    except Exception as e:
        print('Error repairing session:', f, e)
"

echo "Pulling latest updates..."
# Assuming script is run from AryaPremium
git pull --recurse-submodules origin main
cd ..
git pull origin main

echo "Starting Premium Ecosystem and Delivery Bot in background..."
screen -dmS arya_bot bash -c 'cd ~/bot/TryAryaForwardBot && python3 main.py & cd ~/bot/TryAryaForwardBot/AryaPremium && source venv/bin/activate && python3 main.py'

echo "Starting API in background..."
screen -dmS arya_api bash -c 'cd ~/bot/TryAryaForwardBot && python3 -m uvicorn mini_app_api:app --host 0.0.0.0 --port 8000 --workers 1'

echo "Ecosystem successfully started! Type 'screen -ls' to see the running bots."
