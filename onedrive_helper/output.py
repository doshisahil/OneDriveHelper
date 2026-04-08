"""Output helpers for CLI results."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from onedrive_helper.models import (
    AlbumCreationResult,
    DiskCleanupResult,
    FolderUploadResult,
    SyncScanReport,
)


def to_dict(value: Any) -> Any:
    """Convert dataclasses and nested structures into JSON-serializable values."""
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [to_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: to_dict(item) for key, item in value.items()}
    return value


def export_json(value: Any, output_path: str) -> None:
    """Write a result model to disk as JSON."""
    with open(output_path, "w", encoding="utf-8") as file_handle:
        json.dump(to_dict(value), file_handle, indent=2)


def print_result(value: Any) -> None:
    """Render a result model to stdout."""
    if isinstance(value, DiskCleanupResult):
        print(f"Scanned files: {value.scanned_files}")
        print(f"Deleted files: {value.deleted_files}")
        print(f"Backed up files: {value.backed_up_files}")
        print(f"Skipped files: {value.skipped_files}")
    elif isinstance(value, AlbumCreationResult):
        print(f"Source folder: {value.source_folder_path}")
        print(f"Album: {value.album_name or 'N/A'}")
        print(f"Album ID: {value.album_id or 'N/A'}")
        print(f"Discovered files: {value.total_discovered}")
        print(f"Pre-existing items: {value.pre_existing_skip}")
        print(f"Added this run: {value.added_files}")
        print(f"Failed: {value.failed_files}")
        if value.dry_run:
            print("Dry run only: no changes were written.")
    elif isinstance(value, FolderUploadResult):
        print(f"Remote path: {value.remote_path}")
        print(f"Total files: {value.total_files}")
        print(f"Uploaded files: {value.uploaded_files}")
        print(f"Skipped files: {value.skipped_files}")
        print(f"Failed files: {value.failed_files}")
    elif isinstance(value, SyncScanReport):
        print(f"Total files: {value.total_files}")
        print(f"Synced files: {value.synced_files}")
        print(f"Unsynced files: {value.unsynced_files}")
        print(f"Synced bytes: {value.synced_bytes}")
        print(f"Unsynced bytes: {value.unsynced_bytes}")
    else:
        print(json.dumps(to_dict(value), indent=2))
