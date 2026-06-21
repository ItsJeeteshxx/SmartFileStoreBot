with open('AryaPremium/plugins/userbot/market_seller.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('logger.warning(f"DEBUG_START:', 'print(f"DEBUG_START_PRINT:', 1)
content = content.replace('logger.warning(f"DEBUG_START:', 'print(f"DEBUG_START_PRINT:', 1)

with open('AryaPremium/plugins/userbot/market_seller.py', 'w', encoding='utf-8') as f:
    f.write(content)
