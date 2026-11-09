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

    # Inspect schema first
    cols_docs = await pool.fetch("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name = 'documents'
        ORDER BY ordinal_position
    """)
    print("documents columns:", [c['column_name'] for c in cols_docs])
    cols_chunks = await pool.fetch("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name = 'document_chunks'
        ORDER BY ordinal_position
    """)
    print("chunks columns:   ", [c['column_name'] for c in cols_chunks])
    print()

    docs = await pool.fetch(
        "SELECT * FROM documents WHERE namespace = $1 LIMIT 5", namespace
    )
    print(f"Documents in namespace '{namespace}': {len(docs)}")
    for d in docs:
        d_dict = dict(d)
        print(f"  {d_dict}")

    print()
    chunks = await pool.fetch(
        """SELECT c.id, c.chunk_index, c.content, c.content_type, length(c.content) AS chars
           FROM document_chunks c JOIN documents d ON c.document_id = d.id
           WHERE d.namespace = $1 ORDER BY c.chunk_index""", namespace,
    )
    print(f"Chunks: {len(chunks)}")
    for c in chunks:
        txt = c['content'] or ''
        print(f"  [idx={c['chunk_index']}] chars={c['chars']} content_type={c['content_type']}")
        print(f"    FULL CONTENT:")
        print(f"    {txt}")
        print()


asyncio.run(main())
