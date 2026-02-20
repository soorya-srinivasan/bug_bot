# RAG (Retrieval-Augmented Generation) Implementation

## Overview

This changeset introduces a RAG-powered chat system into Bug Bot, enabling engineers to query historical bugs, investigations, and findings using natural language. The system embeds structured bug data into a PostgreSQL-backed vector store (via `pgvector`), then retrieves semantically relevant documents at query time and feeds them as context to Claude Haiku for answer generation.

---

## Architecture

```
User question
     │
     ▼
┌──────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  POST /chat  │────▶│  embed_query()   │────▶│ similarity_search │
│  (FastAPI)   │     │ (MiniLM-L6-v2)  │     │ (pgvector cosine)│
└──────────────┘     └──────────────────┘     └──────────────────┘
                                                      │
                                              top-k documents
                                                      │
                                                      ▼
                                              ┌──────────────────┐
                                              │   Claude Haiku   │
                                              │  (answer gen)    │
                                              └──────────────────┘
                                                      │
                                                      ▼
                                              { answer, sources[] }
```

---

## Files Changed / Added

### New files (the `src/bug_bot/rag/` package)

| File | Purpose |
|------|---------|
| `rag/__init__.py` | Package exports: `rag_chat`, `index_bug_report`, `index_investigation`, `index_finding`, `reindex_all` |
| `rag/embeddings.py` | Embedding layer — wraps `sentence-transformers` with lazy-loaded singleton model |
| `rag/vectorstore.py` | Vector storage and retrieval — `store_embeddings`, `similarity_search`, `delete_by_source`, `get_stats` |
| `rag/indexer.py` | Document indexing — transforms ORM models into text chunks, embeds them, stores in vector DB |
| `rag/chat.py` | Chat orchestration — embeds user query, retrieves context, calls Claude Haiku to generate answer |

### Modified files

| File | What changed |
|------|--------------|
| `models/models.py` | Added `RagDocument` SQLAlchemy model with `pgvector.sqlalchemy.Vector(384)` column |
| `config.py` | Added `rag_embedding_model` and `rag_top_k` settings |
| `api/admin.py` | Added three new endpoints: `POST /chat`, `POST /rag/index`, `GET /rag/stats` |
| `docker-compose.yml` | Swapped PostgreSQL image from `postgres:16` to `pgvector/pgvector:pg16` (includes the `vector` extension) |
| `pyproject.toml` | Added `sentence-transformers>=3.0.0` and `pgvector>=0.3.0` dependencies |
| `alembic/versions/f8a2b3c4d5e6_...py` | Migration to create the `rag_documents` table with HNSW index |

---

## Detailed Component Breakdown

### 1. Database Schema (`rag_documents` table)

The Alembic migration (`f8a2b3c4d5e6`) performs:

1. **Enables the `vector` extension** — `CREATE EXTENSION IF NOT EXISTS vector`
2. **Creates the table:**

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK, auto-generated via `gen_random_uuid()` |
| `source_type` | `VARCHAR(30)` | One of: `bug_report`, `investigation`, `finding` |
| `source_id` | `VARCHAR(100)` | Bug ID or composite key like `BUG-001:finding-uuid` |
| `chunk_text` | `TEXT` | The plain-text representation of the source document |
| `chunk_metadata` | `JSONB` | Structured metadata (severity, status, services, etc.) |
| `embedding` | `vector(384)` | 384-dimensional dense vector from the MiniLM model |
| `created_at` | `TIMESTAMPTZ` | Row creation time |
| `updated_at` | `TIMESTAMPTZ` | Last update time |

3. **Creates two indexes:**
   - `idx_rag_documents_source` — B-tree on `(source_type, source_id)` for fast delete-before-reindex lookups
   - `idx_rag_documents_embedding_hnsw` — **HNSW** index on the embedding column using `vector_cosine_ops`, configured with `m=16` and `ef_construction=64` for approximate nearest neighbor search

The corresponding SQLAlchemy model (`RagDocument`) in `models/models.py` mirrors this schema using `pgvector.sqlalchemy.Vector(384)` for the embedding column.

### 2. Embedding Layer (`rag/embeddings.py`)

