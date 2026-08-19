"""Workspace sandbox / path validation (Phase 9).

Every filesystem-touching tool resolves its paths through a
`WorkspaceSandbox` instead of touching `Path`/`os` directly. This is the
single choke point that guarantees no tool -- however it was invoked, and
regardless of what an LLM suggested -- can read or write outside the
project's workspace directory.
"""

from __future__ import annotations

from pathlib import Path

from app.tools.exceptions import PathValidationError

#: Directories that are never readable/writable through the tool system,
#: even though they live inside the sandbox root. Direct manipulation of
#: version-control internals or nested environments must go through the
#: dedicated git tools (Phase 9 git_* tools), never raw file I/O.
#:
#: Compared case-insensitively (see `resolve()`) -- on Windows and macOS,
#: the filesystem itself is case-insensitive, so a check against exact
#: casing only (".git" but not ".GIT"/".Git") is not actually a security
#: boundary on those platforms: an LLM-authored path spelled with
#: different casing resolves to the very same directory this list exists
#: to protect, silently bypassing the block.
_BLOCKED_DIR_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv"}

#: Windows reserves these device names in *every* directory, regardless of
#: extension (`con.py`, `NUL.txt`, `com1.md` are all reserved) and
#: regardless of case. Creating/opening one of these on Windows doesn't
#: fail cleanly at the pathlib/validation layer -- it raises a raw
#: `OSError` deep inside `Path.write_text()`/`open()`, which (before this
#: fix) nothing in the tool chain caught, so an LLM asking to write e.g.
#: `app/con.py` (a perfectly ordinary-looking module name that happens to
#: collide with a reserved device) would crash the entire task instead of
#: getting a clean, actionable validation error. Checking for this here --
#: on every platform, not just when running on Windows -- means the same
#: project behaves identically regardless of where it's run, rather than
#: working on Linux/macOS and only failing once someone runs it on
#: Windows.
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def _is_windows_reserved_name(part: str) -> bool:
    """True if `part` (a single path component, e.g. `"con.py"`) collides
    with a Windows-reserved device name -- the base name before the first
    `.`, compared case-insensitively, exactly matching Windows' own rule.
    """

    stem = part.split(".", 1)[0]
    return stem.upper() in _WINDOWS_RESERVED_NAMES


class WorkspaceSandbox:
    """Confines all path resolution to a single root directory.

    Args:
        root: the project workspace directory. Created if it doesn't exist.
        max_file_bytes: the largest file a read/write tool may touch.
    """

    def __init__(self, root: str | Path, max_file_bytes: int = 2_000_000):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_file_bytes = max_file_bytes

    def resolve(self, relative_path: str, *, must_exist: bool = False) -> Path:
        """Resolve `relative_path` to an absolute path guaranteed inside `root`.

        Raises:
            PathValidationError: on absolute paths, `..` traversal, symlink
                escapes, references to blocked directories, references to
                a Windows-reserved device name, or (if `must_exist`) a
                path that doesn't exist.
        """

        if not relative_path or not isinstance(relative_path, str):
            raise PathValidationError("Path must be a non-empty string.")

        # Every path an LLM produces for this tool system is documented as
        # forward-slash (POSIX-style), and that's what every caller in
        # this codebase sends. But nothing stops a Windows-hosted model,
        # a copy-pasted Windows path, or a caller running on Windows from
        # producing backslashes -- and `Path` treats backslash as an
        # ordinary filename character on POSIX (not a separator), so
        # `"app\\main.py"` would resolve to a single, literal, almost
        # certainly non-existent filename containing a backslash instead
        # of the nested path that was clearly intended. Normalizing here
        # means path strings behave identically regardless of which
        # separator style produced them or which OS is running them.
        normalized = relative_path.replace("\\", "/")

        raw = Path(normalized)
        if raw.is_absolute():
            raise PathValidationError(
                f"Absolute paths are not allowed: '{relative_path}'."
            )
        # `Path.is_absolute()` alone is not a reliable check cross-platform:
        # a POSIX-rooted path like "/etc/passwd" is absolute on Linux/macOS
        # but pathlib does NOT consider it absolute on Windows (Windows
        # "absolute" requires a drive letter or UNC root; a bare leading
        # "/" is merely "drive-relative"). Without this explicit check,
        # such a path would slip past the `is_absolute()` guard above when
        # running on Windows. `candidate.relative_to(self.root)` below
        # still catches the resulting escape as a defense-in-depth
        # backstop, but rejecting it here gives a clear, specific error
        # instead of a generic "escapes the sandbox" one.
        if normalized.startswith("/"):
            raise PathValidationError(
                f"Absolute paths are not allowed: '{relative_path}'."
            )
        if any(part == ".." for part in raw.parts):
            raise PathValidationError(
                f"Path traversal ('..') is not allowed: '{relative_path}'."
            )
        if any(part.lower() in _BLOCKED_DIR_NAMES for part in raw.parts):
            raise PathValidationError(
                f"Path touches a blocked directory: '{relative_path}'."
            )
        reserved = next((part for part in raw.parts if _is_windows_reserved_name(part)), None)
        if reserved is not None:
            raise PathValidationError(
                f"'{reserved}' is a reserved device name on Windows and "
                f"cannot be used as a file or directory name: '{relative_path}'."
            )

        candidate = (self.root / raw).resolve()

        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PathValidationError(
                f"Resolved path escapes the workspace sandbox: '{relative_path}'."
            ) from exc

        if must_exist and not candidate.exists():
            raise PathValidationError(f"Path does not exist: '{relative_path}'.")

        return candidate

    def check_size(self, size_bytes: int) -> None:
        if size_bytes > self.max_file_bytes:
            raise PathValidationError(
                f"File size {size_bytes} bytes exceeds the "
                f"{self.max_file_bytes}-byte sandbox limit."
            )

    def relative(self, absolute_path: Path) -> str:
        """Inverse of `resolve`: express an in-sandbox path relative to root.

        Always returns forward-slash ('/') separated output, even on
        Windows. These strings are used as artifact identifiers, compared
        against LLM-authored paths like `target_files`/`WorkerFileChange.path`
        (which are always forward-slash, since the model has no notion of
        the host OS), and returned directly to API/UI consumers -- so the
        separator must be stable across platforms, not `str(Path)`'s
        OS-native one (which is '\\' on Windows and silently breaks any
        exact-string comparison against a forward-slash path).
        """

        return absolute_path.resolve().relative_to(self.root).as_posix()
