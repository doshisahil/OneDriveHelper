"""Unified CLI entry point for OneDrive helper workflows."""

from __future__ import annotations

import argparse
import asyncio

from onedrive_helper.auth import get_credential
from onedrive_helper.config import setup_logging
from onedrive_helper.graph_client import GraphClient
from onedrive_helper.output import export_json, print_result
from onedrive_helper.services.album_creator import AlbumCreatorService
from onedrive_helper.services.disk_cleanup import DiskCleanupService
from onedrive_helper.services.folder_upload import FolderUploadService
from onedrive_helper.services.sync_scanner import SyncScannerService

setup_logging()


async def _run_cleanup(args: argparse.Namespace) -> object:
    async with GraphClient(get_credential()) as graph_client:
        service = DiskCleanupService(graph_client)
        return await service.run(args.local_path, args.backup_path)


async def _resolve_album_source(graph_client: GraphClient, args: argparse.Namespace) -> dict[str, str]:
    if args.source_folder_id:
        return {
            "id": args.source_folder_id,
            "name": args.source_folder_name or args.source_folder_path.rstrip("/").split("/")[-1] or "Folder",
            "path": args.source_folder_path,
        }
    selected = await graph_client.browse_for_folder()
    if selected is None:
        raise SystemExit(0)
    return selected


async def _resolve_album_target(graph_client: GraphClient, args: argparse.Namespace) -> tuple[str | None, str | None]:
    if args.album_id:
        return args.album_id, args.album_name
    if args.resume:
        return None, args.album_name
    print("\nAlbum target:")
    print("  [1] Create a new album")
    print("  [2] Add to an existing album")
    mode = input("Choose [1/2] (default 1): ").strip() or "1"
    if mode == "2":
        selected = await graph_client.choose_existing_album()
        if selected is None:
            raise SystemExit(0)
        return selected["id"], selected["name"]
    return None, args.album_name


async def _run_album(args: argparse.Namespace) -> object:
    async with GraphClient(get_credential()) as graph_client:
        source_folder = await _resolve_album_source(graph_client, args) if not args.resume else None
        album_id, album_name = await _resolve_album_target(graph_client, args)
        service = AlbumCreatorService(graph_client)
        if not args.dry_run and not args.yes:
            print(f"\nReady to process source folder '{source_folder['path'] if source_folder else 'resume state'}'.")
            if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                raise SystemExit(0)
        return await service.run(
            source_folder,
            album_name=album_name,
            album_id=album_id,
            dry_run=args.dry_run,
            resume_path=args.resume,
        )


async def _run_upload(args: argparse.Namespace) -> object:
    async with GraphClient(get_credential()) as graph_client:
        service = FolderUploadService(graph_client)
        return await service.run(args.local_path, args.remote_path)


async def _run_scan(args: argparse.Namespace) -> object:
    async with GraphClient(get_credential()) as graph_client:
        service = SyncScannerService(graph_client)
        return await service.run(args.local_path, include_all=args.all_files)


async def _dispatch(args: argparse.Namespace) -> object:
    if args.command == "cleanup":
        return await _run_cleanup(args)
    if args.command == "album":
        return await _run_album(args)
    if args.command == "upload":
        return await _run_upload(args)
    if args.command == "scan":
        return await _run_scan(args)
    raise ValueError(f"Unsupported command: {args.command}")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(description="Unified OneDrive helper CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    cleanup_parser = subparsers.add_parser("cleanup", help="Delete local files already synced to OneDrive")
    cleanup_parser.add_argument("--local-path", required=True, help="Local folder to scan")
    cleanup_parser.add_argument("--backup-path", help="Optional backup destination before deletion")
    cleanup_parser.add_argument("--output-json", help="Write the result to a JSON file")

    album_parser = subparsers.add_parser("album", help="Create or update a OneDrive album")
    album_parser.add_argument("--source-folder-id", help="Optional OneDrive folder ID")
    album_parser.add_argument("--source-folder-path", default="/", help="Display path for the source folder")
    album_parser.add_argument("--source-folder-name", help="Display name for the source folder")
    album_parser.add_argument("--album-name", help="Album name for new albums")
    album_parser.add_argument("--album-id", help="Existing album ID to reuse")
    album_parser.add_argument("--dry-run", action="store_true", help="Scan only without writing changes")
    album_parser.add_argument("--resume", help="Resume from an album state JSON file")
    album_parser.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    album_parser.add_argument("--output-json", help="Write the result to a JSON file")

    upload_parser = subparsers.add_parser("upload", help="Upload a local folder to OneDrive")
    upload_parser.add_argument("--local-path", required=True, help="Local folder to upload")
    upload_parser.add_argument("--remote-path", required=True, help="OneDrive destination path")
    upload_parser.add_argument("--output-json", help="Write the result to a JSON file")

    scan_parser = subparsers.add_parser("scan", help="Scan a local folder and report sync status")
    scan_parser.add_argument("--local-path", required=True, help="Local folder to scan")
    scan_parser.add_argument("--all-files", action="store_true", help="Scan all files instead of media only")
    scan_parser.add_argument("--output-json", help="Write the result to a JSON file")
    return parser


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()
    result = asyncio.run(_dispatch(args))
    print_result(result)
    if getattr(args, "output_json", None):
        export_json(result, args.output_json)
