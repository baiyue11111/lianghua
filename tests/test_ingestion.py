from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from market.ingest import sha256_file


class IngestionTests(unittest.TestCase):
    def test_sha256_file_is_content_addressed(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.txt"
            path.write_text("audit me", encoding="utf-8")
            self.assertEqual(
                sha256_file(path),
                "3a7f63b380ceb6f5a07f7a8faa342e14252c0fdd5c874bf9ce7cc01b99f6d934",
            )


if __name__ == "__main__":
    unittest.main()
