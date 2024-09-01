"""
Main Module for Processing Local Drive Files
"""
import asyncio
import os
import sys
from pathlib import Path
from tkinter import messagebox
import shutil
import logging
from datetime import datetime
import graph_api

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("process_log.log"),
        logging.StreamHandler()
    ]
)

VALID_EXTENSIONS = [
    '*.jpg', '*.jpeg', '*.png', '*.mp4', '*.mpg', "*.mov",
    "*.mp4", "*.mts", "*.avi", "*.heif", "*.heifs", "*.heic",
    "*.heics", "*.avci", "*.avcs", "*.hif"
]


async def process_file(graph_api_helper, path, local_path, backup_path):
    """
    Check if a file exists remotely and delete it if so, optionally backing it up.

    :param graph_api_helper: An instance of GraphAPI to perform searches.
    :param path: The path of the file to be checked and potentially deleted.
    :param local_path: The base local directory path.
    :param backup_path: Directory to back up files before deletion.
    """
    path_str = str(path)
    try:
        matched_files = await graph_api_helper.search_file(path.name, path_str)
        if matched_files:
            if backup_path:
                backup_file(path, local_path, backup_path)
            os.remove(path_str)
            logging.info(f"Deleted: {path_str}")
        else:
            logging.info(f"Skipped: {path_str}")
    except Exception as e:
        logging.exception(f"Skipped as EXCEPTION OCCURRED : {path_str}")
        return




def backup_file(path, local_path, backup_path):
    """
    Backup the file to the specified backup directory, preserving directory structure.

    :param path: The original file path.
    :param local_path: The base local directory path.
    :param backup_path: The directory where the file should be backed up.
    """
    relative_path = path.relative_to(local_path)
    backup_file_path = Path(backup_path) / relative_path
    backup_file_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_file_path)
    logging.info(f"Backed up: {path} to {backup_file_path}")


async def scan_directory(graph_api_helper, local_path, backup_path):
    """
    Scan the directory for files with valid extensions and process each file.

    :param graph_api_helper: An instance of GraphAPI to perform the searches.
    :param local_path: The path to the directory to scan.
    :param backup_path: Directory to back up files before deletion.
    """
    for extension in VALID_EXTENSIONS:
        async for path in get_files_by_extension(local_path, extension):
            await process_file(graph_api_helper, path, local_path, backup_path)


async def get_files_by_extension(base_path, extension):
    """
    A generator function to yield files of a specific extension within a base path,
    including both lowercase and uppercase versions of the extension.

    :param base_path: The base directory to search within.
    :param extension: The file extension to search for.
    :yield: Files matching the extension.
    """
    for path in Path(base_path).rglob(extension):
        yield path
    for path in Path(base_path).rglob(extension.upper()):
        yield path


async def main():
    """
    Main function to execute the file scanning and processing.
    """
    local_drive_path = input("Enter Drive location: ")
    backup_choice = input("Would you like to back up files before deleting? (yes/no): ").strip().lower()

    if not local_drive_path:
        messagebox.showerror("Error", "Please enter a local drive path.")
        sys.exit(1)

    if not os.path.exists(local_drive_path):
        messagebox.showerror("Error", "Local drive path does not exist.")
        sys.exit(1)

    backup_path = None
    if backup_choice == 'yes':
        backup_path = input("Enter Backup location: ")
        if not os.path.exists(backup_path):
            os.makedirs(backup_path)
            logging.info(f"Created backup directory at: {backup_path}")

    graph_api_helper = graph_api.GraphAPI()
    await scan_directory(graph_api_helper, local_drive_path, backup_path)


if __name__ == "__main__":
    asyncio.run(main())
