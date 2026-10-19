"""Regressions for database-only catalog fixture generation."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import make_catalog_goldens as generator


class CatalogGeneratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_matching_collation_preserves_capture_file_order(self):
        from argparse import Namespace
        from looper.db import connection

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            shutil.copyfile(generator.HERE / "catalog_fixture_input.json", output / "catalog_fixture_input.json")
            pool = AsyncMock()
            pool.fetchrow.return_value = {"datcollate": "en_US.utf8", "datctype": "en_US.utf8"}
            # Only verifies publishing, not SQL collation; real DB parity is required separately.
            captured = b'{"files":[{"path":"references/note.txt"},{"path":"SKILL.md"}]}\n'
            with patch.object(generator, "HERE", output), \
                    patch.object(connection, "init_pool", new=AsyncMock(return_value=pool)), \
                    patch.object(connection, "close_pool", new=AsyncMock()), \
                    patch.object(generator, "capture", new=AsyncMock(return_value={"catalog.json": captured})), \
                    contextlib.redirect_stdout(io.StringIO()):
                await generator.main(Namespace(db_url="postgresql://postgres:postgres@127.0.0.1:54329/loopgo_generator_test"))
            self.assertEqual((output / "catalog.json").read_bytes(), captured)
            self.assertEqual((output / "catalog_fixture.sql").read_text(),
                             generator.fixture_sql(json.loads((output / "catalog_fixture_input.json").read_text())))

    async def test_collation_mismatch_cannot_replace_schema_or_goldens(self):
        from argparse import Namespace
        from looper.db import connection

        for collate, ctype in (("C", "C"), ("en_US.utf8", "C")):
            with self.subTest(collate=collate, ctype=ctype), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp)
                shutil.copyfile(generator.HERE / "catalog_fixture_input.json", output / "catalog_fixture_input.json")
                (output / "catalog.json").write_text("existing database golden\n")
                before = {p.name: p.read_bytes() for p in output.iterdir()}
                pool = AsyncMock()
                pool.fetchrow.return_value = {"datcollate": collate, "datctype": ctype}
                with patch.object(generator, "HERE", output), \
                        patch.object(connection, "init_pool", new=AsyncMock(return_value=pool)), \
                        patch.object(connection, "close_pool", new=AsyncMock()), \
                        patch.object(generator, "capture", new=AsyncMock(return_value={})):
                    with self.assertRaisesRegex(ValueError, "en_US.utf8"):
                        await generator.main(Namespace(db_url="postgresql://postgres:postgres@127.0.0.1:54329/loopgo_generator_test"))
                pool.execute.assert_not_awaited()
                self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, before)

    def test_offline_generation_cannot_replace_goldens(self):
        # Run a copy so the pre-fix generator cannot overwrite repository files.
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            for name in ("make_catalog_goldens.py", "make_goldens.py", "catalog_fixture_input.json"):
                shutil.copyfile(generator.HERE / name, output / name)
            (output / "raw").mkdir()
            golden = output / "catalog.json"
            golden.write_text("existing database golden\n")
            # make_goldens locates the checkout from its own path.
            script = output / "make_goldens.py"
            script.write_text(script.read_text().replace(
                "REPO = HERE.parents[2]",
                f"REPO = Path({str(generator.goldens.REPO)!r})"))
            result = subprocess.run([sys.executable, str(output / "make_catalog_goldens.py"), "--offline"],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(golden.read_text(), "existing database golden\n")

    async def test_connection_failure_leaves_outputs_untouched(self):
        from argparse import Namespace
        from looper.db import connection

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            shutil.copyfile(generator.HERE / "catalog_fixture_input.json", output / "catalog_fixture_input.json")
            for name in ("catalog_fixture.sql", "catalog.json"):
                (output / name).write_text("existing database output\n")
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            with patch.object(generator, "HERE", output), \
                    patch.object(connection, "init_pool", new=AsyncMock(side_effect=OSError("unreachable"))):
                with self.assertRaisesRegex(OSError, "unreachable"):
                    await generator.main(Namespace(db_url="postgresql://postgres:postgres@127.0.0.1:54329/loopgo_generator_test"))
            self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