Uses `sentence-transformers` with a **lazy-loaded singleton** pattern:

- **Model:** `all-MiniLM-L6-v2` (configurable via `settings.rag_embedding_model`)
  - 384-dimensional output
  - ~22M parameters, fast inference on CPU
  - Good general-purpose semantic similarity performance
- **`embed_texts(texts)`** — batch-encodes a list of strings, returns `list[list[float]]`. Uses `normalize_embeddings=True` so cosine similarity reduces to a dot product.
- **`embed_query(query)`** — encodes a single query string, returns `list[float]`.

The model is loaded once on first call and cached in the module-level `_model` variable. This avoids repeated model loading across requests.

### 3. Document Indexer (`rag/indexer.py`)

Responsible for transforming ORM objects into embeddable text chunks. Three source types are supported:

#### Bug Report chunks

Built from `BugReport` model fields:

```
Bug ID: BUG-001
Severity: P1
Status: investigating
Report: <original_message>
```

#### Investigation chunks

Built from `Investigation` model. Includes conditional fields:

```
Bug ID: BUG-001
Summary: <summary>
Root Cause: <root_cause>          # if present
Fix Type: config_change
Confidence: 0.92
Recommended Actions: ...          # if present, joined with semicolons
Services: payment-api, auth-svc   # if present, joined with commas
PR: https://github.com/...        # if present
```

#### Finding chunks

Built from `InvestigationFinding` model:

```
Bug ID: BUG-001
Category: error_rate
Severity: high
Finding: <finding text>
```

The `source_id` for findings uses a composite key format `{bug_id}:{finding_id}` to support multiple findings per bug.

#### Indexing functions

- **`index_bug_report(session, bug_id)`** — indexes or re-indexes a single bug. Deletes existing chunks for that source before inserting.
- **`index_investigation(session, bug_id)`** — same pattern for investigations.
- **`index_finding(session, finding_id)`** — same pattern for individual findings.
- **`reindex_all(session)`** — full re-index of all bug reports, investigations, and findings. Batch-embeds each type for efficiency, then stores. Returns counts per type.

All indexing functions follow a **delete-then-insert** pattern (not upsert), ensuring stale chunks are removed before new ones are written. Each function commits after storing.

### 4. Vector Store (`rag/vectorstore.py`)

Low-level storage and retrieval against the `rag_documents` table:

- **`store_embeddings(session, documents)`** — inserts a list of document dicts (each with `source_type`, `source_id`, `chunk_text`, `chunk_metadata`, `embedding`) as `RagDocument` rows. Returns the count of inserted rows.

- **`similarity_search(session, query_embedding, top_k=5)`** — executes a raw SQL query using the `<=>` cosine distance operator:

  ```sql
  SELECT id, source_type, source_id, chunk_text, chunk_metadata,
         1 - (embedding <=> '[...]'::vector) AS similarity
  FROM rag_documents
  ORDER BY embedding <=> '[...]'::vector
  LIMIT :top_k
  ```

  The vector literal is inlined into the SQL string (not as a bind parameter) to avoid asyncpg's conflict between `:param` bind syntax and PostgreSQL's `::` cast operator. The `top_k` parameter is still bound normally. Similarity is computed as `1 - cosine_distance`, so values closer to 1.0 indicate higher relevance.

- **`delete_by_source(session, source_type, source_id)`** — removes all rows matching a given source type and ID. Used before re-indexing to prevent duplicates.

- **`get_stats(session)`** — returns aggregate statistics: total document count, count grouped by `source_type`, and the timestamp of the most recently indexed document.

### 5. Chat Orchestration (`rag/chat.py`)

The `rag_chat()` function ties the pipeline together:

1. **Embed the user's question** via `embed_query(message)`
2. **Retrieve top-k similar documents** via `similarity_search(session, query_embedding, top_k=settings.rag_top_k)` (default `top_k=5`)
3. **Build context blocks** — each retrieved document is formatted as `[SOURCE_TYPE — source_id]\n{chunk_text}`, separated by `---` dividers
4. **Construct the prompt** — conversation history (if any) is prepended, then the user message is wrapped with the retrieved context
5. **Call Claude Haiku** (`claude-haiku-4-5-20251001`) with:
   - A system prompt that instructs the model to answer only from context, cite bug IDs, and be concise/technical
   - `max_tokens=1024`
   - `max_retries=5` on the Anthropic client
