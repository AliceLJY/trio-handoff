import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "trio-handoff.py"
SPEC = importlib.util.spec_from_file_location("trio_handoff", MODULE_PATH)
trio_handoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trio_handoff)


def assembled(*parts):
    """Build fake credential shapes at runtime so push protection sees no token literal."""
    return "".join(parts)


class RedactionTests(unittest.TestCase):
    def test_common_secret_shapes_are_removed(self):
        secrets = [
            assembled("eyJhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", ".",
                      "ZmFrZXNpZ25hdHVyZTEyMzQ1Njc4OTA"),
            assembled("AK", "IA", "IOSFODNN7EXAMPLE"),
            assembled("AI", "za", "SyD1234567890abcdefghijklmnopqrst"),
            assembled("sk", "-", "ant-api03-fake_token_value_1234567890"),
            assembled("github", "_pat_", "11AA22BB33CC44DD55EE66FF77GG88HH"),
            assembled("gh", "p_", "1234567890abcdefghijklmnopqrst"),
            assembled("xox", "b-", "1234567890-abcdefghijklmnop"),
            assembled("xai", "-", "fake_token_value_1234567890"),
            assembled("jina", "_", "fake_token_value_1234567890"),
            assembled("npm", "_", "fake_token_value_1234567890"),
            assembled("hf", "_", "fake_token_value_1234567890"),
            assembled("1234567890", ":", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"),
            "url-user",
            "url-password",
            "quoted-openai-secret-value",
            "shell-secret-value",
            "flag-secret-value",
            "header-secret-value",
            "alice@example.com",
        ]
        source = "\n".join([
            secrets[0],
            secrets[1],
            secrets[2],
            secrets[3],
            secrets[4],
            secrets[5],
            secrets[6],
            secrets[7],
            secrets[8],
            secrets[9],
            secrets[10],
            secrets[11],
            "https://url-user:url-password@example.test/repo.git",
            '\"OPENAI_API_KEY\": \"quoted-openai-secret-value\"',
            "export SERVICE_AUTH_TOKEN=shell-secret-value",
            "tool --provider-api-key flag-secret-value --safe-mode",
            "Authorization: Basic header-secret-value",
            secrets[18],
        ])

        result = trio_handoff.redact(source)

        for secret in secrets:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, result)
        self.assertIn("https://[redacted-user]:[redacted-password]@example.test/repo.git", result)
        self.assertIn('\"OPENAI_API_KEY\": \"[redacted]\"', result)

    def test_multiline_private_keys_are_removed(self):
        private_key = """-----BEGIN PRIVATE KEY-----
ZmFrZS1rZXktbGluZS0x
ZmFrZS1rZXktbGluZS0y
-----END PRIVATE KEY-----"""
        result = trio_handoff.redact(f"before\n{private_key}\nafter")
        self.assertEqual(result, "before\n[redacted-private-key]\nafter")

    def test_non_secret_context_is_preserved(self):
        source = "git status --short\nhttps://example.test/repo.git\ntokenization is not a token"
        self.assertEqual(trio_handoff.redact(source), source)


if __name__ == "__main__":
    unittest.main()
