# Agent Handoff Instructions

Read `README.md` before changing behavior; `README.zh-CN.md` contains the same guide in Chinese. The implemented rules below are intentional custom rules, not bugs to be “corrected” to standard Xiangqi.

## Non-negotiable invariants

1. **The server is authoritative.** Validate turns, ownership, coordinates, move legality, request IDs, captures, and match outcomes in `app.py`. The browser in `static/app.js` is a renderer/input client, not a rule engine.
2. **Preserve hidden-piece privacy.** `Piece.kind` is the true identity, and `Piece.move_kind` is the original-square movement pattern. A face-down piece moves by `move_kind`; after it flips face-up, it moves by `kind`. `public_board()` / `snapshot()` must not leak a hidden piece's `kind` to either browser. For captured hidden pieces, only the capturing side learns the true kind.
3. **Preserve the custom movement variants.** Advisors may leave the palace and cross the river; Elephants may cross the river but still move two diagonally with an unblocked eye; a revealed Soldier starting inside its own palace may move one step in any cardinal direction.
4. **Undo means two moves, not one.** Accepting an undo restores the state before the latest two successful moves, removes both corresponding history entries, restores captures/log/turn/last-move markers, and updates the turn sequence so request locks do not leak into the restored turn.
5. **Requests are server-validated.** Undo/draw can be requested only on the requester's turn. Only one request may be pending. A rejection blocks that request type for the remainder of that turn; stale or mismatched request IDs must not be accepted. Surrender ends the match immediately.
6. **Persistence must remain coherent.** If a `GameRoom` field changes, update `serialize_game()` and `deserialize_game()` with safe defaults for old saved JSON. Also consider whether it belongs in undo snapshots (`history`) and in the viewer-specific `snapshot()` payload. If the schema needs SQL changes, add a migration in `db()`.
7. **Keep reconnection working.** `players.sid` is a legacy column name that stores the persistent player ID from the Flask session, not the current Socket.IO SID. The live connection ID is stored separately in each in-memory `game.players[side]["sid"]` and changes on reconnect.
8. **Single-process assumption.** `rooms` is process-local. SQLite allows recovery after restart but is not a shared real-time room backend for multiple workers. Do not recommend multi-worker deployment without introducing shared state and a Socket.IO message queue.
9. **Treat runtime data as private.** Never commit `players.sqlite3`, `.flask_secret_key`, `.env`, or test data copied from a user's live database. Preserve the first two files during upgrades and backups.
10. **Keep the browser client local.** `templates/index.html` loads Socket.IO from `/static/socket.io.js`, not from an online CDN. Do not replace it with a CDN URL; the game must remain usable on an offline LAN.
11. **Preserve replay privacy and ownership.** Record only public board frames during play. Saved replays are owner-scoped, and hidden captured-piece identities must be filtered separately for each participant. Accepted two-move undos remove the corresponding replay frames.

## Change workflow

1. Read the relevant rule/architecture part of `README.md` and inspect both the server and its client event handler.
2. Make the smallest behavior change possible; avoid editing generated/runtime files.
3. Run:

   ```bash
   python -m py_compile app.py
   node --check static/app.js
   ```

   `node` is optional if unavailable. Syntax checks alone do not prove Socket.IO behavior.
4. Manually test with two independent browsers/devices: room creation/joining, ordinary and illegal moves, information privacy, capture, request acceptance/rejection, undo of two moves, replay keep/discard and step controls, reload/reconnect, server restart, and end-of-game cleanup.
5. Update both README files when changing user-visible rules, deployment steps, or event contracts.
6. Before packaging, verify that the archive contains only source/docs and does not contain virtual environments, `__pycache__`, SQLite databases, or secret key files.

## Main files

- `app.py`: Flask routes, Socket.IO event handlers, rule validation, serialization, and SQLite persistence.
- `templates/index.html`: page structure and board/dialog containers.
- `static/app.js`: event listeners, board rendering and browser interactions.
- `static/style.css`: board, piece, responsive layout, markers and dialogs.
- `requirements.txt`: runtime Python dependencies.
- `config.yaml`: server bind address and port loaded by `app.py`.
