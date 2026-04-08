"""Dataclasses representing CLI and service results."""

# pylint: disable=too-many-instance-attributes

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class FileStatus:
    """Status for a single local file."""

    name: str
    local_path: str
    status: str
    size: int = 0
    cloud_path: Optional[str] = None
    message: Optional[str] = None


@dataclass(slots=True)
class DiskCleanupResult:
    """Result for the disk cleanup flow."""

    local_path: str
    backup_path: Optional[str]
    scanned_files: int = 0
    deleted_files: int = 0
    backed_up_files: int = 0
    skipped_files: int = 0
    errors: list[str] = field(default_factory=list)
    files: list[FileStatus] = field(default_factory=list)


@dataclass(slots=True)
class AlbumCreationResult:
    """Result for the album creation flow."""

    source_folder_id: str
    source_folder_path: str
    source_folder_name: str
    album_id: Optional[str] = None
    album_name: Optional[str] = None
    total_discovered: int = 0
    pre_existing_skip: int = 0
    added_files: int = 0
    failed_files: int = 0
    dry_run: bool = False
    state_path: Optional[str] = None


@dataclass(slots=True)
class FolderUploadResult:
    """Result for a folder upload flow."""

    local_path: str
    remote_path: str
    total_files: int = 0
    uploaded_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    errors: list[str] = field(default_factory=list)
    files: list[FileStatus] = field(default_factory=list)


@dataclass(slots=True)
class SyncScanReport:
    """Summary of a local folder scan versus OneDrive content."""

    local_path: str
    total_files: int = 0
    synced_files: int = 0
    unsynced_files: int = 0
    synced_bytes: int = 0
    unsynced_bytes: int = 0
    synced: list[FileStatus] = field(default_factory=list)
    unsynced: list[FileStatus] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
