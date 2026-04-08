"""Sync scanner service."""

# pylint: disable=too-few-public-methods

from __future__ import annotations

import asyncio
from pathlib import Path

from onedrive_helper.config import FOLDER_CONCURRENCY, VALID_MEDIA_SUFFIXES
from onedrive_helper.models import FileStatus, SyncScanReport


class SyncScannerService:
    """Scan a local folder and report OneDrive sync status."""

    def __init__(self, graph_client) -> None:
        self._graph_client = graph_client

    @staticmethod
    def _accumulate_result(
        report: SyncScanReport,
        file_status: FileStatus,
        is_synced: bool,
    ) -> None:
        report.total_files += 1
        if file_status.status == "error":
            report.errors.append(f"{file_status.local_path}: {file_status.message}")
            return
        if is_synced:
            report.synced.append(file_status)
            report.synced_files += 1
            report.synced_bytes += file_status.size
            return
        report.unsynced.append(file_status)
        report.unsynced_files += 1
        report.unsynced_bytes += file_status.size

    @staticmethod
    def _should_include(path: Path, include_all: bool) -> bool:
        return include_all or path.suffix.lower() in VALID_MEDIA_SUFFIXES

    async def _scan_single_file(
        self,
        path: Path,
        semaphore: asyncio.Semaphore,
    ) -> tuple[FileStatus, bool]:
        async with semaphore:
            try:
                matches = await self._graph_client.search_file(path.name, str(path))
                if matches:
                    return (
                        FileStatus(
                            name=path.name,
                            local_path=str(path),
                            cloud_path=matches[0].get("cloud_path"),
                            size=path.stat().st_size,
                            status="synced",
                        ),
                        True,
                    )
                return (
                    FileStatus(
                        name=path.name,
                        local_path=str(path),
                        size=path.stat().st_size,
                        status="unsynced",
                    ),
                    False,
                )
            except (OSError, RuntimeError) as exc:
                return (
                    FileStatus(
                        name=path.name,
                        local_path=str(path),
                        size=path.stat().st_size if path.exists() else 0,
                        status="error",
                        message=str(exc),
                    ),
                    False,
                )

    async def run(self, local_folder_path: str, include_all: bool = False) -> SyncScanReport:
        """Scan a local folder and summarize synced versus unsynced files."""
        local_root = Path(local_folder_path).expanduser().resolve()
        if not local_root.exists() or not local_root.is_dir():
            raise ValueError(f"Local path does not exist or is not a folder: {local_folder_path}")

        report = SyncScanReport(local_path=str(local_root))
        semaphore = asyncio.Semaphore(FOLDER_CONCURRENCY)
        pending_tasks: list[asyncio.Future[tuple[FileStatus, bool]]] = []

        async def process_batch() -> None:
            for file_status, is_synced in await asyncio.gather(*pending_tasks):
                self._accumulate_result(report, file_status, is_synced)
            pending_tasks.clear()

        for path in local_root.rglob("*"):
            if not path.is_file() or not self._should_include(path, include_all):
                continue
            pending_tasks.append(asyncio.create_task(self._scan_single_file(path, semaphore)))
            if len(pending_tasks) >= FOLDER_CONCURRENCY:
                await process_batch()

        if pending_tasks:
            await process_batch()
        return report
