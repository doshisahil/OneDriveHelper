"""Folder upload service."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from onedrive_helper.config import FOLDER_CONCURRENCY
from onedrive_helper.models import FileStatus, FolderUploadResult


class FolderUploadService:
    """Upload a local folder tree into OneDrive."""

    def __init__(self, graph_client) -> None:
        self._graph_client = graph_client

    async def _upload_single_file(
        self,
        local_file_path: Path,
        remote_parent_id: str,
        remote_folder_path: str,
        semaphore: asyncio.Semaphore,
    ) -> FileStatus:
        async with semaphore:
            try:
                response = await self._graph_client.upload_file(str(local_file_path), remote_parent_id)
                item = response.get("item", {})
                status = response.get("status", "uploaded")
                return FileStatus(
                    name=local_file_path.name,
                    local_path=str(local_file_path),
                    cloud_path=item.get("cloud_path") or f"{remote_folder_path}/{local_file_path.name}",
                    size=local_file_path.stat().st_size,
                    status=status,
                )
            except (OSError, RuntimeError) as exc:
                return FileStatus(
                    name=local_file_path.name,
                    local_path=str(local_file_path),
                    size=local_file_path.stat().st_size if local_file_path.exists() else 0,
                    status="error",
                    message=str(exc),
                )

    async def run(self, local_folder_path: str, remote_onedrive_path: str) -> FolderUploadResult:
        """Upload a local folder tree to a OneDrive location."""
        local_root = Path(local_folder_path).expanduser().resolve()
        if not local_root.exists() or not local_root.is_dir():
            raise ValueError(f"Local path does not exist or is not a folder: {local_folder_path}")

        remote_root = await self._graph_client.ensure_remote_folder(remote_onedrive_path)
        remote_root_path = self._graph_client.normalize_remote_path(remote_onedrive_path)
        result = FolderUploadResult(local_path=str(local_root), remote_path=remote_root_path)
        semaphore = asyncio.Semaphore(FOLDER_CONCURRENCY)
        remote_ids: dict[Path, str] = {Path("."): remote_root["id"]}
        remote_paths: dict[Path, str] = {Path("."): remote_root_path.rstrip("/") or "/"}

        for current_root, dir_names, file_names in os.walk(local_root):
            current_path = Path(current_root)
            relative_path = current_path.relative_to(local_root)
            relative_key = Path(".") if str(relative_path) == "." else relative_path
            parent_id = remote_ids[relative_key]
            parent_remote_path = remote_paths[relative_key]

            for dir_name in sorted(dir_names):
                child_folder = await self._graph_client.ensure_child_folder(parent_id, dir_name)
                child_key = relative_key / dir_name
                remote_ids[child_key] = child_folder["id"]
                remote_paths[child_key] = (parent_remote_path.rstrip("/") + "/" + dir_name).rstrip("/")

            tasks = [
                self._upload_single_file(current_path / file_name, parent_id, parent_remote_path, semaphore)
                for file_name in sorted(file_names)
            ]
            for file_status in await asyncio.gather(*tasks):
                result.total_files += 1
                result.files.append(file_status)
                if file_status.status == "uploaded":
                    result.uploaded_files += 1
                elif file_status.status == "skipped":
                    result.skipped_files += 1
                else:
                    result.failed_files += 1
                    if file_status.message:
                        result.errors.append(f"{file_status.local_path}: {file_status.message}")
        return result
