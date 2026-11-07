import subprocess
import sys
import unittest
from pathlib import Path

from pulse import redact


class RedactionLimitTests(unittest.TestCase):
    def test_long_non_email_token_with_at_is_processed_within_budget(self):
        payload = "я" * 40000 + "@"
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; from pulse import redact; "
                    "value = sys.stdin.read(); assert redact(value) == value",
                ],
                input=payload,
                text=True,
                capture_output=True,
                timeout=3,
                cwd=Path(__file__).resolve().parents[1],
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail("Маскирование одного длинного токена превысило 3 секунды")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_consecutive_addresses_and_invalid_candidates_preserve_masking(self):
        for text, expected in (
            ("a@b.com+next@c.org", "[EMAIL][EMAIL]"),
            ("wrong@name test+tag@sub.example.org", "wrong@name [EMAIL]"),
            ("русский.ящик@почта.example", "[EMAIL]"),
            ("a@b.c x@y.io", "a@b.c [EMAIL]"),
            ("x@@a@b.org", "x@@[EMAIL]"),
            (
                "почта a@b.com, телефон +7 999 123 45 67",
                "почта [EMAIL], телефон [PHONE]",
            ),
        ):
            with self.subTest(text=text):
                self.assertEqual(redact(text), expected)
