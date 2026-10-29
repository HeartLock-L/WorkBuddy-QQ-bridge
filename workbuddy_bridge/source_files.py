"""Inventory the owned Python modules without importing the bot framework."""

import hashlib
from pathlib import Path

SOURCE_NAMES = (
    "main.py",
    "memory_store.py",
    "memory_service.py",
    "bridge_policy.py",
    "draw_flow.py",
    "proactive_chat.py",
    "jimeng.py",
    "image_jobs.py",
    "image_job_commands.py",
    "request_queue.py",
    "prompts.py",
    "reply_text.py",
    "source_files.py",
    "cache_maintenance.py",
    "runtime_status.py",
    "attachments.py",
    "meme_library.py",
    "meme_collection.py",
)


def source_paths(entry: Path) -> tuple[Path, ...]:
    """List owned source paths in the same order for every consumer.

    Args:
        entry: Plugin entry point, including a renamed entry in offline fixtures.

    Returns:
        Entry point followed by its declared sibling modules.
    """
    entry = Path(entry)
    return (entry, *(entry.with_name(name) for name in SOURCE_NAMES[1:]))


def source_fingerprint(entry: Path, *, missing_ok: bool = False) -> str:
    """Hash source names and bytes with explicit boundaries.

    Args:
        entry: Plugin entry point whose sibling modules share this inventory.
        missing_ok: Allow absent siblings in partial legacy or offline fixtures.
            Installed plugins and runtime imports use the strict default.

    Returns:
        SHA-256 fingerprint of the declared source files.

    Raises:
        OSError: A required source file cannot be read.
    """
    entry = Path(entry)
    digest = hashlib.sha256()
    for path in source_paths(entry):
        if missing_ok and path != entry and not path.exists():
            continue
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
