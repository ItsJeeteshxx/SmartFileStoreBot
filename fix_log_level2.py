with open('AryaPremium/plugins/userbot/market_seller.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('print(f"DEBUG_START_PRINT:', 'logger.error(f"DEBUG_START_PRINT:')

with open('AryaPremium/plugins/userbot/market_seller.py', 'w', encoding='utf-8') as f:
    f.write(content)
