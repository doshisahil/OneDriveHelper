"""Album creation service."""

# pylint: disable=too-few-public-methods

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from onedrive_helper.config import BATCH_LIMIT
from onedrive_helper.models import AlbumCreationResult


@dataclass(slots=True)
class AlbumState:
    """Persisted resume metadata for album creation."""

    album_id: Optional[str]
    added_ids: set[str]
    source_folder: Optional[dict[str, str]]


class AlbumCreatorService:
    """Create or update a OneDrive photo album from a source folder."""

    def __init__(self, graph_client) -> None:
        self._graph_client = graph_client

    @staticmethod
    def _default_state_path(album_id: str) -> str:
        short_id = album_id.replace("-", "")[:12]
        return f".album_state_{short_id}.json"

    @staticmethod
    def _load_state(path: str) -> AlbumState:
        if not os.path.exists(path):
            return AlbumState(album_id=None, added_ids=set(), source_folder=None)
        with open(path, encoding="utf-8") as file_handle:
            data = json.load(file_handle)
        source_folder = data.get("source_folder")
        if not isinstance(source_folder, dict):
            source_folder = None
        return AlbumState(
            album_id=data.get("album_id"),
            added_ids=set(data.get("added_ids", [])),
            source_folder=source_folder,
        )

    @staticmethod
    def _save_state(
        path: str,
        album_id: str,
        added_ids: set[str],
        source_folder: dict[str, str],
    ) -> None:
        temp_path = path + ".tmp"
        payload = {
            "album_id": album_id,
            "added_ids": sorted(added_ids),
            "source_folder": source_folder,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(temp_path, "w", encoding="utf-8") as file_handle:
            json.dump(payload, file_handle, indent=2)
        os.replace(temp_path, path)

    async def _add_items(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        album_id: str,
        item_ids: list[str],
        already_added: set[str],
        state_path: str,
        source_folder: dict[str, str],
    ) -> tuple[int, int]:
        pending = [item_id for item_id in item_ids if item_id not in already_added]
        success_count = 0
        failure_count = 0

        for batch_start in range(0, len(pending), BATCH_LIMIT):
            chunk = pending[batch_start : batch_start + BATCH_LIMIT]
            requests = [
                {
                    "id": str(index),
                    "method": "POST",
                    "url": f"/me/drive/bundles/{album_id}/children",
                    "headers": {"Content-Type": "application/json"},
                    "body": {"id": item_id},
                }
                for index, item_id in enumerate(chunk)
            ]
            try:
                response = await self._graph_client.post_batch(requests)
            except RuntimeError:
                failure_count += len(chunk)
                self._save_state(state_path, album_id, already_added, source_folder)
                continue

            for item in response.get("responses", []):
                request_index = int(item.get("id", -1))
                if request_index < 0 or request_index >= len(chunk):
                    continue
                item_id = chunk[request_index]
                status = item.get("status", 0)
                if status in (200, 201, 204, 409):
                    already_added.add(item_id)
                    success_count += 1
                else:
                    failure_count += 1
            self._save_state(state_path, album_id, already_added, source_folder)
        return success_count, failure_count

    async def run(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        source_folder: Optional[dict[str, str]] = None,
        *,
        album_name: Optional[str] = None,
        album_id: Optional[str] = None,
        dry_run: bool = False,
        resume_path: Optional[str] = None,
    ) -> AlbumCreationResult:
        """Create or update an album using a selected OneDrive folder."""
        resume_state = AlbumState(album_id=None, added_ids=set(), source_folder=None)
        if resume_path:
            resume_state = self._load_state(resume_path)
        if source_folder is None:
            source_folder = resume_state.source_folder
        if source_folder is None:
            raise ValueError("A source folder is required for album creation.")

        resolved_album_id = resume_state.album_id or album_id
        default_album_name = f"{source_folder['name']} Album"
        resolved_album_name = album_name or default_album_name
        counters = {"folders": 0, "files": 0}
        media_items = await self._graph_client.enumerate_media(
            source_folder["id"],
            source_folder["path"],
            counters=counters,
        )

        result = AlbumCreationResult(
            source_folder_id=source_folder["id"],
            source_folder_path=source_folder["path"],
            source_folder_name=source_folder["name"],
            album_id=resolved_album_id,
            album_name=resolved_album_name,
            total_discovered=len(media_items),
            pre_existing_skip=len(resume_state.added_ids),
            dry_run=dry_run,
        )
        if dry_run or not media_items:
            return result

        if resolved_album_id is None:
            album = await self._graph_client.create_album(resolved_album_name)
            resolved_album_id = album["id"]
            resolved_album_name = album.get("name", resolved_album_name)

        state_path = resume_path or self._default_state_path(resolved_album_id)
        item_ids = [item["id"] for item in media_items]
        added_files, failed_files = await self._add_items(
            resolved_album_id,
            item_ids,
            resume_state.added_ids,
            state_path,
            source_folder,
        )

        result.album_id = resolved_album_id
        result.album_name = resolved_album_name
        result.added_files = added_files
        result.failed_files = failed_files
        result.state_path = state_path
        if failed_files == 0 and Path(state_path).exists():
            Path(state_path).unlink()
            result.state_path = None
        return result
