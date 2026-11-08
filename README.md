# Prompt Library for ComfyUI

**Version 1.1.0**

Prompt Library is an extension and custom node for [ComfyUI](https://github.com/Comfy-Org/ComfyUI) that automatically indexes prompts, parameters, and workflows from your generated media (images, video, and audio). It maintains an independent, searchable SQLite database of your generation history so your prompts and workflows remain accessible even after output files are deleted or moved.

---

## Features

- **Automatic Background Capture**: Recursively scans your ComfyUI `output/` folder in the background (configurable interval, default 30s) independent of open browser sessions.
- **Robust Metadata Extraction**: Resolves positive and negative conditioning chains across standard CLIP text encoders, multi-stage pipelines, LoRA loaders, and modern video/multimodal architectures (e.g., MiniMax H3, Krea, Flux Klein, LTX, MOSS Image).
- **Workflow Snapshots & Tab Isolation**: Saves entire canvas workflow graphs alongside extracted prompts. Open snapshots in temporary workflow tabs without altering or overwriting your active canvas graph.
- **Independent Storage**: Your library is stored in SQLite WAL format under `ComfyUI/user/prompt_library/`. Deleting, moving, or cleaning up generated output media never removes your prompts or workflow records.
- **Media Gallery & Playback**: View images and stream HTML5 video and audio directly in the ComfyUI interface with cached thumbnails and decoded video frame previews.
- **Collections & Multi-Page Bulk Actions**: Organize prompts with custom collections, tags, and favorites. Perform bulk operations across multiple paginated pages. Deleting a collection leaves its prompt records intact.
- **Portable Archives & Deduplication**: Export selections as self-contained ZIP archives and preview imports with automatic conflict detection and idempotent deduplication.
- **Automated & Safety Backups**: Scheduled automatic backups with retention pruning, manual one-click backups with ZIP download, and automatic safety backups created immediately prior to any database restoration.

---

## Install Guide

**Updates are published to this repository's `main` branch. The Releases page and its ZIP downloads will not receive further updates.** Git clone is recommended so you can keep your installation current with `git pull`. If you install an older release ZIP, follow the one-time setup below before updating.

### Option 1: Git Clone (Recommended)

1. Navigate to your ComfyUI `custom_nodes` directory:
   ```sh
   cd ComfyUI/custom_nodes
   ```
2. Clone this repository:
   ```sh
   git clone https://github.com/IAmThroot/Prompt-Library.git prompt_library
   ```
3. Restart ComfyUI.

### Option 2: Download Release ZIP

1. Go to the [Releases](https://github.com/IAmThroot/Prompt-Library/releases) page.
2. Download an available `prompt_library.zip`. These archives are older snapshots and do not include subsequent fixes.
3. Extract the ZIP into your ComfyUI `custom_nodes/` directory so the folder structure is:
   ```text
   ComfyUI/
   └── custom_nodes/
       └── prompt_library/
           ├── __init__.py
           ├── nodes.py
           ├── db.py
           ├── web/
           └── ...
   ```
4. Restart ComfyUI.

### Updating an Existing Installation

For a Git installation, stop ComfyUI, open a terminal in your `ComfyUI/custom_nodes/prompt_library` folder, and run:

```sh
git pull --ff-only origin main
```

Restart ComfyUI, then hard-refresh the browser (`Ctrl+Shift+R`, or `Cmd+Shift+R` on macOS). Run this update command whenever you want the latest changes; release ZIPs will remain unchanged. If Git reports local changes or diverging history, preserve your changes and resolve the message before continuing.

**Installed from a release ZIP? One-time setup is required.** ZIP downloads do not contain Git history, so `git pull` cannot run directly in the extracted folder:

1. Stop ComfyUI.
2. Move the existing `prompt_library` extension folder to a backup location **outside** `ComfyUI/custom_nodes` so ComfyUI cannot load two copies. Keep any local customizations there.
3. Open a terminal in `ComfyUI/custom_nodes` and run:
   ```sh
   git clone https://github.com/IAmThroot/Prompt-Library.git prompt_library
   cd prompt_library
   git pull --ff-only origin main
   ```
   Cloning already fetches the latest code; the pull verifies it is current and is the command to use for future updates.
4. Restart ComfyUI and hard-refresh the browser.

Your library is stored separately under `ComfyUI/user/prompt_library/`. Keep that directory intact; replacing the extension folder does not require removing your saved library.

### Requirements & Optional Dependencies


- **Python**: 3.10+
- **Pillow (`PIL`)**: Required for image reading and thumbnail generation (already included with ComfyUI).
- **PyAV or FFmpeg**: Optional but recommended for video and audio metadata extraction and video thumbnail frame extraction:
  - If installed, PyAV is preferred.
  - If FFmpeg (`ffprobe` / `ffmpeg`) is available on your system `PATH`, it is automatically used as a fallback.

---

## Usage

### 1. Opening the Library

- **Sidebar Dock** (Modern ComfyUI): Click the **Prompts** book icon (`pi pi-book`) on the left toolbar.
- **Floating Button** (Legacy Frontends): Click the **Prompt library** button at the bottom-left of the viewport.
- **Command Palette**: Search for `Open Prompt Library` (`Ctrl+P` / `Cmd+P`).
- **Prompt Library Node**: Click the **Open library** button directly on any `Prompt Library` node on the canvas.

### 2. Output Scanning & Background Capture

- **Automatic Capture**: Runs in the background at regular intervals while ComfyUI is running, even with the browser closed.
- **Manual Scan**: Click **Scan outputs** in the library header. Hold `Shift` and click **Scan outputs** to force a full re-read of all files.
- **Scan Report**: Click **Scan report** to inspect discovered files, unsupported formats, deferred writes, or extraction diagnostics.

### 3. Saving Prompts and Workflows

- **Save Current Workflow**: Click **Save current workflow** in the library panel or command palette. The active graph and extracted prompt text are saved to the library. Graphs with non-text nodes are saved as workflow-only entries (`kind='workflow'`).
- **Prompt Library Node**: Click **Save to library** directly on the node to store its positive and negative prompt fields.
- **New Prompt**: Click **New prompt** to manually compose a title, positive prompt, negative prompt, tags, and notes.

### 4. Inserting Prompts into Workflows

- **Insert**: Click **Insert** on any prompt block to insert text into the currently selected node on the canvas. If the node has multiple text boxes, an explicit field chooser dialog will appear.
- **Add Node**: Click **Add node** to spawn a new `CLIPTextEncode` node pre-populated with the prompt text.
- **Use / Use Pair**: When a `Prompt Library` node is active, click **Use** (or **Use pair** for multi-stage prompts) to fill its input fields.
- **Copy / Copy Both**: Copy prompt text to your system clipboard (supports plain text and `Positive\n\nNegative prompt:` formats).

### 5. Workflow Snapshots in Separate Tabs

- In the expanded view of any entry containing a workflow snapshot, click **Open workflow in new tab**.
- This opens a cloned workflow in a temporary workspace tab using a fresh UUID, keeping your active canvas workflow intact and unmodified.
- You can also download the standalone `.json` workflow or API graph using the download links.

### 6. Collections & Bulk Organization

- **Filter & Search**: Filter by *All*, *Favorites*, *Images*, *Videos*, *Audio*, or *Saved by me*. Search across titles, prompt text, stage descriptions, and tags.
- **Manage Collections**: Create, rename, or delete collections. Deleting a collection removes the category container but leaves all associated prompts and workflows intact.
- **Bulk Actions**: Select items across multiple pages with **Select page** or **Select all matching**. Apply bulk actions:
  - Add to or remove from collections
  - Add or remove tags
  - Mark as favorite / unfavorite
  - Export selected
  - Remove from library

### 7. Media Gallery & Playback

- Expand any card to see its linked generation history, original filenames, file timestamps, and extraction settings.
- Click **View image** to preview images in full resolution.
- Click **Play media** to stream HTML5 videos or audio with native seek, playback, and volume controls.
- Closing the viewer immediately stops playback and releases media decoders.
- Scans and library data refreshes reload expanded galleries and workflow lists. Generation pagination restarts on a data refresh; checkbox selection changes preserve loaded gallery pages.

### 8. Settings, Backups & Recovery

Click **Settings and backups** to configure:
- **Capture Settings**: Toggle background capture, scan interval (seconds), hidden output files and folders, and symlink traversal. Disabling hidden capture skips future scans of hidden content; previously indexed records remain saved.
- **Automatic Backups**: Toggle automated periodic backups, set backup interval (hours), and configure backup retention count (older scheduled backups are pruned automatically; manual and safety backups are preserved).
- **Manual Backups**: Click **Back up now** to trigger an instant backup and download the resulting ZIP archive.
- **Safe Restore**: Restore previous backups with one click. An automatic safety backup of the current library is created immediately prior to restoration.

Closing a job progress dialog stops browser polling while the server continues the job. Check **Recent jobs** in **Settings and backups** for its status; reopen settings to refresh that list. Keeping the progress dialog open shows the normal completion and download options.

---

## Testing

Run the full Python regression test suite from the repository root:

### Python Unit Tests
```sh
python -m unittest discover -s tests -v
```

### Syntax and Compilation Verification
```sh
python -m compileall -q __init__.py nodes.py db.py extract.py routes.py scanner.py service.py archives.py library.py validation.py
node --input-type=module --check < web/prompt_library.js
node --input-type=module --check < web/lifecycle.js
node --experimental-vm-modules --test tests/test_frontend.mjs
```

### Output Folder Audit Tool (Optional)
To audit an existing outputs folder without modifying its files or touching your live library:
```sh
python tools/audit_outputs.py /path/to/ComfyUI/output --roundtrip --report /tmp/prompt-library-report.json
```

---

## Safety, recovery, and resource limits

External media links require two settings: enable **Follow linked files and folders**, then add each permitted external directory under **Allowed external folders**, one absolute path per line. With traversal disabled, both file and folder symlinks are skipped. Internal links remain supported when traversal is enabled. Removing an allowed folder revokes access to its original media; saved prompts, workflows, and cached previews remain in the library. Disabling capture also prevents automatic scanning on restart.

**Portable exports** and **recovery backups** serve different purposes:

- Export/import uses the existing portable ZIP format for sharing selected entries. Portable manifests are limited to 64 MiB and individual previews to 2 MiB, in addition to the configured compressed and expanded archive limits. Oversized exports fail without publishing a partial archive; export a smaller selection or increase the applicable archive limit.
- New local backups use recovery format v2: a SQLite snapshot plus checksummed previews and a compact manifest. Local restore does not apply portable upload limits. Existing v1 local backups remain restorable. Restore from the backup catalogue in **Settings and backups**; recovery-v2 files are not accepted by portable import. Manual and safety backups are not automatically pruned.
- Backup publication follows validation. Restore keeps a safety backup and rolls back database and preview changes if recovery fails.

Background jobs use one worker with at most 32 active/queued/uploading jobs. Terminal jobs and their files are retained for up to 24 hours, with at most 1,000 job records and an 8 GiB budget for job artifacts and upload/output reservations. Old terminal artifacts may be reclaimed earlier to make room; active jobs and their inputs are protected. Recovery backups are outside this temporary-job budget. Full conflict reports are stored on disk (up to 256 MiB); the UI shows at most 100 conflicts with long fields shortened. Download the full report before it expires. Interrupted jobs are marked failed after restart and are not automatically replayed.

For API clients, bulk operations must explicitly supply exactly one selection form: `{"ids":[1,2]}` or `{"filters":{"search":"portrait"}}`. Missing or malformed selections return HTTP 400. Empty IDs affect nothing; explicit empty filters select all matching entries. A full job queue or exhausted temporary storage returns HTTP 429 with `Retry-After: 30`. Export retains its existing empty-selection behavior.

### Additional regression checks

The real HTTP integration tests require `aiohttp` (already used by ComfyUI). For a standalone development environment, install `Pillow` and `aiohttp`. Without aiohttp, the HTTP integration tests are explicitly skipped. The remaining tests use temporary libraries and do not require ComfyUI.

```sh
python -m unittest discover -s tests -v
node --experimental-vm-modules --test tests/test_frontend.mjs
node --input-type=module --check < web/lifecycle.js
```
See [CHANGELOG.md](CHANGELOG.md) for implemented changes.
