from __future__ import annotations

# These versions are intentionally explicit. Increment PARSER_VERSION when a
# parser/normalization change can alter the sections produced from identical
# source HTML. Increment INDEX_VERSION when the persisted docs.db index format
# or tokenization changes and therefore requires a full rebuild.
PARSER_VERSION = 1
INDEX_VERSION = 1
