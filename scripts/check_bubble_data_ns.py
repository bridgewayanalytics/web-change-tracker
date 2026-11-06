import asyncio, sys, os
sys.path.insert(0, '.')

os.environ['OPENAI_FETCH_FROM_SSM'] = 'true'
from bubble.ssm_loader import load_db_env_from_ssm
load_db_env_from_ssm()

from bubble.pgvector.client import init_pg_pool, get_pg_pool

async def main():
    await init_pg_pool()
    pool = get_pg_pool()

    # List all docs in bubble-data namespace
    rows = await pool.fetch("""
        SELECT title, content_type, updated_at
        FROM documents
        WHERE namespace = 'bubble-data'
          AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY updated_at DESC
        LIMIT 50
    """)
    print(f"bubble-data namespace ({len(rows)} docs):")
    for r in rows:
        print(f"  [{r['content_type']}] {r['title']} | updated={str(r['updated_at'])[:19]}")

    # Also search specifically for agendaitems
    print("\n--- Search for 'agendaitems' in title ---")
    rows2 = await pool.fetch("""
        SELECT title, content_type, updated_at, length(content) as chars
        FROM documents
        WHERE namespace = 'bubble-data'
          AND lower(title) LIKE '%agendaitem%'
          AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY updated_at DESC
    """)
    for r in rows2:
        print(f"  {r['title']} | chars={r['chars']} | updated={str(r['updated_at'])[:19]}")

asyncio.run(main())
