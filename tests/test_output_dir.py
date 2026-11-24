import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "trio-handoff.py"


class OutputDirectoryTests(unittest.TestCase):
    def test_default_output_creates_missing_desktop_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "session.jsonl"
            source.write_text(
                json.dumps({"type": "user", "message": {"content": "test goal"}})
                + "\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["HOME"] = str(root)
            self.assertFalse((root / "Desktop").exists())

            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), str(source)],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            created = list((root / "Desktop").glob("trio-handoff-cc2cx-*.md"))
            self.assertEqual(len(created), 1)

    def test_explicit_out_creates_missing_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "session.jsonl"
            source.write_text(
                json.dumps({"type": "user", "message": {"content": "test goal"}})
                + "\n",
                encoding="utf-8",
            )
            out_file = root / "nested" / "dir" / "bundle.md"

            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), str(source), "--out", str(out_file)],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_file.is_file())


if __name__ == "__main__":
    unittest.main()
