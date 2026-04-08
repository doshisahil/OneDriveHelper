"""Unified async Graph client for the OneDrive helper CLI."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import aiohttp
from azure.identity import InteractiveBrowserCredential

from onedrive_helper.config import (
    BATCH_LIMIT,
    FOLDER_CONCURRENCY,
    GRAPH_BASE,
    MEDIA_EXTENSION_ALLOWLIST,
    MEDIA_MIME_PREFIXES,
    PAGE_SIZE,
    RETRY_CAP,
    RETRY_MAX,
    SCOPES,
    SMALL_FILE_UPLOAD_BYTES,
    UPLOAD_CHUNK_SIZE,
    setup_logging,
)

log = setup_logging()
TOKEN_REFRESH_BUFFER_SECONDS = 60


class GraphRequestError(RuntimeError):
    """Graph request failure with status context."""

    def __init__(self, status: int, method: str, url: str, message: str) -> None:
        super().__init__(f"HTTP {status} [{method}] {url}: {message}")
        self.status = status
        self.method = method
        self.url = url
        self.message = message


class GraphClient:  # pylint: disable=too-many-public-methods
    """Minimal async Microsoft Graph REST client."""

    def __init__(self, credential: InteractiveBrowserCredential) -> None:
        self._credential = credential
        self._session: Optional[aiohttp.ClientSession] = None
        self._token_headers: Optional[dict[str, str]] = None
        self._token_expires_at: Optional[datetime] = None

    async def __aenter__(self) -> "GraphClient":
        connector = aiohttp.TCPConnector(limit=FOLDER_CONCURRENCY + 4)
        self._session = aiohttp.ClientSession(connector=connector)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session is not None:
            await self._session.close()

    @staticmethod
    def _token_is_valid(expires_at: Optional[datetime]) -> bool:
        if expires_at is None:
            return False
        return expires_at > datetime.now(timezone.utc)

    @staticmethod
    def _refresh_deadline(expires_on: int) -> datetime:
        """Refresh slightly before expiry, or immediately if that buffer has passed."""
        now = datetime.now(timezone.utc)
        refresh_timestamp = expires_on - TOKEN_REFRESH_BUFFER_SECONDS
        if refresh_timestamp <= int(now.timestamp()):
            return now
        return datetime.fromtimestamp(refresh_timestamp, tz=timezone.utc)

    @staticmethod
    def _encode_odata_search_term(value: str) -> str:
        """Double OData single quotes before URL-encoding the search term."""
        return quote(value.replace("'", "''"), safe="")

    async def _auth_headers(self) -> dict[str, str]:
        if self._token_headers is None or not self._token_is_valid(self._token_expires_at):
            token = await asyncio.to_thread(self._credential.get_token, *SCOPES)
            self._token_expires_at = self._refresh_deadline(token.expires_on)
            self._token_headers = {
                "Authorization": f"Bearer {token.token}",
                "Accept": "application/json",
            }
        return self._token_headers

    async def _request(
        self,
        method: str,
        url: str,
        *,
        auth: bool = True,
        headers: Optional[dict[str, str]] = None,
        **kwargs: Any,
    ) -> Any:
        if self._session is None:
            raise RuntimeError("GraphClient session is not open.")

        delay = 1
        request_headers = headers.copy() if headers else {}
        if auth:
            request_headers.update(await self._auth_headers())

        for attempt in range(1, RETRY_MAX + 1):
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=request_headers,
                    **kwargs,
                ) as response:
                    if response.status == 429:
                        wait_time = int(response.headers.get("Retry-After", delay))
                        log.warning(
                            "Rate-limited (429). Waiting %ds [attempt %d/%d].",
                            wait_time,
                            attempt,
                            RETRY_MAX,
                        )
                        await asyncio.sleep(wait_time)
                        delay = min(delay * 2, RETRY_CAP)
                        continue

                    if response.status in (500, 502, 503, 504):
                        log.warning(
                            "Server error %d. Retrying in %ds [attempt %d/%d].",
                            response.status,
                            delay,
                            attempt,
                            RETRY_MAX,
                        )
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, RETRY_CAP)
                        continue

                    body = await self._read_body(response)
                    if response.status >= 400:
                        message = self._extract_error_message(body)
                        raise GraphRequestError(response.status, method, url, message)
                    return body
            except aiohttp.ClientConnectionError as exc:
                if attempt == RETRY_MAX:
                    raise RuntimeError(str(exc)) from exc
                await asyncio.sleep(delay)
                delay = min(delay * 2, RETRY_CAP)

        raise RuntimeError(f"All retries exhausted for {method} {url}")

    @staticmethod
    async def _read_body(response: aiohttp.ClientResponse) -> Any:
        if response.status == 204:
            return {}

        text = await response.text()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    @staticmethod
    def _extract_error_message(body: Any) -> str:
        if isinstance(body, dict):
            return body.get("error", {}).get("message", "Graph request failed.")
        return str(body)

    @staticmethod
    def compute_hash(filename: str, hash_type: str = "sha256") -> str:
        """Compute a file hash using the requested algorithm."""
        hasher = hashlib.new(hash_type)
        with open(filename, "rb") as file_handle:
            while chunk := file_handle.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    async def get_url(self, url: str) -> dict[str, Any]:
        """Issue a GET to an absolute Graph URL."""
        response = await self._request("GET", url)
        return response if isinstance(response, dict) else {}

    async def get(self, path: str) -> dict[str, Any]:
        """Issue a GET against the Graph base URL."""
        return await self.get_url(f"{GRAPH_BASE}{path}")

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Issue a JSON POST against the Graph base URL."""
        response = await self._request(
            "POST",
            f"{GRAPH_BASE}{path}",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        return response if isinstance(response, dict) else {}

    async def put_bytes(
        self,
        url: str,
        payload: bytes,
        *,
        auth: bool = True,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Send bytes with PUT and return the resulting JSON body."""
        response = await self._request("PUT", url, data=payload, auth=auth, headers=headers)
        return response if isinstance(response, dict) else {}

    @staticmethod
    def format_item_path(item: dict[str, Any]) -> str:
        """Format a drive item path using the parent reference."""
        parent_path = item.get("parentReference", {}).get("path", "")
        if parent_path.startswith("/drive/root:"):
            parent_path = parent_path.replace("/drive/root:", "", 1)
        if not parent_path:
            parent_path = "/"
        if parent_path != "/":
            parent_path = parent_path.rstrip("/")
        item_name = item.get("name", "")
        if parent_path == "/":
            return f"/{item_name}" if item_name else "/"
        return f"{parent_path}/{item_name}" if item_name else parent_path

    @staticmethod
    def normalize_remote_path(remote_path: str) -> str:
        """Normalize a OneDrive path to a root-relative form."""
        cleaned = remote_path.strip()
        if not cleaned or cleaned == "/":
            return "/"
        return "/" + cleaned.strip("/")

    @staticmethod
    def _encode_remote_path(remote_path: str) -> str:
        path = GraphClient.normalize_remote_path(remote_path)
        if path == "/":
            return ""
        return "/".join(quote(part, safe="") for part in path.strip("/").split("/"))

    async def get_item(self, item_id: str) -> dict[str, Any]:
        """Fetch a drive item by its ID."""
        return await self.get(
            f"/me/drive/items/{item_id}?$select=id,name,size,file,folder,parentReference,webUrl"
        )

    async def get_item_by_path(self, remote_path: str) -> Optional[dict[str, Any]]:
        """Resolve a root-relative OneDrive path to a drive item."""
        normalized_path = self.normalize_remote_path(remote_path)
        if normalized_path == "/":
            return await self.get("/me/drive/root?$select=id,name,folder,parentReference")

        encoded_path = self._encode_remote_path(normalized_path)
        try:
            return await self.get(
                f"/me/drive/root:/{encoded_path}?$select=id,name,size,file,folder,parentReference,webUrl"
            )
        except GraphRequestError as exc:
            if exc.status == 404:
                return None
            raise

    async def search_file(self, file_name: str, file_path: str) -> list[dict[str, Any]]:
        """Search OneDrive by name and validate size and hash against a local file."""
        encoded_name = self._encode_odata_search_term(file_name)
        query_url = (
            f"{GRAPH_BASE}/me/drive/root/search(q='{encoded_name}')"
            "?$select=id,name,size,file,parentReference,webUrl"
        )
        item_list = await self.get_url(query_url)
        values = item_list.get("value", [])
        if not values:
            return []

        file_size = os.path.getsize(file_path)
        matches: list[dict[str, Any]] = []
        hash_cache: dict[str, str] = {}

        for item in values:
            if item.get("size") != file_size:
                continue

            hashes = item.get("file", {}).get("hashes", {})
            api_sha256 = hashes.get("sha256Hash", "").lower()
            api_sha1 = hashes.get("sha1Hash", "").lower()

            if api_sha256 and api_sha256 == await self._get_local_hash(file_path, "sha256", hash_cache):
                item["cloud_path"] = self.format_item_path(item)
                matches.append(item)
            elif api_sha1 and api_sha1 == await self._get_local_hash(file_path, "sha1", hash_cache):
                item["cloud_path"] = self.format_item_path(item)
                matches.append(item)

        return matches

    async def _get_local_hash(
        self,
        file_path: str,
        hash_type: str,
        hash_cache: dict[str, str],
    ) -> str:
        if hash_type not in hash_cache:
            hash_cache[hash_type] = await asyncio.to_thread(self.compute_hash, file_path, hash_type)
        return hash_cache[hash_type]

    async def list_children(
        self,
        item_id: str,
        *,
        folders_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return all direct children of a drive item with pagination support."""
        select = "id,name,size,folder,file,parentReference,webUrl"
        if item_id == "root":
            url = f"{GRAPH_BASE}/me/drive/root/children?$top={PAGE_SIZE}&$select={select}"
        else:
            url = (
                f"{GRAPH_BASE}/me/drive/items/{item_id}/children"
                f"?$top={PAGE_SIZE}&$select={select}"
            )

        items: list[dict[str, Any]] = []
        while url:
            page = await self.get_url(url)
            for item in page.get("value", []):
                if folders_only and "folder" not in item:
                    continue
                items.append(item)
            url = page.get("@odata.nextLink", "")
        return items

    async def get_child_by_name(
        self,
        parent_id: str,
        name: str,
    ) -> Optional[dict[str, Any]]:
        """Return the direct child of a parent folder by name."""
        for item in await self.list_children(parent_id):
            if item.get("name", "").casefold() == name.casefold():
                return item
        return None

    async def create_folder(self, parent_id: str, name: str) -> dict[str, Any]:
        """Create a folder below a given parent folder."""
        try:
            return await self.post(
                f"/me/drive/items/{parent_id}/children",
                {
                    "name": name,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "fail",
                },
            )
        except GraphRequestError as exc:
            if exc.status != 409:
                raise
            existing = await self.get_child_by_name(parent_id, name)
            if existing is None:
                raise
            return existing

    async def ensure_child_folder(self, parent_id: str, name: str) -> dict[str, Any]:
        """Get or create a child folder below a given parent folder."""
        existing = await self.get_child_by_name(parent_id, name)
        if existing is not None and "folder" in existing:
            return existing
        return await self.create_folder(parent_id, name)

    async def ensure_remote_folder(self, remote_path: str) -> dict[str, Any]:
        """Resolve or create a OneDrive folder path."""
        normalized_path = self.normalize_remote_path(remote_path)
        if normalized_path == "/":
            root_item = await self.get_item_by_path("/")
            if root_item is None:
                raise RuntimeError("Unable to resolve OneDrive root folder.")
            return root_item

        current = await self.get_item_by_path("/")
        if current is None:
            raise RuntimeError("Unable to resolve OneDrive root folder.")

        for part in normalized_path.strip("/").split("/"):
            current = await self.ensure_child_folder(current["id"], part)
        return current

    async def enumerate_media(
        self,
        folder_id: str,
        folder_path: str = "/",
        counters: Optional[dict[str, int]] = None,
        semaphore: Optional[asyncio.Semaphore] = None,
    ) -> list[dict[str, Any]]:
        """Recursively enumerate media items beneath a OneDrive folder."""
        media_items, counts = await self._enumerate_media(
            folder_id,
            folder_path=folder_path,
            semaphore=semaphore,
        )
        if counters is not None:
            counters["folders"] = counters.get("folders", 0) + counts["folders"]
            counters["files"] = counters.get("files", 0) + counts["files"]
        return media_items

    async def _enumerate_media(  # pylint: disable=too-many-locals
        self,
        folder_id: str,
        *,
        folder_path: str,
        semaphore: Optional[asyncio.Semaphore] = None,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        current_semaphore = semaphore or asyncio.Semaphore(FOLDER_CONCURRENCY)
        async with current_semaphore:
            children = await self.list_children(folder_id)

        media_items: list[dict[str, Any]] = []
        counts = {"folders": 1, "files": 0}
        sub_tasks: list[asyncio.Task[tuple[list[dict[str, Any]], dict[str, int]]]] = []
        for item in children:
            if "folder" in item:
                child_path = folder_path.rstrip("/") + "/" + item["name"]
                sub_tasks.append(
                    asyncio.create_task(
                        self._enumerate_media(
                            item["id"],
                            folder_path=child_path,
                            semaphore=current_semaphore,
                        )
                    )
                )
                continue

            mime_type = item.get("file", {}).get("mimeType", "") or ""
            name_lower = item.get("name", "").lower()
            is_media = any(mime_type.startswith(prefix) for prefix in MEDIA_MIME_PREFIXES)
            is_allowlisted = any(name_lower.endswith(ext) for ext in MEDIA_EXTENSION_ALLOWLIST)
            if is_media or is_allowlisted:
                item["cloud_path"] = self.format_item_path(item)
                media_items.append(item)
                counts["files"] += 1

        if not sub_tasks:
            return media_items, counts

        results = await asyncio.gather(*sub_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                log.error(
                    "Failed to enumerate media beneath '%s': %s",
                    folder_path,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )
                continue
            child_items, child_counts = result
            media_items.extend(child_items)
            counts["folders"] += child_counts["folders"]
            counts["files"] += child_counts["files"]
        return media_items, counts

    async def browse_for_folder(self) -> Optional[dict[str, str]]:
        """Interactively browse the OneDrive folder tree and select a folder."""
        id_stack: list[str] = []
        name_stack: list[str] = []
        current_id = "root"
        divider = "─" * 64
        print(f"\n{divider}")
        print("  OneDrive Folder Browser")
        print("  Navigate to the source folder, then press [S] to select it.")
        print(divider)

        while True:
            display_path = "/" + "/".join(name_stack) if name_stack else "/"
            sub_folders = await self.list_children(current_id, folders_only=True)
            sub_folders.sort(key=lambda item: item["name"].casefold())

            print(f"\n  Location : {display_path}")
            print(f"  Sub-folders ({len(sub_folders)}):\n")
            for index, folder in enumerate(sub_folders, start=1):
                print(f"    [{index:>4}]  {folder['name']}/")
            print()
            print("    [S]   Select this folder")
            if id_stack:
                print("    [B]   Go back")
            print("    [Q]   Quit")
            print()

            raw_choice = input("  Enter choice: ").strip()
            choice = raw_choice.upper()
            if choice == "Q":
                return None
            if choice == "S":
                folder_path = "/" + "/".join(name_stack) if name_stack else "/"
                folder_name = name_stack[-1] if name_stack else "OneDrive Root"
                return {"id": current_id, "name": folder_name, "path": folder_path}
            if choice == "B" and id_stack:
                current_id = id_stack.pop()
                name_stack.pop()
                continue
            if raw_choice.isdigit():
                selected_index = int(raw_choice) - 1
                if 0 <= selected_index < len(sub_folders):
                    id_stack.append(current_id)
                    current_id = sub_folders[selected_index]["id"]
                    name_stack.append(sub_folders[selected_index]["name"])
                    continue
            print("  ! Unrecognised input — try again.")

    async def create_album(self, name: str) -> dict[str, Any]:
        """Create an empty OneDrive album bundle."""
        return await self.post(
            "/me/drive/bundles",
            {
                "name": name,
                "@microsoft.graph.conflictBehavior": "rename",
                "bundle": {"album": {}},
            },
        )

    async def list_albums(self) -> list[dict[str, str]]:
        """Return existing OneDrive album bundles."""
        albums: list[dict[str, str]] = []
        url = f"{GRAPH_BASE}/me/drive/bundles?$top={PAGE_SIZE}&$select=id,name,bundle"
        while url:
            page = await self.get_url(url)
            for item in page.get("value", []):
                bundle = item.get("bundle") or {}
                odata_type = str(bundle.get("@odata.type", "")).lower()
                if isinstance(bundle.get("album"), dict) or odata_type.endswith(".album"):
                    albums.append({"id": item["id"], "name": item.get("name", "Unnamed Album")})
            url = page.get("@odata.nextLink", "")
        albums.sort(key=lambda item: item["name"].casefold())
        return albums

    async def choose_existing_album(self) -> Optional[dict[str, str]]:
        """Interactively choose an existing album."""
        albums = await self.list_albums()
        if not albums:
            print("\nNo existing OneDrive albums were found.")
            return None

        divider = "─" * 64
        print(f"\n{divider}")
        print("  Existing OneDrive Albums")
        print(divider)
        for index, album in enumerate(albums, start=1):
            print(f"    [{index:>4}]  {album['name']}  ({album['id']})")
        print("\n    [Q]   Cancel\n")

        while True:
            raw_choice = input("  Pick album number: ").strip()
            if raw_choice.upper() == "Q":
                return None
            if raw_choice.isdigit():
                selected_index = int(raw_choice) - 1
                if 0 <= selected_index < len(albums):
                    return albums[selected_index]
            print("  ! Unrecognised input — try again.")

    async def post_batch(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        """Submit a Graph batch request."""
        if len(requests) > BATCH_LIMIT:
            raise ValueError(f"Batch size exceeds Graph limit of {BATCH_LIMIT}.")
        return await self.post("/$batch", {"requests": requests})

    async def get_matching_remote_file(
        self,
        parent_id: str,
        local_file_path: str,
        remote_name: str,
    ) -> Optional[dict[str, Any]]:
        """Return a remote file that exactly matches a local file by name, size, and hash."""
        candidate = await self.get_child_by_name(parent_id, remote_name)
        if candidate is None or "file" not in candidate:
            return None
        detailed_item = await self.get_item(candidate["id"])
        if detailed_item.get("size") != os.path.getsize(local_file_path):
            return None

        hashes = detailed_item.get("file", {}).get("hashes", {})
        api_sha256 = hashes.get("sha256Hash", "").lower()
        api_sha1 = hashes.get("sha1Hash", "").lower()
        if api_sha256:
            if api_sha256 != self.compute_hash(local_file_path, "sha256"):
                return None
        elif api_sha1:
            if api_sha1 != self.compute_hash(local_file_path, "sha1"):
                return None
        else:
            return None

        detailed_item["cloud_path"] = self.format_item_path(detailed_item)
        return detailed_item

    async def upload_file(
        self,
        local_file_path: str,
        remote_parent_id: str,
        file_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Upload a local file to OneDrive, skipping matching remote files."""
        remote_name = file_name or Path(local_file_path).name
        existing = await self.get_matching_remote_file(
            remote_parent_id,
            local_file_path,
            remote_name,
        )
        if existing is not None:
            return {"status": "skipped", "item": existing}

        file_size = os.path.getsize(local_file_path)
        if file_size < SMALL_FILE_UPLOAD_BYTES:
            uploaded_item = await self._simple_upload(local_file_path, remote_parent_id, remote_name)
        else:
            uploaded_item = await self._upload_large_file(local_file_path, remote_parent_id, remote_name)
        uploaded_item["cloud_path"] = self.format_item_path(uploaded_item)
        return {"status": "uploaded", "item": uploaded_item}

    async def _simple_upload(
        self,
        local_file_path: str,
        remote_parent_id: str,
        remote_name: str,
    ) -> dict[str, Any]:
        with open(local_file_path, "rb") as file_handle:
            payload = file_handle.read()
        encoded_name = quote(remote_name, safe="")
        return await self.put_bytes(
            f"{GRAPH_BASE}/me/drive/items/{remote_parent_id}:/{encoded_name}:/content",
            payload,
            headers={"Content-Type": "application/octet-stream"},
        )

    async def _upload_large_file(
        self,
        local_file_path: str,
        remote_parent_id: str,
        remote_name: str,
    ) -> dict[str, Any]:
        encoded_name = quote(remote_name, safe="")
        upload_session = await self.post(
            f"/me/drive/items/{remote_parent_id}:/{encoded_name}:/createUploadSession",
            {"item": {"@microsoft.graph.conflictBehavior": "replace", "name": remote_name}},
        )
        upload_url = upload_session["uploadUrl"]
        file_size = os.path.getsize(local_file_path)
        uploaded_item: dict[str, Any] = {}

        with open(local_file_path, "rb") as file_handle:
            start = 0
            while start < file_size:
                chunk = file_handle.read(UPLOAD_CHUNK_SIZE)
                end = start + len(chunk) - 1
                response = await self.put_bytes(
                    upload_url,
                    chunk,
                    auth=False,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                    },
                )
                if "id" in response:
                    uploaded_item = response
                start = end + 1
        return uploaded_item
