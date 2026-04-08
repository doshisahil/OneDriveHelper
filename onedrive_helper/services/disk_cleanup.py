"""Disk cleanup service for removing already-synced local files."""

# pylint: disable=too-few-public-methods

from __future__ import annotations

import os
import shutil
from pathlib import Path

from onedrive_helper.config import VALID_MEDIA_SUFFIXES
from onedrive_helper.models import DiskCleanupResult, FileStatus


class DiskCleanupService:
    """Delete local files that already exist on OneDrive."""

    def __init__(self, graph_client) -> None:
        self._graph_client = graph_client

    @staticmethod
    def _should_include(path: Path) -> bool:
        return path.suffix.lower() in VALID_MEDIA_SUFFIXES

    @staticmethod
    def _backup_file(path: Path, local_path: Path, backup_path: Path) -> None:
        relative_path = path.relative_to(local_path)
        backup_file_path = backup_path / relative_path
        backup_file_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_file_path)

    async def run(self, local_path: str, backup_path: str | None = None) -> DiskCleanupResult:
        """Run the cleanup flow for a local directory."""
        local_root = Path(local_path).expanduser().resolve()
        if not local_root.exists() or not local_root.is_dir():
            raise ValueError(f"Local path does not exist or is not a folder: {local_path}")

        backup_root = None
        if backup_path:
            backup_root = Path(backup_path).expanduser().resolve()
            backup_root.mkdir(parents=True, exist_ok=True)

        result = DiskCleanupResult(local_path=str(local_root), backup_path=str(backup_root) if backup_root else None)
        for path in local_root.rglob("*"):
            if not path.is_file() or not self._should_include(path):
                continue

            result.scanned_files += 1
            file_size = path.stat().st_size
            try:
                matches = await self._graph_client.search_file(path.name, str(path))
            except (OSError, RuntimeError) as exc:
                result.errors.append(f"{path}: {exc}")
                result.files.append(
                    FileStatus(
                        name=path.name,
                        local_path=str(path),
                        status="error",
                        size=file_size,
                        message=str(exc),
                    )
                )
                continue

            if matches:
                if backup_root is not None:
                    self._backup_file(path, local_root, backup_root)
                    result.backed_up_files += 1
                os.remove(path)
                result.deleted_files += 1
                result.files.append(
                    FileStatus(
                        name=path.name,
                        local_path=str(path),
                        cloud_path=matches[0].get("cloud_path"),
                        size=file_size,
                        status="deleted",
                    )
                )
            else:
                result.skipped_files += 1
                result.files.append(
                    FileStatus(
                        name=path.name,
                        local_path=str(path),
                        size=file_size,
                        status="skipped",
                    )
                )
        return result
