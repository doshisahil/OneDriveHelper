"""Shared configuration for the OneDrive helper CLI."""

from __future__ import annotations

import logging
import sys

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
BATCH_LIMIT = 20
PAGE_SIZE = 200
RETRY_MAX = 6
RETRY_CAP = 120
FOLDER_CONCURRENCY = 8
SMALL_FILE_UPLOAD_BYTES = 4 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 10 * 320 * 1024
MEDIA_MIME_PREFIXES = ("image/", "video/")
MEDIA_EXTENSION_ALLOWLIST = (".mts",)
VALID_MEDIA_SUFFIXES = (
    ".jpg",
    ".jpeg",
    ".png",
    ".mp4",
    ".mpg",
    ".mov",
    ".mts",
    ".avi",
    ".heif",
    ".heifs",
    ".heic",
    ".heics",
    ".avci",
    ".avcs",
    ".hif",
)
SCOPES = ["User.Read", "Files.ReadWrite", "Files.Read.All"]
AUTH_RECORD_FILE = ".graph_auth_record.json"
TOKEN_CACHE_NAME = "onedrive_helper"
DEFAULT_LOG_FILE = "onedrive_helper.log"

_LOGGING_CONFIGURED = False


def setup_logging(log_file: str = DEFAULT_LOG_FILE) -> logging.Logger:
    """Configure root logging once and return the package logger."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return logging.getLogger("onedrive_helper")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    _LOGGING_CONFIGURED = True
    return logging.getLogger("onedrive_helper")
