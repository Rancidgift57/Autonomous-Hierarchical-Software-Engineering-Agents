"""Filesystem tools (Phase 9): read_file, write_file, edit_file, delete_file,
list_files, search_files.

Every tool resolves paths exclusively through `ctx.sandbox` -- none of them
touch `Path`/`os` with a raw, caller-supplied path.
"""

from __future__ import annotations

import fnmatch
import re

from app.tools.base import BaseTool, ToolContext, ToolResult
from app.tools.exceptions import PathValidationError, ToolError
from app.tools.permissions import Permission

_MAX_SEARCH_MATCHES = 200
_MAX_SEARCH_FILES = 2000
_TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".md",
    ".txt", ".toml", ".cfg", ".ini", ".html", ".css", ".sql", ".env",
    ".sh", ".gitignore",
}


class ReadFileTool(BaseTool):
    name = "read_file"
    required_permission = Permission.READ

    async def _run(self, ctx: ToolContext, path: str, **_: object) -> ToolResult:
        resolved = ctx.sandbox.resolve(path, must_exist=True)
        if not resolved.is_file():
            return ToolResult(tool_name=self.name, success=False, error=f"Not a file: '{path}'.")
        size = resolved.stat().st_size
        ctx.sandbox.check_size(size)
        try:
            content = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            return ToolResult(
                tool_name=self.name, success=False, error=f"File is not valid UTF-8 text: {exc}"
            )
        except OSError as exc:
            # A platform-level failure (e.g. the file is locked by another
            # process on Windows, or a permissions error) must degrade to
            # a normal failed ToolResult, not an unhandled exception that
            # crashes the whole task -- same fault-tolerance principle
            # already applied to subprocess calls in app/tools/shell.py.
            return ToolResult(tool_name=self.name, success=False, error=f"Could not read '{path}': {exc}")
        return ToolResult(
            tool_name=self.name,
            success=True,
            output=content,
            metadata={"path": path, "bytes": size},
        )


class WriteFileTool(BaseTool):
    name = "write_file"
    required_permission = Permission.WRITE

    async def _run(
        self, ctx: ToolContext, path: str, content: str, overwrite: bool = True, **_: object
    ) -> ToolResult:
        resolved = ctx.sandbox.resolve(path)
        ctx.sandbox.check_size(len(content.encode("utf-8")))

        if resolved.exists() and not overwrite:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"File already exists and overwrite=False: '{path}'.",
            )

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            # Same rationale as ReadFileTool above. This is the specific
            # gap that made a Windows-only path-length limit, a locked
            # file, or a permissions error take down the entire worker
            # task (and, via that, the whole project run) instead of
            # surfacing as one failed write the manager/self-healing loop
            # could see and react to.
            return ToolResult(tool_name=self.name, success=False, error=f"Could not write '{path}': {exc}")
        return ToolResult(
            tool_name=self.name,
            success=True,
            output=None,
            metadata={"path": path, "bytes_written": len(content.encode("utf-8"))},
        )


class EditFileTool(BaseTool):
    """Unique find/replace edit, mirroring the `str_replace` computer tool."""

    name = "edit_file"
    required_permission = Permission.WRITE

    async def _run(
        self, ctx: ToolContext, path: str, old_str: str, new_str: str = "", **_: object
    ) -> ToolResult:
        resolved = ctx.sandbox.resolve(path, must_exist=True)
        ctx.sandbox.check_size(resolved.stat().st_size)
        try:
            text = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(tool_name=self.name, success=False, error=f"Could not read '{path}': {exc}")

        count = text.count(old_str)
        if count == 0:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"old_str not found in '{path}'.",
            )
        if count > 1:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"old_str is not unique in '{path}' ({count} occurrences).",
            )

        updated = text.replace(old_str, new_str, 1)
        try:
            resolved.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return ToolResult(tool_name=self.name, success=False, error=f"Could not write '{path}': {exc}")
        return ToolResult(
            tool_name=self.name,
            success=True,
            output=None,
            metadata={"path": path},
        )


class DeleteFileTool(BaseTool):
    """Destructive -- requires elevated (ADMIN) permission."""

    name = "delete_file"
    required_permission = Permission.ADMIN

    async def _run(self, ctx: ToolContext, path: str, **_: object) -> ToolResult:
        resolved = ctx.sandbox.resolve(path, must_exist=True)
        if not resolved.is_file():
            return ToolResult(tool_name=self.name, success=False, error=f"Not a file: '{path}'.")
        try:
            resolved.unlink()
        except OSError as exc:
            return ToolResult(tool_name=self.name, success=False, error=f"Could not delete '{path}': {exc}")
        return ToolResult(tool_name=self.name, success=True, metadata={"path": path})


class ListFilesTool(BaseTool):
    name = "list_files"
    required_permission = Permission.READ

    async def _run(
        self, ctx: ToolContext, path: str = ".", pattern: str | None = None, **_: object
    ) -> ToolResult:
        resolved = ctx.sandbox.resolve(path, must_exist=True)
        if not resolved.is_dir():
            return ToolResult(
                tool_name=self.name, success=False, error=f"Not a directory: '{path}'."
            )

        entries: list[str] = []
        try:
            children = sorted(resolved.iterdir())
        except OSError as exc:
            return ToolResult(tool_name=self.name, success=False, error=f"Could not list '{path}': {exc}")
        for child in children:
            rel = ctx.sandbox.relative(child)
            if pattern and not fnmatch.fnmatch(child.name, pattern):
                continue
            entries.append(rel + ("/" if child.is_dir() else ""))

        return ToolResult(tool_name=self.name, success=True, output=entries)


class SearchFilesTool(BaseTool):
    """Regex content search across text files under `path`, sandbox-scoped."""

    name = "search_files"
    required_permission = Permission.READ
    default_timeout = 15.0

    async def _run(
        self,
        ctx: ToolContext,
        query: str,
        path: str = ".",
        max_results: int = 50,
        **_: object,
    ) -> ToolResult:
        resolved = ctx.sandbox.resolve(path, must_exist=True)
        try:
            regex = re.compile(query)
        except re.error as exc:
            raise ToolError(f"Invalid search regex: {exc}") from exc

        matches: list[dict[str, object]] = []
        scanned = 0

        candidates = [resolved] if resolved.is_file() else sorted(resolved.rglob("*"))
        for file_path in candidates:
            already_full = len(matches) >= min(max_results, _MAX_SEARCH_MATCHES)
            if scanned >= _MAX_SEARCH_FILES or already_full:
                break
            if not file_path.is_file() or file_path.suffix not in _TEXT_SUFFIXES:
                continue
            try:
                # Re-validate every candidate through the sandbox, defence
                # in depth against a future rglob change following symlinks.
                ctx.sandbox.resolve(ctx.sandbox.relative(file_path))
            except PathValidationError:
                continue

            scanned += 1
            try:
                text = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(
                        {
                            "path": ctx.sandbox.relative(file_path),
                            "line": lineno,
                            "text": line.strip()[:300],
                        }
                    )
                    if len(matches) >= min(max_results, _MAX_SEARCH_MATCHES):
                        break

        return ToolResult(
            tool_name=self.name,
            success=True,
            output=matches,
            metadata={"files_scanned": scanned},
        )


def build_filesystem_tools() -> list[BaseTool]:
    return [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        DeleteFileTool(),
        ListFilesTool(),
        SearchFilesTool(),
    ]
