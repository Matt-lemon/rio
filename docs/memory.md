# Memory boundary

`core/memory/long_term.py` implements a local JSON store with explicit remember/search/forget operations and conservative malformed-file handling. It is not a vector database: search uses text/token similarity. `MemoryManager` applies the existing selection rules.

`NasMemoryClient` provides HTTP health, remember and search calls to an optional separately deployed memory API. Retrieval is enabled selectively by `rio_memory_context.py`; retrieved items and their provenance are input data, not tool permissions or identity overrides. No Qdrant/PostgreSQL database, embedding model, operator history or remote service is distributed here.

The Execution Store's conversation history/summary is distinct from long-term memory. Durable call results are not reconstructed from conversational summaries. The model does not own either storage format.

The preview demo needs no memory service and writes only temporary execution state. Remote authentication, encryption, backup/retention, privacy policy and database administration remain deployment responsibilities. Not implemented in this preview: a standalone portable vector/relational memory server, cryptographically verified memories or an automatic guarantee against all factual/provenance hallucinations.
