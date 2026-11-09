"""Bounded, read-only inspection of explicitly configured files."""

import os
import stat
from pathlib import Path

from scrappy_doo.orchestration import Tool


class LocalToolError(Exception):
    """A requested local inspection is not permitted or cannot be performed."""


class LocalTools:
    def __init__(self, *, roots: tuple[Path, ...], max_file_bytes: int = 1_000_000, max_matches: int = 100):
        if not roots or max_file_bytes <= 0 or max_matches <= 0:
            raise ValueError("Roots and positive inspection limits are required")
        self._roots = tuple(Path(root).resolve() for root in roots)
        self._max_file_bytes = max_file_bytes
        self._max_matches = max_matches

    def _read(self, path: str) -> str:
        if not isinstance(path, str):
            raise LocalToolError("Path must be a string")
        try:
            resolved = Path(path).resolve(strict=True)
            root = next((root for root in self._roots if resolved.is_relative_to(root)), None)
            if root is None:
                raise LocalToolError("Path is outside the configured roots")
            parts = resolved.relative_to(root).parts
            if not parts:
                raise LocalToolError("Path is not a regular file")
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for component in parts[:-1]:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = child
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise LocalToolError("Path is not a regular file")
                    with os.fdopen(descriptor, "rb", closefd=False) as source:
                        content = source.read(self._max_file_bytes + 1)
                finally:
                    os.close(descriptor)
            finally:
                os.close(directory)
            if len(content) > self._max_file_bytes:
                raise LocalToolError("File exceeds the configured size limit")
            if b"\x00" in content:
                raise LocalToolError("Binary files cannot be inspected as text")
            return content.decode("utf-8")
        except LocalToolError:
            raise
        except (OSError, UnicodeError, ValueError) as error:
            raise LocalToolError("Cannot read file as UTF-8 text") from error

    def read_lines(self, *, path: str, start_line: int, end_line: int) -> str:
        if (not isinstance(start_line, int) or isinstance(start_line, bool)
                or not isinstance(end_line, int) or isinstance(end_line, bool)
                or start_line < 1 or end_line < start_line):
            raise LocalToolError("Invalid line range")
        lines = self._read(path).splitlines()
        return "\n".join(lines[start_line - 1:end_line])

    def search_text(self, *, path: str, query: str) -> str:
        if not isinstance(query, str) or not query:
            raise LocalToolError("A nonempty search query is required")
        matches = []
        truncated = False
        for number, line in enumerate(self._read(path).splitlines(), 1):
            if query in line:
                if len(matches) == self._max_matches:
                    truncated = True
                    break
                matches.append(f"{number}: {line}")
        if truncated:
            matches.append("[Results truncated]")
        return "\n".join(matches)

    def as_tools(self) -> dict[str, Tool]:
        return {
            "read_lines": Tool(handler=self.read_lines, required_arguments=("path", "start_line", "end_line")),
            "search_text": Tool(handler=self.search_text, required_arguments=("path", "query")),
        }
