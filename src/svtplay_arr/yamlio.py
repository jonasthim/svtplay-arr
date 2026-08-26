"""Atomic YAML round-tripping for the config UI.

Both writers share this because the risky part is identical: a crash or a
full disk mid-write must not leave a truncated config that stops the
service booting. Same reasoning as the worker's publish -- only an atomic
rename makes a file visible.
"""

import os
import tempfile
from pathlib import Path

import yaml


class ConcurrentModification(RuntimeError):
    """The file changed since the form that is trying to write it was rendered."""


def read_with_mtime(path: Path) -> tuple[dict, float | None]:
    """Parsed contents plus the mtime to carry in a form.

    A missing file reads as empty rather than raising: the caller may be
    creating it for the first time.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    return (yaml.safe_load(text) or {}), path.stat().st_mtime


def atomic_write_yaml(
    path: Path,
    data: dict,
    *,
    header: list[str],
    expected_mtime: float | None = None,
    mode: int = 0o640,
) -> None:
    """Replace `path` atomically, keeping the previous contents as `.bak`.

    `expected_mtime` is the mtime the caller last saw. If the file has
    changed since, the write is refused -- two tabs, or a hand edit over
    SSH while a page is open, would otherwise silently discard one set of
    changes.

    The new content is fully written, fsynced and chmoded to a temp file
    *before* the target is touched at all, and the backup is made by
    hard-linking the original rather than moving it. That way `path`
    always holds either the old content or the new content -- never
    nothing, even if the process dies or the disk fills partway through.
    """
    if path.exists():
        current = path.stat().st_mtime
        if expected_mtime is not None and current != expected_mtime:
            raise ConcurrentModification(
                f"{path} changed since this form was opened; "
                "reload the page and reapply your change"
            )

    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    text = "".join(f"# {line}\n" for line in header) + body

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)

        if path.exists():
            backup = path.with_suffix(path.suffix + ".bak")
            backup.unlink(missing_ok=True)
            os.link(path, backup)

        os.replace(tmp, path)

        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
