import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "trio-handoff.py"


class GitDiffCliTests(unittest.TestCase):
    def test_invalid_base_exits_nonzero_without_writing_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Trio Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "trio@example.test"],
                check=True,
            )
            (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-q", "-m", "initial"],
                check=True,
            )

            source = root / "session.jsonl"
            source.write_text(
                json.dumps({"type": "user", "message": {"content": "test goal"}})
                + "\n",
                encoding="utf-8",
            )
            bundle = root / "bundle.md"
            missing_base = "definitely-not-a-ref"

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    str(source),
                    "--repo",
                    str(repo),
                    "--base",
                    missing_base,
                    "--out",
                    str(bundle),
                ],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(" diff ", result.stderr)
            self.assertIn(missing_base, result.stderr)
            self.assertFalse(bundle.exists())


if __name__ == "__main__":
    unittest.main()
