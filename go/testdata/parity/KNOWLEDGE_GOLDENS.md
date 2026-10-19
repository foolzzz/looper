# Knowledge and project skill read parity

The `knowledge*`, `document-*` and `project-skill*` responses are generated
through the repository's Python v2.10.0 handlers and view functions. The
fixture uses an owned temporary directory; path traversal and symlinks point
to harmless sibling files inside that fixture root. It never requires files
from an operator's home or runtime directory.

`make_goldens.py --db-url postgresql://postgres:postgres@127.0.0.1:54329/loopgo_...`
is the normal database-backed generator. Knowledge fixture projects are
loaded after the older responses have been captured, preserving their meaning.
`TestKnowledgeGoldenParity` loads the same schema, seed and additional fixture
into a disposable database and compares the Go HTTP responses.

These new golden files were initially produced by
`make_knowledge_goldens_offline.py` because the execution sandbox refused
localhost network connections. That fallback substitutes only the database
read functions and parses the actual INSERT fixture instead of duplicating
its rows. It calls the real Python HTTP handlers, serializers and views. It
also writes `knowledge_fixture_input.json` for database-free Go view tests.
**This offline generation does not verify SQL or database parity.** Run the
normal generator and the Go integration tests when PostgreSQL is reachable.

Unique root paths are replaced with `<knowledge-root>`. Each harness first
checks the response's original `index_chars` against its actual Unicode
character count, then compares the character count after replacing the root.
Document content, live size, missing status, file names, set hash, limits,
seeded dates and error messages retain their actual Python values.
