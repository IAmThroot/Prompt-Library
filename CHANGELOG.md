# Changelog

All notable changes to Prompt Library will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Refresh expanded generation galleries and workflow lists when library data reloads, while preserving loaded gallery pages during selection-only rerenders.
- Continue scanning remaining files when a source disappears or becomes inaccessible during thumbnail processing, recording a per-file diagnostic.
- Apply the hidden capture setting to hidden filenames as well as hidden directories. Previously indexed records remain saved.
- Stop browser job polling when progress dialogs close without cancelling server jobs or reopening completion dialogs afterward.
- Validate explicit selections on the selection endpoint and return HTTP 400 for missing, malformed, or ambiguous selections. Preserve existing export defaults.

### Documentation

- Document repository-only updates: the Releases page and release ZIPs will no longer receive updates.
- Add Git update instructions and a one-time migration from release ZIP installations to a Git checkout.
- Explain gallery refresh behavior, hidden-file capture, and how to check jobs after closing progress dialogs.

### Tests

- Add regressions for gallery invalidation, job observation cancellation, disappearing preview sources, hidden filenames, and selection endpoint validation.
- Automated checks passed: 91 Python tests and seven frontend tests; two additional HTTP integration tests were skipped because aiohttp was unavailable. Live ComfyUI checks were confirmed by the maintainer.

## [1.1.0] - 2026-11-03

### Security

- Validate portable prompt details, stages, metadata, and generation records before importing. Reserved fields such as record IDs cannot be overridden through imported details; existing stored details are also read through a whitelist.
- Reject oversized archive previews before decompression and enforce bounded reads, checksums, ownership checks, and image validation.
- Apply symlink policy to files as well as folders. External targets require both enabled traversal and an explicitly allowed directory. Recheck permissions when serving originals; revoking access preserves saved prompts and previews.
- Require explicit, validated selections for bulk operations. Missing selections no longer affect the entire library.

### Recovery and compatibility

- Write new local backups in recovery format v2 using a SQLite snapshot, compact manifest, and checksummed previews. Validate staged backups before publication.
- Restore local recovery backups independently of portable upload limits, including legacy v1 local backups. Preserve safety backups, the local backup catalogue, and rollback of database and previews after restore failures.
- Keep portable ZIP export/import for sharing. Recovery-v2 archives are restored through the backup catalogue, not portable import.
- Reject oversized portable exports without publishing partial files. Retain the 64 MiB portable manifest limit and 2 MiB preview limit.

### Correctness

- Respect saved capture and traversal settings from startup; disabling capture prevents the initial automatic scan.
- Make a personal copy when editing text associated with a saved workflow, keeping the original workflow and generation provenance intact.
- Display metadata and stage details from the same newest generation, using source time and generation ID as the ordering criteria.
- Share workflow discovery between prompt extraction and workflow storage, including case-insensitive metadata and direct API graphs in comments. Bump the extractor version to backfill existing files.
- Try ffprobe when an installed PyAV reader fails, retaining both diagnostics if neither reader succeeds.
- Keep import report publication inside the import transaction so report failures roll back changes.

### Resource handling and performance

- Persist background job metadata, fail interrupted jobs on restart without replaying them, and reconcile orphaned artifacts.
- Limit active/queued/uploading jobs to 32, retained terminal records to 1,000, retention to 24 hours, and temporary job artifacts/reservations to 8 GiB. Protect active inputs from eviction and return HTTP 429 with retry guidance when admission fails.
- Store full import conflict reports on disk, with a 256 MiB limit. Return at most 100 conflicts with shortened long fields in job responses and provide full report downloads.
- Export from consistent database snapshots, select relevant rows in SQL, and stream preview copying and database hashing. Release the live database lock before backup compression and hashing.
- Cache thumbnail validation in a bounded 4,096-entry cache, batch scanner asset lookups, and back off failed thumbnail generation up to one hour. Forced scans bypass retry backoff.
- Move preview fallback database work off the HTTP event loop.
- Preserve loaded gallery pages during unrelated panel rerenders, cancel obsolete requests, and dispose panel/gallery resources while retaining media playback cleanup.

### Tests and packaging

- Add archive, recovery, media permission, job lifecycle, scanner, provenance, and interruption regressions, plus real aiohttp concurrency tests.
- Add frontend lifecycle tests for gallery retention, stale responses, request cancellation, disposal, and media cleanup.
- Include the validation module, frontend lifecycle module, and this changelog in release packaging; run HTTP and frontend tests in the release workflow.

## [1.0.0] - 2026-11-02

### Added

- Initial release of Prompt Library for ComfyUI.
- Automatic background output directory scanning and metadata extraction for images, videos, and audio.
- Canvas workflow snapshot preservation and isolated tab restoration.
- SQLite-backed local library under `user/prompt_library/`.
- Media gallery with thumbnail previews and HTML5 video/audio playback.
- Custom collections, tags, favorites, and multi-page bulk actions.
- Portable archive exports and imports with deduplication.
- Scheduled and manual backup and restore support.