6. **Return** a dict with `answer` (the generated text) and `sources` (list of retrieved chunks with similarity scores)

The system prompt enforces grounded answers:
- Only use retrieved context (no hallucination)
- Always cite Bug IDs
- Acknowledge when context is insufficient
- Be concise and technical

### 6. API Endpoints (`api/admin.py`)

Three new endpoints were added to the admin router:

#### `POST /chat`

**Request body:**

```json
{
  "message": "What caused the payment failures last week?",
  "conversation_history": [
    { "role": "user", "content": "..." },
    { "role": "assistant", "content": "..." }
  ]
}
```

**Response:**

```json
{
  "answer": "Based on the investigation of BUG-042...",
  "sources": [
    {
      "bug_id": "BUG-042",
      "source_type": "investigation",
      "chunk_text": "...",
      "similarity": 0.87
    }
  ]
}
```

Supports multi-turn conversation through the `conversation_history` field.

#### `POST /rag/index`

Triggers a full re-index of all bugs, investigations, and findings. Returns counts:

```json
{
  "status": "completed",
  "total": 150,
  "indexed": {
    "bug_reports": 50,
    "investigations": 50,
    "findings": 50
  }
}
```

#### `GET /rag/stats`

Returns current index statistics:

```json
{
  "total_documents": 150,
  "by_type": {
    "bug_report": 50,
    "investigation": 50,
    "finding": 50
  },
  "last_indexed_at": "2026-02-21T10:30:00+00:00"
}
```

---

## Infrastructure Changes

### Docker Compose

The PostgreSQL image was changed from `postgres:16` to `pgvector/pgvector:pg16`. This image includes the `pgvector` extension pre-compiled, so the `CREATE EXTENSION IF NOT EXISTS vector` in the migration succeeds without manual extension installation.

### Dependencies (`pyproject.toml`)

Two new packages:

- **`sentence-transformers>=3.0.0`** — provides the `SentenceTransformer` class for encoding text into dense vectors. Pulls in PyTorch, transformers, and tokenizers as transitive dependencies.
- **`pgvector>=0.3.0`** — provides `pgvector.sqlalchemy.Vector` column type for SQLAlchemy, enabling native vector storage and operations.

### Configuration (`config.py`)

Two new settings (both with sensible defaults):

| Setting | Default | Description |
|---------|---------|-------------|
| `rag_embedding_model` | `all-MiniLM-L6-v2` | HuggingFace model ID for sentence-transformers |
| `rag_top_k` | `5` | Number of documents to retrieve per query |

---

## Design Decisions

1. **pgvector over a dedicated vector DB** — keeps the architecture simple (single PostgreSQL instance) and avoids introducing Pinecone/Weaviate/Qdrant as an additional service. Suitable for the expected document count (hundreds to low thousands).

2. **HNSW index over IVFFlat** — HNSW provides better recall at query time without requiring `SET ivfflat.probes`. The `m=16, ef_construction=64` parameters balance build speed vs. query accuracy for the expected dataset size.

3. **384-dim MiniLM vs. larger models** — `all-MiniLM-L6-v2` is chosen for fast CPU inference. The 384-dimensional output is compact and sufficient for matching bug descriptions against queries. A larger model (e.g., `all-mpnet-base-v2` at 768 dims) could be swapped in via config if accuracy needs improve.

4. **Delete-then-insert over upsert** — simplifies the indexing logic. Since re-indexing is infrequent and document counts are low, the extra DELETE + INSERT is negligible.

5. **Inlined vector literal in SQL** — the `similarity_search` function inlines the vector as a SQL literal instead of using a bind parameter. This works around asyncpg's parsing conflict between `:param` bind syntax and PostgreSQL's `::` cast operator.

6. **Claude Haiku for answer generation** — Haiku (`claude-haiku-4-5-20251001`) provides fast, cost-effective responses for a RAG chatbot. The model is instructed via system prompt to stay grounded in the retrieved context and cite bug IDs.
