"""Proposed scoped, read-only local inspection tools for coverage review."""

import tempfile
import unittest
from pathlib import Path

from scrappy_doo.local_tools import LocalToolError, LocalTools


class LocalToolsTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.base = Path(temporary_directory.name)
        self.root = self.base / "allowed"
        self.root.mkdir()
        self.file = self.root / "notes.txt"
        self.file.write_text("first line\ncart total 20\nlast line\n", encoding="utf-8")
        self.outside = self.base / "outside.txt"
        self.outside.write_text("private content\n", encoding="utf-8")
        self.tools = LocalTools(roots=(self.root,), max_file_bytes=1024, max_matches=2)

    def test_read_selected_lines_from_allowed_file(self):
        result = self.tools.read_lines(path=str(self.file), start_line=2, end_line=3)

        self.assertIn("cart total 20", result)
        self.assertIn("last line", result)
        self.assertNotIn("first line", result)

    def test_search_returns_matching_line_and_line_number(self):
        result = self.tools.search_text(path=str(self.file), query="cart total")

        self.assertIn("cart total 20", result)
        self.assertIn("2", result)

    def test_tools_refuse_files_outside_configured_roots(self):
        with self.assertRaises(LocalToolError):
            self.tools.read_lines(path=str(self.outside), start_line=1, end_line=1)
        with self.assertRaises(LocalToolError):
            self.tools.search_text(path=str(self.outside), query="private")

    def test_parent_path_traversal_cannot_escape_root(self):
        path = self.root / ".." / "outside.txt"

        with self.assertRaises(LocalToolError):
            self.tools.read_lines(path=str(path), start_line=1, end_line=1)

    def test_symlink_to_outside_file_cannot_escape_root(self):
        link = self.root / "link.txt"
        link.symlink_to(self.outside)

        with self.assertRaises(LocalToolError):
            self.tools.read_lines(path=str(link), start_line=1, end_line=1)

    def test_invalid_line_range_is_rejected(self):
        for start, end in ((0, 1), (3, 2)):
            with self.subTest(start=start, end=end), self.assertRaises(LocalToolError):
                self.tools.read_lines(path=str(self.file), start_line=start, end_line=end)

    def test_file_size_limit_prevents_reading_an_oversized_file(self):
        self.file.write_text("x" * 1025, encoding="utf-8")

        with self.assertRaises(LocalToolError):
            self.tools.read_lines(path=str(self.file), start_line=1, end_line=1)

    def test_binary_file_is_not_treated_as_text(self):
        self.file.write_bytes(b"\xff\x00\xfe")

        with self.assertRaises(LocalToolError):
            self.tools.read_lines(path=str(self.file), start_line=1, end_line=1)

    def test_search_output_is_bounded_and_reports_truncation(self):
        self.file.write_text("match one\nmatch two\nmatch three\n", encoding="utf-8")

        result = self.tools.search_text(path=str(self.file), query="match")

        self.assertIn("match one", result)
        self.assertIn("match two", result)
        self.assertNotIn("match three", result)
        self.assertIn("truncat", result.lower())

    def test_inspection_does_not_change_the_file(self):
        original = self.file.read_bytes()

        self.tools.read_lines(path=str(self.file), start_line=1, end_line=2)
        self.tools.search_text(path=str(self.file), query="cart")

        self.assertEqual(self.file.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
