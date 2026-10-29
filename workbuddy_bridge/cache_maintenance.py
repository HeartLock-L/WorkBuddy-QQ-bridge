"""Maintain explicitly owned cache paths without importing the bot runtime."""

import stat
import time
from pathlib import Path


def cleanup_root(work_dir: Path, path: Path, expected: Path) -> Path | None:
    """Validate an exact maintenance directory without following redirections.

    Args:
        work_dir: Boundary containing all managed directories.
        path: Configured or enumerated directory.
        expected: Explicitly permitted absolute location inside work_dir.

    Returns:
        The absolute directory, or None for missing, redirected, or invalid paths.
    """
    try:
        base = work_dir.absolute()
        root = path.absolute()
        if root != expected.absolute() or not root.is_relative_to(base):
            return None
        if root.resolve() != root:
            return None
        current = root
        while True:
            info = current.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 1024
            ):
                return None
            if current == base:
                return root
            current = current.parent
    except (OSError, RuntimeError):
        return None


def clean_old_attachments(
    work_dir: Path, attachment_root: Path, keep_days: int, stop_event=None
) -> int:
    """Remove expired received attachments without entering redirected trees.

    Args:
        work_dir: Boundary containing all managed directories.
        attachment_root: Configured received-attachment directory.
        keep_days: Retention period; nonpositive values disable deletion.
        stop_event: Optional threading event set during plugin shutdown.

    Returns:
        Deleted file count. User output directories are outside this scope.
    """
    if keep_days <= 0 or (stop_event is not None and stop_event.is_set()):
        return 0
    root = cleanup_root(work_dir, attachment_root, work_dir / "收到的附件")
    if root is None:
        return 0

    cutoff = time.time() - keep_days * 86400
    removed = 0
    pending = [root]
    directories = []
    while pending:
        if stop_event is not None and stop_event.is_set():
            return removed
        directory = pending.pop()
        if cleanup_root(work_dir, directory, directory) is None:
            continue
        try:
            entries = list(directory.iterdir())
        except (OSError, RuntimeError):
            continue
        for path in entries:
            if stop_event is not None and stop_event.is_set():
                return removed
            try:
                info = path.lstat()
                if (
                    getattr(info, "st_file_attributes", 0) & 1024
                    or path.resolve() != path
                ):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    directories.append(path)
                elif stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                    if cleanup_root(work_dir, directory, directory) is not None:
                        path.unlink()
                        removed += 1
            except (OSError, RuntimeError):
                continue
    for directory in reversed(directories):
        if stop_event is not None and stop_event.is_set():
            break
        try:
            if cleanup_root(work_dir, directory, directory) is not None:
                directory.rmdir()  # Only empty, verified directories can be removed.
        except (OSError, RuntimeError):
            continue
    return removed


def temp_cache_entries(work_dir: Path, temp_root: Path):
    """Yield regular files in the two explicitly managed cache directories.

    Args:
        work_dir: Boundary containing all managed directories.
        temp_root: Configured media-cache directory.

    Yields:
        A path and its stat result. Links and unowned subdirectories are skipped.
    """
    root = cleanup_root(work_dir, temp_root, work_dir / "AstrBot" / "data" / "temp")
    if root is None:
        return
    # Visit owned idle images first so a large flat cache cannot starve them.
    for directory in (root / "idle_pics", root):
        if cleanup_root(work_dir, directory, directory) is None:
            continue
        try:
            entries = list(directory.iterdir())
        except (OSError, RuntimeError):
            continue
        for path in entries:
            try:
                info = path.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 1024
                ):
                    continue
                if path.resolve() == path:
                    yield path, info
            except (OSError, RuntimeError):
                continue


def clean_old_temp(
    work_dir: Path, temp_root: Path, keep_days: int, max_files: int, stop_event=None
) -> tuple[int, int]:
    """Remove expired cache files within the shared per-pass deletion limit.

    Args:
        work_dir: Boundary containing all managed directories.
        temp_root: Configured media-cache directory.
        keep_days: Retention period; nonpositive values disable deletion.
        max_files: Shared deletion limit across both media directories.
        stop_event: Optional threading event set when the plugin is unloading.

    Returns:
        Deleted file count and freed bytes; recent files and directories remain.
    """
    if keep_days <= 0 or (stop_event is not None and stop_event.is_set()):
        return 0, 0
    cutoff = time.time() - keep_days * 86400
    removed = freed = 0
    for path, _info in temp_cache_entries(work_dir, temp_root):
        if removed >= max_files or (stop_event is not None and stop_event.is_set()):
            break
        try:
            if cleanup_root(work_dir, path.parent, path.parent) is None:
                continue
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 1024
            ):
                continue
            if info.st_mtime >= cutoff or path.resolve() != path:
                continue
            path.unlink()
            removed += 1
            freed += info.st_size
        except (OSError, RuntimeError):
            continue
    return removed, freed


def temp_usage(work_dir: Path, temp_root: Path) -> int:
    """Measure the same cache scope that maintenance can clean.

    Args:
        work_dir: Boundary containing all managed directories.
        temp_root: Configured media-cache directory.

    Returns:
        Bytes in temp's top level and idle_pics, excluding links and other trees.
    """
    return sum(info.st_size for _path, info in temp_cache_entries(work_dir, temp_root))
