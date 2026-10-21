# Gate boundary

`services/rio-gate/gate.py` is an existing FastAPI rule-based evidence service. POST `/gate` receives text and availability flags (`has_memory`, `has_screen`, `has_file`, `has_code`, `has_device_info`, `allow_speculation`). It returns allow/need_evidence/allow_speculation and associated evidence/persistence instructions.

The caller asserts those flags. Gate does not read a file, observe a device or authenticate a memory merely because a flag is true. It is not an independent truth oracle or an execution authorization server.

Selected ordinary chat paths call this endpoint when enabled. Explicit tool routes use Catalog/Runner/adapter checks instead. Replies grounded in structured tool results have separate evidence handling. The offline demo avoids chat and does not need to start Gate.

Experimental training/evaluation datasets and model benchmarks are excluded. Not implemented: a universal model-based Gate, a cryptographic evidence attestation protocol, or a guarantee that every model sentence is verified. Future ideas are not described as current implementation.
