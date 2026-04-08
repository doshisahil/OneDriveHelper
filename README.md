# OneDriveHelper

A unified Python CLI for working with personal OneDrive content from the terminal.

## Features

- Remove local media files that are already present on OneDrive to save disk space.
- Create or update a OneDrive photo album from an existing OneDrive folder.
- Upload a local folder tree to a OneDrive destination folder.
- Scan a local folder and generate sync statistics, including unsynced files.

## Requirements

- Python 3.9+
- An Azure app registration with delegated Microsoft Graph permissions:
  - `User.Read`
  - `Files.Read.All`
  - `Files.ReadWrite`
- Environment variables:
  - `CLIENT_ID`
  - `TENANT_ID` (optional, defaults to `consumers`)

## Install

```bash
pip install -r requirements.txt
```

## Usage

### Cleanup local files already synced to OneDrive

```bash
python main.py cleanup --local-path /path/to/local/folder --backup-path /optional/backup/folder
```

### Create or update a OneDrive album

```bash
python main.py album --dry-run
python main.py album --album-id <existing_album_id>
python main.py album --resume .album_state_xxxxx.json
```

If you do not provide `--source-folder-id`, the CLI opens an interactive OneDrive folder browser.

### Upload a local folder to OneDrive

```bash
python main.py upload --local-path /path/to/local/folder --remote-path /Photos/Trips
```

### Scan a local folder and export sync stats

```bash
python main.py scan --local-path /path/to/local/folder --output-json sync-report.json
```

Use `--all-files` with `scan` to include all file types instead of only media files.
