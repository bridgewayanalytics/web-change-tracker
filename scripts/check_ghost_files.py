"""Probe pgvector for the ghost filenames the agent instructions reference.

Lists top-50 distinct document titles per namespace and specifically searches
for 01_calendar_items / 02_agenda_items / 03_library_items in any namespace.
"""
import asyncio, sys, os
sys.path.insert(0, '.')

os.environ['OPENAI_FETCH_FROM_SSM'] = 'true'
from bubble.ssm_loader import load_db_env_from_ssm
load_db_env_from_ssm()

from bubble.pgvector.client import init_pg_pool, get_pg_pool


async def main():
    await init_pg_pool()
    pool = get_pg_pool()

    # 1. Namespace summary — counts per namespace
    print("=== NAMESPACES ===")
    rows = await pool.fetch("""
        SELECT namespace, COUNT(*) AS doc_count
        FROM documents
        WHERE (expires_at IS NULL OR expires_at > NOW())
        GROUP BY namespace
        ORDER BY doc_count DESC
    """)
    for r in rows:
        print(f"  {r['namespace']:<50} {r['doc_count']:>6}")

    # 2. Search for the ghost filenames anywhere
    print("\n=== GHOST FILENAME SEARCH ===")
    for name in ['01_calendar_items', '02_agenda_items', '03_library_items',
                 'calendar_items', 'agenda_items', 'library_items']:
        rows = await pool.fetch("""
            SELECT namespace, title, COUNT(*) OVER () AS total
            FROM documents
            WHERE (expires_at IS NULL OR expires_at > NOW())
              AND lower(title) LIKE $1
            LIMIT 5
        """, f'%{name.lower()}%')
        total = rows[0]['total'] if rows else 0
        print(f"  \"{name}\": {total} doc(s) with that in the title")
        for r in rows[:3]:
            print(f"    [{r['namespace']}] {r['title']}")

    # 3. Sample bubble-data titles to see what the agent actually has access to
    print("\n=== bubble-data top titles (by update recency) ===")
    rows = await pool.fetch("""
        SELECT title, content_type, updated_at
        FROM documents
        WHERE namespace = 'bubble-data'
          AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY updated_at DESC
        LIMIT 30
    """)
    for r in rows:
        print(f"  [{r['content_type']}] {r['title'][:90]}")


asyncio.run(main())
