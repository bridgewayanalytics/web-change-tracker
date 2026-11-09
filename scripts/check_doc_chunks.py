"""List pgvector chunks for a specific document-extraction namespace."""
import asyncio, sys, os
sys.path.insert(0, '.')
os.environ['OPENAI_FETCH_FROM_SSM'] = 'true'
from bubble.ssm_loader import load_db_env_from_ssm
load_db_env_from_ssm()
from bubble.pgvector.client import init_pg_pool, get_pg_pool


async def main():
    doc_uuid = sys.argv[1] if len(sys.argv) > 1 else "cf6b9c4fd482555a90a25834137e00bd"
    namespace = f"document-extraction:{doc_uuid}"
    print(f"Namespace: {namespace}")

    await init_pg_pool()
    pool = get_pg_pool()

    docs = await pool.fetch("""
        SELECT title, content_type, length(content) AS chars, updated_at
        FROM documents
        WHERE namespace = $1
          AND (expires_at IS NULL OR expires_at > NOW())
    """, namespace)
    print(f"Documents in namespace: {len(docs)}")
    for d in docs:
        print(f"  [{d['content_type']}] {d['title']}  chars={d['chars']}  updated={d['updated_at']}")

    chunks = await pool.fetch("""
        SELECT c.id, c.ordinal, length(c.text) AS chars, c.text
        FROM chunks c
        JOIN documents d ON c.document_id = d.id
        WHERE d.namespace = $1
          AND (d.expires_at IS NULL OR d.expires_at > NOW())
        ORDER BY c.ordinal
    """, namespace)
    print(f"\nChunks: {len(chunks)}")
    for c in chunks:
        preview = c['text'].replace("\n", " ")[:200]
        print(f"  [{c['ordinal']:>2}] chars={c['chars']:>5}  preview: {preview}")


asyncio.run(main())
