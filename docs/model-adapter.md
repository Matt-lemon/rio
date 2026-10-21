# Replaceable model slot

`rio_core.py` reads `LLM_BASE_URL`, `LLM_MODEL`, sampling/time limits and an optional API key from local configuration. It builds payloads for an OpenAI-compatible chat-completions HTTP endpoint; this is a protocol choice, not a dependency on OpenAI's hosted API or Codex source code.

`core/context_tools.py` and `core/execution/planner.py` parse bounded model proposals. Tool names/arguments still pass through current Catalog validation and adapter permissions. Unparseable, missing, changed or ambiguous plans are not new permissions. Changing endpoint/model does not move identity, state, memory or policy into weights.

The planner, tool selector and ordinary response path have different payload/limit requirements. The existing contract is not a universal adapter for every model/API. Identical prompt/tool behavior across models, multimodal support, automatic model downloads and automatic GPU/server startup are **not implemented** guarantees.

The offline demo never calls this endpoint. For optional conversation use, copy a generic identity configuration and set the endpoint for a model you are entitled to run. Model weights and their licenses remain separate; no benchmark/model-quality claim is part of v0.1.
