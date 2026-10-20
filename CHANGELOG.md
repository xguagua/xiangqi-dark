# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-21

### Added

- Record every successful move in a privacy-filtered match replay.
- Prompt each participant after the match ends to keep or discard the replay.
- Add a saved-replay picker and step viewer to the lobby, including a range slider and previous/next controls.
- Persist retained replays per player in SQLite and expose owner-authorized replay list and detail endpoints.
- Add English and Chinese documentation for replay behavior and the related HTTP contract.

### Changed

- Preserve replay frames across unfinished-game persistence and remove the matching frames when a two-move undo is accepted.
- Include the optional replay offer in the `room_ended` Socket.IO payload.
- Serve the browser Socket.IO client locally for offline LAN use.

### Fixed

- Load the server bind address and port from `config.yaml`, with a validated `PORT` environment override.
- Add PyYAML to the runtime dependencies so YAML configuration works in the project virtual environment.
