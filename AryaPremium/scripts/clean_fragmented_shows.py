import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from config import Config
except ImportError:
    from AzyaPremium.config import Config

from motor.motor_asyncio import AsyncIOMotorClient

async def clean_fragmented_shows():
    mongo_uri = getattr(Config, 'MONGO_URI', None) or getattr(Config, 'DATABASE_URI', None)
    db_name = getattr(Config, 'DATABASE_NAME', 'forward-bot')

    if not mongo_uri:
        print('[ERROR] No MONGO_URI configured.')
        return

    print(f'[INFO] Connecting to database {db_name}...')
    client = AsyncIOMotorClient(mongo_uri)
    db = client[db_name]

    print('[INFO] Searching for fragmented show records...')
    query = {
        '$and': [
            {
                '$or': [
                    {'platform': {'$regex': '^(kuku tv|story tv)$', '$options': 'i'}},
                    {'is_show': True}
                ]
            },
            {
                '$or': [
                    {'parts': {'$size': 0}},
                    {'parts': {'$size': 1}},
                    {'parts': {'$exists': False}},
                    {'file_count': {'$lte': 1}}
                ]
            }
        ]
    }

    fragmented_docs = await db.premium_stories.find(query).to_list(length=500)
    print(f'[INFO] Found {len(fragmented_docs)} candidate fragmented records:')

    for doc in fragmented_docs:
        t = doc.get('title') or doc.get('story_name_en')
        p = doc.get('platform')
        parts_len = len(doc.get('parts', []))
        doc_id = doc.get('_id')
        print(f" - [ID: {doc_id}] Title: {t} | Platform: {p} | Parts: {parts_len}")

    if fragmented_docs:
        del_result = await db.premium_stories.delete_many(query)
        print(f'\n[SUCCESS] Successfully deleted {del_result.deleted_count} fragmented show records.')
    else:
        print('\n[INFO] Database is already clean. No fragmented records found.')

if __name__ == '__main__':
    asyncio.run(clean_fragmented_shows())
