#!/usr/bin/env python3
"""
OneDrive Album Creator
======================
Creates a OneDrive photo album from a selected OneDrive folder,
recursively adding every photo and video found within it.

IMPORTANT NOTES
---------------
• Works with **personal OneDrive** (Microsoft/consumer accounts) only.
  OneDrive for Business / SharePoint does not support the bundle/album API.

• Requires an Azure AD app registration:
    1. Go to https://portal.azure.com → Azure Active Directory → App registrations
    2. Register a new app (any name).
    3. Under "Authentication" → "Add a platform" → "Mobile and desktop
       applications" → tick the http://localhost redirect URI.
    4. Under "API permissions" → "Add a permission" → Microsoft Graph →
       Delegated → add:  Files.ReadWrite  and  User.Read
    5. Copy the Application (client) ID.

• Set environment variables:
    CLIENT_ID   Application (client) ID from the portal
    TENANT_ID   "consumers" for personal Microsoft accounts  (default)

Usage
-----
    python create_album.py              # interactive run
    python create_album.py --dry-run    # scan and report, no changes
    python create_album.py --resume .album_state_<id>.json
    python create_album.py --album-id <existing_album_id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from azure.identity import (
    AuthenticationRecord,
    InteractiveBrowserCredential,
    TokenCachePersistenceOptions,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Microsoft Graph $batch ceiling — do NOT exceed 20
BATCH_LIMIT = 20

# Max items returned per page (Graph ceiling is 200)
PAGE_SIZE = 200

# HTTP retry settings
RETRY_MAX = 6
RETRY_CAP = 120  # seconds

# Max concurrent folder-listing calls (to respect rate limits while still
# being fast for large libraries with many sub-folders)
FOLDER_CONCURRENCY = 8

# MIME-type prefixes that qualify an item as a photo or video
MEDIA_MIME_PREFIXES = ("image/", "video/")

# Explicit extension allow-list for media files that may have missing or
# inconsistent MIME types from Graph metadata.
MEDIA_EXTENSION_ALLOWLIST = (".mts",)

SCOPES = ["Files.ReadWrite", "Files.Read.All"]

# Files used to persist login state across runs.
AUTH_RECORD_FILE = ".graph_auth_record.json"
TOKEN_CACHE_NAME = "onedrive_album_creator"

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("album_creator.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Async Graph HTTP client
# ─────────────────────────────────────────────────────────────────────────────

class GraphSession:
    """
    Minimal async Microsoft Graph REST client.

    Features
    --------
    • Automatic token acquisition and refresh via Azure Identity.
    • Retry with capped exponential back-off on 429 (rate-limited) and
      5xx server errors.
    • Respects the ``Retry-After`` response header.
    • Connection pool capped at ``FOLDER_CONCURRENCY`` to avoid flooding
      the API.
    """

    def __init__(self, credential: InteractiveBrowserCredential) -> None:
        self._cred = credential
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "GraphSession":
        connector = aiohttp.TCPConnector(limit=FOLDER_CONCURRENCY + 4)
        self._session = aiohttp.ClientSession(connector=connector)
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()

    # ── token ──────────────────────────────────────────────────────────────
    def _auth_headers(self) -> dict:
        token = self._cred.get_token(*SCOPES)
        return {
            "Authorization": f"Bearer {token.token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

    # ── core request with retry ────────────────────────────────────────────
    async def _request(self, method: str, url: str, **kwargs) -> dict:
        delay = 1
        for attempt in range(1, RETRY_MAX + 1):
            try:
                async with self._session.request(
                    method, url, headers=self._auth_headers(), **kwargs
                ) as resp:
                    if resp.status == 429:
                        wait = int(resp.headers.get("Retry-After", delay))
                        log.warning(
                            "Rate-limited (429). Waiting %ds [attempt %d/%d].",
                            wait, attempt, RETRY_MAX,
                        )
                        await asyncio.sleep(wait)
                        delay = min(delay * 2, RETRY_CAP)
                        continue

                    if resp.status in (500, 502, 503, 504):
                        log.warning(
                            "Server error %d. Retrying in %ds [attempt %d/%d].",
                            resp.status, delay, attempt, RETRY_MAX,
                        )
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, RETRY_CAP)
                        continue

                    # 204 No Content is a legitimate success with no body
                    if resp.status == 204:
                        return {}

                    body = await resp.json(content_type=None)

                    if resp.status >= 400:
                        err_msg = (
                            body.get("error", {}).get("message", "")
                            if isinstance(body, dict)
                            else str(body)
                        )
                        raise RuntimeError(
                            f"HTTP {resp.status} [{method}] {url}: {err_msg}"
                        )

                    return body

            except aiohttp.ClientConnectionError as exc:
                log.warning(
                    "Connection error [attempt %d/%d]: %s", attempt, RETRY_MAX, exc
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, RETRY_CAP)

        raise RuntimeError(f"All {RETRY_MAX} retries exhausted for {method} {url}")

    # ── public API ─────────────────────────────────────────────────────────
    async def get_url(self, url: str) -> dict:
        return await self._request("GET", url)

    async def post(self, path: str, body: dict) -> dict:
        return await self._request("POST", f"{GRAPH_BASE}{path}", json=body)


def _create_persistent_credential(client_id: str, tenant_id: str) -> InteractiveBrowserCredential:
    """
    Create an InteractiveBrowserCredential backed by a persistent token cache,
    then restore/save an authentication record so interactive login is reused
    across runs whenever possible.
    """
    cache_opts = TokenCachePersistenceOptions(
        name=TOKEN_CACHE_NAME,
        allow_unencrypted_storage=False,
    )

    auth_record: Optional[AuthenticationRecord] = None
    if os.path.exists(AUTH_RECORD_FILE):
        try:
            with open(AUTH_RECORD_FILE, encoding="utf-8") as fh:
                auth_record = AuthenticationRecord.deserialize(fh.read())
            log.info("Loaded persisted authentication record from %s.", AUTH_RECORD_FILE)
        except Exception as exc:
            log.warning("Could not load auth record from %s: %s", AUTH_RECORD_FILE, exc)

    credential = InteractiveBrowserCredential(
        client_id=client_id,
        tenant_id=tenant_id,
        cache_persistence_options=cache_opts,
        authentication_record=auth_record,
    )

    # If there is no record, do one interactive auth once and persist it.
    if auth_record is None:
        try:
            record = credential.authenticate(scopes=SCOPES)
            with open(AUTH_RECORD_FILE, "w", encoding="utf-8") as fh:
                fh.write(record.serialize())
            log.info("Saved authentication record to %s.", AUTH_RECORD_FILE)
        except Exception as exc:
            log.warning(
                "Initial authentication record save failed (%s). "
                "Token cache may still preserve session.",
                exc,
            )

    return credential


# ─────────────────────────────────────────────────────────────────────────────
# OneDrive folder traversal
# ─────────────────────────────────────────────────────────────────────────────

async def list_children(
    session: GraphSession,
    item_id: str,
    folders_only: bool = False,
) -> list[dict]:
    """
    Return *all* direct children of a drive item, handling OData pagination.

    Parameters
    ----------
    session:      Authenticated GraphSession.
    item_id:      Drive item ID.  Pass ``"root"`` for the My Drive root.
    folders_only: When True, only folder items are returned.
    """
    items: list[dict] = []
    select = "id,name,folder,file"
    url = (
        f"{GRAPH_BASE}/me/drive/items/{item_id}/children"
        f"?$top={PAGE_SIZE}&$select={select}"
    )

    while url:
        page = await session.get_url(url)
        for item in page.get("value", []):
            if folders_only and "folder" not in item:
                continue
            items.append(item)
        # @odata.nextLink is absent on the last page
        url = page.get("@odata.nextLink")

    return items


async def enumerate_media(
    session: GraphSession,
    folder_id: str,
    folder_path: str = "/",
    _counters: Optional[dict] = None,
    _semaphore: Optional[asyncio.Semaphore] = None,
) -> list[dict]:
    """
    Recursively enumerate every photo/video item under *folder_id*.

    Sub-folder listings are executed concurrently (bounded by a semaphore)
    for fast traversal of large libraries.

    Returns a list of ``{"id": str, "name": str, "path": str}`` dicts
    representing every qualifying drive item found.
    """
    if _counters is None:
        _counters = {"folders": 0, "files": 0}
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(FOLDER_CONCURRENCY)

    _counters["folders"] += 1
    log.info(
        "Scanning: %-60s  (%d files found so far)",
        folder_path,
        _counters["files"],
    )

    async with _semaphore:
        children = await list_children(session, folder_id, folders_only=False)

    media: list[dict] = []
    sub_tasks: list = []

    for item in children:
        if "folder" in item:
            sub_path = folder_path.rstrip("/") + "/" + item["name"]
            sub_tasks.append(
                enumerate_media(session, item["id"], sub_path, _counters, _semaphore)
            )

        elif "file" in item:
            mime = item.get("file", {}).get("mimeType", "") or ""
            name_lower = item.get("name", "").lower()
            is_mime_media = any(mime.startswith(p) for p in MEDIA_MIME_PREFIXES)
            is_allowlisted_ext = any(name_lower.endswith(ext) for ext in MEDIA_EXTENSION_ALLOWLIST)

            if is_mime_media or is_allowlisted_ext:
                media.append(
                    {
                        "id":   item["id"],
                        "name": item["name"],
                        "path": folder_path.rstrip("/") + "/" + item["name"],
                    }
                )
                _counters["files"] += 1

    if sub_tasks:
        results = await asyncio.gather(*sub_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                log.error("Failed to enumerate a sub-folder: %s", result)
            else:
                media.extend(result)

    return media


# ─────────────────────────────────────────────────────────────────────────────
# Interactive OneDrive folder browser
# ─────────────────────────────────────────────────────────────────────────────

async def browse_for_folder(session: GraphSession) -> Optional[dict]:
    """
    Navigate the user's OneDrive folder tree interactively from the CLI.

    Returns ``{"id": str, "name": str, "path": str}`` for the selected
    folder, or ``None`` if the user quits.
    """
    id_stack:   list[str] = []   # parent IDs   (bottom = immediate parent)
    name_stack: list[str] = []   # parent names (parallel with id_stack)
    current_id: str       = "root"

    DIV = "─" * 64
    print(f"\n{DIV}")
    print("  OneDrive Folder Browser")
    print("  Navigate to the source folder, then press [S] to select it.")
    print(DIV)

    while True:
        display_path = ("/" + "/".join(name_stack)) if name_stack else "/"

        try:
            sub_folders = await list_children(session, current_id, folders_only=True)
        except RuntimeError as exc:
            log.error("Could not list folder: %s", exc)
            return None

        sub_folders.sort(key=lambda x: x["name"].casefold())

        print(f"\n  Location : {display_path}")
        print(f"  Sub-folders ({len(sub_folders)}):\n")
        for idx, folder in enumerate(sub_folders, 1):
            print(f"    [{idx:>4}]  {folder['name']}/")

        print()
        print("    [S]   Select this folder")
        if id_stack:
            print("    [B]   Go back")
        print("    [Q]   Quit")
        print()

        raw    = input("  Enter choice: ").strip()
        choice = raw.upper()

        if choice == "Q":
            print("\nAborted.")
            return None

        if choice == "S":
            path_str = ("/" + "/".join(name_stack)) if name_stack else "/"
            return {
                "id":   current_id,
                "name": name_stack[-1] if name_stack else "OneDrive Root",
                "path": path_str,
            }

        if choice == "B" and id_stack:
            current_id = id_stack.pop()
            name_stack.pop()
            continue

        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(sub_folders):
                id_stack.append(current_id)
                current_id = sub_folders[idx]["id"]
                name_stack.append(sub_folders[idx]["name"])
            else:
                print("  ! Number out of range — try again.")
        else:
            print("  ! Unrecognised input — try again.")


# ─────────────────────────────────────────────────────────────────────────────
# Album (bundle) creation
# ─────────────────────────────────────────────────────────────────────────────

async def create_album(session: GraphSession, name: str) -> dict:
    """
    Create an empty OneDrive photo-album bundle and return the drive item.

    If the name already exists OneDrive automatically appends a number
    (controlled by ``"@microsoft.graph.conflictBehavior": "rename"``).
    """
    log.info("Creating album: '%s'", name)
    body = {
        "name": name,
        "@microsoft.graph.conflictBehavior": "rename",
        "bundle": {"album": {}},
    }
    album = await session.post("/me/drive/bundles", body)
    log.info("Album created — id=%s  name='%s'", album["id"], album["name"])
    return album


async def list_albums(session: GraphSession) -> list[dict]:
    """Return all existing OneDrive photo albums (bundle.album)."""
    albums: list[dict] = []
    url = f"{GRAPH_BASE}/me/drive/bundles?$top={PAGE_SIZE}&$select=id,name,bundle"
    bundles_seen = 0

    while url:
        page = await session.get_url(url)
        for item in page.get("value", []):
            bundles_seen += 1
            bundle = item.get("bundle") or {}
            odata_type = str(bundle.get("@odata.type", "")).lower()
            has_album_facet = isinstance(bundle.get("album"), dict)
            is_album_type = odata_type.endswith(".album")

            if has_album_facet or is_album_type:
                albums.append({"id": item["id"], "name": item.get("name", "(Unnamed Album)")})
        url = page.get("@odata.nextLink")

    albums.sort(key=lambda x: x["name"].casefold())
    log.info("Album discovery: %d bundle item(s) scanned, %d album(s) detected.", bundles_seen, len(albums))
    if bundles_seen > 0 and not albums:
        log.warning(
            "No album bundles were identifiable from /me/drive/bundles. "
            "This is usually an API visibility/model issue for UI-created albums, "
            "not a missing permission when no 403/401 error was returned."
        )
    return albums


async def list_bundle_candidates(session: GraphSession) -> list[dict]:
    """Return all bundles as fallback candidates when album facets are absent."""
    bundles: list[dict] = []
    url = f"{GRAPH_BASE}/me/drive/bundles?$top={PAGE_SIZE}&$select=id,name,bundle"

    while url:
        page = await session.get_url(url)
        for item in page.get("value", []):
            bundle = item.get("bundle") or {}
            bundles.append(
                {
                    "id": item.get("id", ""),
                    "name": item.get("name", "(Unnamed Bundle)"),
                    "odata_type": str(bundle.get("@odata.type", "")),
                }
            )
        url = page.get("@odata.nextLink")

    bundles.sort(key=lambda x: x["name"].casefold())
    return bundles


async def choose_existing_album(session: GraphSession) -> Optional[dict]:
    """
    Let the user choose an existing OneDrive album.

    Returns {"id": str, "name": str} or None if user cancels.
    """
    albums = await list_albums(session)
    if not albums:
        bundles = await list_bundle_candidates(session)
        if not bundles:
            print("\nNo existing OneDrive albums or bundle candidates were found.")
            return None

        DIV = "─" * 64
        print(f"\n{DIV}")
        print("  Existing Bundle Candidates (fallback)")
        print("  Graph did not expose album facets; choose a bundle or paste album ID.")
        print(DIV)
        for idx, item in enumerate(bundles, 1):
            type_hint = item.get("odata_type") or "unknown"
            print(f"    [{idx:>4}]  {item['name']}  ({item['id']})  type={type_hint}")
        print()
        print("    [M]   Manually enter an album ID")
        print("    [Q]   Cancel")
        print()

        while True:
            raw = input("  Pick bundle number / [M]/[Q]: ").strip()
            choice = raw.upper()

            if choice == "Q":
                return None

            if choice == "M":
                manual_id = input("  Enter album (bundle) ID: ").strip()
                if manual_id:
                    return {"id": manual_id, "name": "Manual Album ID"}
                print("  ! Album ID cannot be empty.")
                continue

            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(bundles):
                    chosen = bundles[idx]
                    return {"id": chosen["id"], "name": chosen["name"]}
                print("  ! Number out of range — try again.")
                continue

            print("  ! Unrecognised input — try again.")

    DIV = "─" * 64
    print(f"\n{DIV}")
    print("  Existing OneDrive Albums")
    print(DIV)
    for idx, album in enumerate(albums, 1):
        print(f"    [{idx:>4}]  {album['name']}  ({album['id']})")
    print()
    print("    [Q]   Cancel")
    print()

    while True:
        raw = input("  Pick album number: ").strip()
        if raw.upper() == "Q":
            return None
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(albums):
                return albums[idx]
            print("  ! Number out of range — try again.")
            continue
        print("  ! Unrecognised input — try again.")


# ─────────────────────────────────────────────────────────────────────────────
# Batched item addition
# ─────────────────────────────────────────────────────────────────────────────

async def add_items_to_album(
    session: GraphSession,
    bundle_id: str,
    item_ids: list[str],
    already_added: set[str],
    state_path: str,
    source_folder_id: Optional[str],
    source_folder_path: Optional[str],
) -> tuple[int, int]:
    """
    Add *item_ids* to the album identified by *bundle_id*.

    Batching strategy
    -----------------
    Up to ``BATCH_LIMIT`` (20) add-to-bundle requests are combined into a
    single Microsoft Graph ``$batch`` call, reducing network round-trips and
    staying well within the service's rate limits.

    Resilience
    ----------
    • Items already present in *already_added* are skipped (resume support).
    • A 409 Conflict response means the item is already in the album; we
      count it as a success and record it to avoid retrying.
    • Progress is atomically persisted to *state_path* after every batch,
      so an interrupted run can be resumed with ``--resume``.

    Returns ``(success_count, failure_count)``.
    """
    pending = [iid for iid in item_ids if iid not in already_added]
    total   = len(pending)

    log.info(
        "Items to add in this run: %d  (pre-existing / already added: %d).",
        total, len(already_added),
    )

    if total == 0:
        log.info("Nothing to do — all discovered items are already in the album.")
        return 0, 0

    success = 0
    failure = 0

    for batch_start in range(0, total, BATCH_LIMIT):
        chunk = pending[batch_start : batch_start + BATCH_LIMIT]

        batch_requests = [
            {
                "id":     str(i),
                "method": "POST",
                "url":    f"/me/drive/bundles/{bundle_id}/children",
                "headers": {"Content-Type": "application/json"},
                "body":   {"id": item_id},
            }
            for i, item_id in enumerate(chunk)
        ]

        try:
            result    = await session.post("/$batch", {"requests": batch_requests})
            responses = result.get("responses", [])
        except RuntimeError as exc:
            log.error(
                "$batch call failed (%s). %d items in this chunk will be "
                "retried if you run with --resume.",
                exc, len(chunk),
            )
            failure += len(chunk)
            # Still persist the progress made so far
            _save_state(
                state_path,
                bundle_id,
                already_added,
                source_folder_id,
                source_folder_path,
            )
            continue

        for resp in responses:
            req_idx = int(resp.get("id", -1))
            if req_idx < 0 or req_idx >= len(chunk):
                continue

            status  = resp.get("status", 0)
            item_id = chunk[req_idx]

            if status in (200, 201, 204):
                already_added.add(item_id)
                success += 1

            elif status == 409:
                # Item is already in the album (not a failure)
                log.debug("Item %s already in album (409). Recorded as success.", item_id)
                already_added.add(item_id)
                success += 1

            else:
                body    = resp.get("body") or {}
                err_msg = (
                    body.get("error", {}).get("message", "")
                    if isinstance(body, dict)
                    else str(body)
                )
                log.warning(
                    "Failed to add item %s — HTTP %d: %s",
                    item_id, status, err_msg,
                )
                failure += 1

        # Atomically persist progress after every batch
        _save_state(
            state_path,
            bundle_id,
            already_added,
            source_folder_id,
            source_folder_path,
        )

        done = batch_start + len(chunk)
        pct  = int(done / total * 100) if total else 100
        log.info(
            "Progress: %d / %d  (%d%%)  added=+%d  errors=+%d",
            done, total, pct, success, failure,
        )

        # Brief pause between batches to stay well within rate limits
        await asyncio.sleep(0.3)

    return success, failure


# ─────────────────────────────────────────────────────────────────────────────
# State persistence  (supports --resume)
# ─────────────────────────────────────────────────────────────────────────────

def _default_state_path(album_id: str) -> str:
    short = album_id.replace("-", "")[:12]
    return f".album_state_{short}.json"


def _save_state(
    path: str,
    album_id: str,
    added_ids: set[str],
    source_folder_id: Optional[str],
    source_folder_path: Optional[str],
) -> None:
    """
    Atomically write progress state via a write-then-rename approach so a
    crash mid-write never corrupts the state file.
    """
    tmp = path + ".tmp"
    data = {
        "album_id":   album_id,
        "added_ids":  sorted(added_ids),
        "source_folder": {
            "id": source_folder_id,
            "path": source_folder_path,
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)   # atomic on POSIX; near-atomic on Windows


def _load_state(path: str) -> tuple[Optional[str], set[str], Optional[dict]]:
    """Load and return ``(album_id, set_of_added_ids, source_folder)`` from a state file."""
    if not os.path.exists(path):
        return None, set(), None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    source_folder = data.get("source_folder")
    if not isinstance(source_folder, dict):
        source_folder = None
    return data.get("album_id"), set(data.get("added_ids", [])), source_folder


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    client_id = os.getenv("CLIENT_ID")
    tenant_id = os.getenv("TENANT_ID", "consumers")  # "consumers" → personal OneDrive

    if not client_id:
        log.error(
            "CLIENT_ID environment variable is not set.\n"
            "  Register an app at https://portal.azure.com, grant it Files.ReadWrite,\n"
            "  add the 'Mobile and desktop – http://localhost' redirect URI,\n"
            "  then export: CLIENT_ID=<your-app-id>"
        )
        sys.exit(1)

    credential = _create_persistent_credential(client_id, tenant_id)

    async with GraphSession(credential) as session:

        # ── 1. Resume detection (done first so folder selection can be skipped)
        resume_album_id: Optional[str] = None
        resume_source_folder: Optional[dict] = None
        already_added: set[str] = set()

        if args.resume:
            resume_album_id, already_added, resume_source_folder = _load_state(args.resume)
            if resume_album_id:
                log.info(
                    "Resuming from '%s' — album_id=%s, %d items already recorded.",
                    args.resume, resume_album_id, len(already_added),
                )
            else:
                log.warning(
                    "State file '%s' present but has no album_id — starting fresh.",
                    args.resume,
                )

        # ── 2. Source folder selection (or restore from resume state)
        folder: Optional[dict] = None
        if resume_source_folder and resume_source_folder.get("id"):
            folder = {
                "id": resume_source_folder.get("id"),
                "path": resume_source_folder.get("path") or "(resumed folder)",
                "name": (resume_source_folder.get("path") or "Resumed Folder").rstrip("/").split("/")[-1] or "Resumed Folder",
            }
            log.info("Using source folder from resume state: %s (id=%s)", folder["path"], folder["id"])
        else:
            folder = await browse_for_folder(session)
            if folder is None:
                sys.exit(0)
            log.info("Selected folder: %s  (id=%s)", folder["path"], folder["id"])

            if args.resume and resume_album_id:
                log.info(
                    "Resume state did not include source folder metadata (older state format), "
                    "so folder selection was required once. Future retries will skip this prompt."
                )
                _save_state(
                    args.resume,
                    resume_album_id,
                    already_added,
                    folder.get("id"),
                    folder.get("path"),
                )
                log.info("Updated resume state with source folder metadata: %s", args.resume)

        # ── 3. Album name ─────────────────────────────────────────────────────
        default_name = f"{folder['name']} Album"
        album_name   = default_name

        # ── 4. Album mode ─────────────────────────────────────────────────────
        selected_album:  Optional[dict] = None
        if resume_album_id:
            selected_album = {"id": resume_album_id, "name": "Resumed Album"}

        if args.album_id and selected_album is None:
            selected_album = {"id": args.album_id, "name": "Existing Album"}
            log.info("Using existing album id from --album-id: %s", args.album_id)

        if selected_album is None:
            print("\nAlbum target:")
            print("  [1] Create a new album")
            print("  [2] Add to an existing album")
            mode = input("Choose [1/2] (default 1): ").strip() or "1"

            if mode == "2":
                selected_album = await choose_existing_album(session)
                if selected_album is None:
                    print("No album selected. Aborted.")
                    sys.exit(0)
                album_name = selected_album["name"]
            else:
                album_name = input(f"\nAlbum name [{default_name}]: ").strip() or default_name

        # ── 5. Recursive media enumeration ────────────────────────────────────
        print("\nScanning OneDrive folder for photos and videos…\n")
        counters: dict = {"folders": 0, "files": 0}
        media_items = await enumerate_media(
            session, folder["id"], folder["path"], counters
        )

        log.info(
            "Scan complete — %d folder(s) visited, %d media file(s) found.",
            counters["folders"],
            counters["files"],
        )

        if not media_items:
            log.warning("No photos or videos found in '%s'. Nothing to do.", folder["path"])
            sys.exit(0)

        # ── 6. Dry-run short-circuit ──────────────────────────────────────────
        if args.dry_run:
            target_mode = "existing album" if selected_album else "new album"
            log.info(
                "[DRY RUN] Would use %s '%s' and add %d item(s). "
                "No changes have been written.",
                target_mode, album_name, len(media_items),
            )
            sys.exit(0)

        # ── 7. Confirm before writing ─────────────────────────────────────────
        action_text = "add to existing album" if selected_album else "create album"
        print(f"\n  Ready to {action_text} '{album_name}' with {len(media_items)} item(s).")
        confirm = input("  Proceed? [y/N] ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

        # ── 8. Create or reuse album ──────────────────────────────────────────
        if selected_album:
            album_id = selected_album["id"]
            album_name = selected_album.get("name", album_name)
            log.info("Reusing existing album id=%s.", album_id)
        else:
            album    = await create_album(session, album_name)
            album_id = album["id"]
            # Use the actual name returned (may differ if there was a conflict)
            album_name = album.get("name", album_name)

        state_path = args.resume or _default_state_path(album_id)

        # ── 9. Add items (batched, with state persistence) ────────────────────
        pre_existing  = len(already_added)
        item_ids      = [item["id"] for item in media_items]

        success, failure = await add_items_to_album(
            session,
            album_id,
            item_ids,
            already_added,
            state_path,
            folder.get("id"),
            folder.get("path"),
        )

        # ── 10. Final summary ─────────────────────────────────────────────────
        DIV = "═" * 64
        print(f"\n{DIV}")
        print(f"  Album:              {album_name}")
        print(f"  Album ID:           {album_id}")
        print(f"  Total discovered:   {len(item_ids)}")
        print(f"  Pre-existing skip:  {pre_existing}")
        print(f"  Added this run:     {success}")
        print(f"  Failed:             {failure}")
        print(DIV)

        if failure == 0:
            log.info("All items added successfully.")
            if os.path.exists(state_path):
                os.remove(state_path)
                log.info("State file removed.")
        else:
            log.warning(
                "%d item(s) failed. Re-run with:  --resume '%s'",
                failure, state_path,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create a OneDrive photo album from an existing OneDrive folder,\n"
            "recursively adding all photos and videos found within it.\n\n"
            "NOTE: Only works with personal OneDrive (Microsoft consumer accounts).\n"
            "      Set CLIENT_ID (and optionally TENANT_ID) before running."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report without creating or modifying any album.",
    )
    parser.add_argument(
        "--resume",
        metavar="STATE_FILE",
        help=(
            "Path to a .album_state_*.json file from a previous interrupted run.\n"
            "The script will skip already-added items and continue from where "
            "it left off."
        ),
    )
    parser.add_argument(
        "--album-id",
        metavar="ALBUM_ID",
        help=(
            "Add files to an existing OneDrive album id instead of creating a new one.\n"
            "If used together with --resume, the album id from --resume takes precedence."
        ),
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
