with open('AryaPremium/plugins/userbot/market_seller.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'story_id = args[1][6:].strip()' in line:
        insert_idx = i + 1
        break

lines.insert(insert_idx, '        import logging\n        logger = logging.getLogger(__name__)\n        logger.info(f"DEBUG_START: extracted story_id: \'{story_id}\' from args: {args}")\n')

for i, line in enumerate(lines):
    if 'story = await db.db.premium_stories.find_one({"story_id": story_id})' in line:
        insert_idx2 = i + 1
        break

lines.insert(insert_idx2, '        logger.info(f"DEBUG_START: story found: {story is not None}")\n')

with open('AryaPremium/plugins/userbot/market_seller.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
