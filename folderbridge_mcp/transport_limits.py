from __future__ import annotations

# FolderBridge transport framing is intentionally much larger than the legacy
# 1 MiB ceiling. This is a per-request framing bound, not a file-size limit:
# exact edit remains 128 MiB and transactional text write remains 512 MiB.
MAX_MCP_MESSAGE_BYTES = 32 * 1024 * 1024
MAX_MCP_MESSAGE_MIB = MAX_MCP_MESSAGE_BYTES // (1024 * 1024)

# Whole-file transactional writes use chunks sized so even pathological JSON
# escaping (up to 6 wire bytes for one input byte) stays below the MCP frame.
MAX_TRANSACTION_CHUNK_BYTES = 4 * 1024 * 1024
