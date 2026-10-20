# Dark Xiangqi LAN

A two-player, browser-based **dark Xiangqi** game for a local area network. The server uses Python, Flask, Flask-SocketIO, Eventlet, and SQLite. The browser UI is plain HTML, CSS, and JavaScript; no Node.js build step is required.

> **Read this document in order:** deployment comes first, followed by the complete game rules and the implementation notes intended to help the next maintainer or coding agent continue the project safely.

## 1. Deployment guide

### 1.1 Requirements

- Python **3.10 or newer** and `pip`.
- A Windows, Linux, or macOS host that can be reached by both players on the same LAN.
- A modern browser on each player's device.
- A local copy of the Socket.IO browser client at `static/socket.io.js`. The page does not use an online CDN, so it works on a fully offline LAN.

No Node.js, npm, frontend bundler, or external database server is needed.

### 1.2 Get the source and enter the project directory

Extract the project archive. The project root is the directory containing `app.py`, `requirements.txt`, `templates/`, and `static/`:

```text
xiangqi-dark/
├── app.py
├── config.yaml
├── requirements.txt
├── README.md
├── README.zh-CN.md
├── LICENSE
├── templates/
│   └── index.html
└── static/
    ├── app.js
    ├── socket.io.js
    └── style.css
```

Open a terminal in `xiangqi-dark/`. Run all commands below from this directory unless stated otherwise.

### 1.3 Create a virtual environment

**Windows PowerShell:**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

If PowerShell blocks the activation script, either use Command Prompt instead:

```bat
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
```

**Linux or macOS:**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

A virtual environment isolates this game's dependencies from other Python projects.

### 1.4 Install the Python dependencies

```bash
python -m pip install -r requirements.txt
```

The current dependency ranges are:

- `Flask` — HTTP routes and HTML template rendering.
- `Flask-SocketIO` — bidirectional real-time game events.
- `eventlet` — the Socket.IO async server backend selected by `app.py`.
- `PyYAML` — loads the server bind address and port from `config.yaml`.

If installation fails, check that the virtual environment is active and that the selected Python version is compatible with the packages available to your platform. Do not silently remove Eventlet from `requirements.txt`: the application explicitly configures `async_mode="eventlet"`.

### 1.5 Start the server

```bash
python app.py
```

The server reads `config.yaml` at startup. Its default configuration listens on **`0.0.0.0:8080`**, making it reachable from other LAN devices. On the host itself, open:

```text
http://127.0.0.1:8080
```

Keep the terminal open while playing. Press **Ctrl+C** in that terminal to stop the server cleanly.

To use a different bind address or port, edit:

```yaml
server:
  ip: 0.0.0.0
  port: 8080
```

The `PORT` environment variable can temporarily override `server.port`:

**PowerShell**

```powershell
$env:PORT = "8080"
python app.py
```

**Linux/macOS**

```bash
PORT=8080 python app.py
```

Both players must use the same host address and port.

### 1.6 Let another device connect over the LAN

1. Find the server computer's **LAN/private IP address**. On Windows, run `ipconfig` and look for the active adapter's IPv4 address. On Linux, use `ip addr`; on macOS, check Network settings or `ifconfig`.
2. Ensure both devices are connected to a network that allows peer-to-peer LAN traffic. Guest Wi-Fi and client-isolated access points may block this.
3. On the second device, open `http://SERVER_LAN_IP:8080` (replace the address and port with the values in `config.yaml`).
4. Each player sets a nickname. One player creates a room and shares the five-character room code; the other enters the code and joins.

**Firewall:** On Windows, allow the intended Python runtime or inbound TCP port `8080` on a trusted **Private** network. If using another port, allow that port instead. Only open the port on networks where you trust the other devices. Do not disable the firewall globally to make the game work.

### 1.7 Offline/browser asset note

The current HTML loads Socket.IO's browser client from `/static/socket.io.js`; it does **not** use an online CDN. The bundled client is Socket.IO 4.8.1 and is compatible with the Socket.IO 4.x server stack used by Flask-SocketIO. Keep this local asset when packaging the project so an offline LAN has no external browser dependency.

When upgrading Socket.IO, replace `static/socket.io.js` with a client version compatible with the installed Flask-SocketIO dependency. Test a fresh browser load and a two-player connection before distributing the build.

### 1.8 Persistent data and restart recovery

The application creates its SQLite database and Flask session key in the project directory, beside `app.py`:

- **`players.sqlite3`** — player nickname and active-match markers in `players`, plus serialized unfinished games in `games`.
- **`.flask_secret_key`** — generated automatically on first startup when the `SECRET_KEY` environment variable is not set. It signs the browser's Flask session cookie, including the persistent player ID.

The same database also contains the `replays` table for matches that a player chose to retain after the game ended. A replay is visible only to the signed-in player who saved it.

Keep both files when upgrading or restarting the server. Removing the database discards stored player/game state. Replacing the secret key invalidates existing signed browser sessions, so players may no longer be recognized as the same players. If the user clears their browser cookies/site data, the server can no longer associate that browser with its prior player ID.

At startup, the app loads unfinished games from SQLite and reserves their seats. When the browser reconnects with its existing session cookie, the Socket.IO `connect` handler binds the new socket ID to the stored seat and sends the latest state. The game board, turn, captured pieces, log, last move, undo history, replay frames, pending request, and request-denial locks are serialized into the `games.state_json` field.

**Safe backup/upgrade procedure:**

1. Stop the server with Ctrl+C.
2. Back up `players.sqlite3` and `.flask_secret_key` to a private location.
3. Update source files; preserve the two data files in the project directory.
4. Activate the virtual environment and run `python -m pip install -r requirements.txt` again if dependencies changed.
5. Start with `python app.py` and test both players, a reconnect, and a restart recovery before relying on the updated version.

Do not commit `players.sqlite3`, `.flask_secret_key`, `.env`, or virtual-environment files to source control.

### 1.9 Optional stable session key

Instead of the generated key file, an operator may set `SECRET_KEY` in the environment. It must be a long, random, private value and must remain stable across restarts; in a multi-instance deployment it must be identical on every instance. Example generation command:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Set the result as `SECRET_KEY` in the launch environment; do not paste a production secret into source code or a public repository. If `SECRET_KEY` is not set, the project-local `.flask_secret_key` file is used automatically.

### 1.10 Troubleshooting

| Symptom | Checks |
|---|---|
| `python` is not found | Install Python, reopen the terminal, and use `py -3` on Windows if needed. |
| `ModuleNotFoundError` | Activate `.venv` and rerun `python -m pip install -r requirements.txt`. |
| Local browser cannot open the page | Check the terminal for startup errors and verify `config.yaml`. Try `http://127.0.0.1:8080` unless the configured port differs. |
| The host works, but the second device cannot connect | Check the host's LAN IP, subnet, guest Wi-Fi/client isolation, OS firewall, and port. Do not use `127.0.0.1` from the second device. |
| Page loads but connection indicator stays disconnected | Check browser developer tools, confirm `/static/socket.io.js` returns successfully, and verify the configured host/port and firewall. |
| Player does not resume their game | Keep browser cookies/site data, `players.sqlite3`, and `.flask_secret_key`; check that the browser is using the same hostname/session and inspect the server log. |
| A game disappears after an upgrade | Stop using that build and restore the database backup. Keep game-state serialization backward-compatible when changing `serialize_game()` or `deserialize_game()`. |

### 1.11 Deployment/security boundaries

This project is intended for trusted-LAN play and development, **not as a hardened public internet service**. It has no account/password system, TLS termination, rate limiting, abuse protections, or multi-worker shared Socket.IO state. Do not expose port 8080 directly to the public internet. For public deployment, put the application behind a properly configured production reverse proxy and add authentication, TLS, request protections, rate limits, and a shared Socket.IO message queue/session strategy first.

The application uses process-local `rooms` plus SQLite persistence and is designed to run as a **single application process**. SQLite saves games across restarts; it does not make in-memory room state automatically shared across multiple server workers.

## 2. Game rules

These are the rules implemented by this project, including its custom dark-Xiangqi variants. They are not identical to standard Chinese chess.

### 2.1 Board, sides, and initial setup

- The board has **9 files by 10 ranks**, with pieces represented at the grid intersections.
- There are two sides: **Red** and **Black**. Red moves first.
- Each side has 16 pieces in total: one General, two Advisors, two Elephants, two Horses, two Chariots, two Cannons, and five Soldiers.
- Each General starts face-up at the center of its home back rank. The other 15 pieces on each side are face-down.
- The **actual identities** of those 15 pieces are randomly shuffled among that side's 15 designated starting squares. The movement pattern of each face-down piece is initially bound to the standard Xiangqi piece associated with its starting square, not to its hidden actual identity. This is the core variant of this game.

### 2.2 Turn and reveal

1. Only move one of your own pieces on your turn.
2. The server validates the move. Clicking a piece or destination in the browser does not make an illegal move valid.
3. A face-down piece moves according to the movement pattern assigned to its original starting square. Its identity stays hidden to the opponent and to its owner on the board.
4. On its first successful move, including a capture move, the piece flips face-up. From then on, its movement follows its **actual revealed piece type**.
5. A move may capture an opposing piece by moving onto its square, if the applicable movement rule permits it. You cannot capture your own piece.
6. Capturing the opposing General immediately wins the game.

The Generals start face-up; all other starting pieces are hidden.

### 2.3 Piece movement

The movement below is the rule for a **revealed piece**, and also describes the movement pattern assigned to the relevant starting square while a piece is still face-down.

| Piece | Movement in this project |
|---|---|
| **Chariot (車/车)** | Any number of intersections horizontally or vertically. No piece may be between the start and destination. |
| **Cannon (炮)** | Moves horizontally or vertically. For a non-capturing move, there must be no piece between start and destination. For a capture, there must be exactly one intervening piece (the screen). |
| **Horse (馬/马)** | Moves in an L shape: two intersections along one axis and one along the other. Its “horse leg” square must be empty. |
| **Elephant (象/相)** | Moves diagonally exactly two intersections in each axis. The midpoint (“elephant eye”) must be empty. **Unlike standard Xiangqi, an Elephant may cross the river.** |
| **Advisor (士/仕)** | Moves one diagonal intersection. **Unlike standard Xiangqi, an Advisor may leave the palace and cross the river.** |
| **General (將/帅)** | Normally moves one intersection horizontally or vertically and must land inside its own palace: files 4–6 (human numbering) and ranks 1–3 for Black or 8–10 for Red. In addition, it may capture the opposing General along the same file if no piece blocks the path. Capturing the opposing General ends the match. |
| **Soldier (卒/兵)** | Outside its own palace, moves one intersection forward; after crossing the river it may instead move one intersection sideways. **Custom rule:** if a revealed Soldier begins its move inside its own palace, it may move one intersection up, down, left, or right. It cannot move diagonally. |

**Direction/river convention:** Red advances toward the top of the board; Black advances toward the bottom. Red is considered to have crossed the river when its current rank is on the upper half (internal row index `y <= 4`); Black when on the lower half (`y >= 5`). The palace test for a Soldier uses its **current/source square**.

### 2.4 Information revealed by captures

- The board identifies every piece's side, but a face-down piece's type is not shown.
- A face-up captured piece is identified normally.
- When a face-down piece is captured, the player who made the capture can see its true type in that player's captured-piece list. The side that lost that hidden piece sees only an unknown/“hidden piece” entry; it does not learn the identity from the opponent's capture list.
- The server retains the full true board internally, but sends each player a filtered state snapshot. Do not change the snapshot/captured-piece filtering casually; otherwise hidden-piece information could leak to the browser.

### 2.5 Undo request (two plies)

- A player can request an undo only during **their own turn**, after at least two successful moves have been made, while both players are in the room and no other request is pending.
- The other player receives an in-page alert-style dialog with **Accept** and **Reject** choices.
- If accepted, the game rolls back the most recent **two moves total**—one move by each side in normal alternating play. Board, turn, captured-piece lists, move log, and last-move markers return to the state before those two moves.
- If rejected, the requester cannot ask for another undo during the same turn. A new turn clears the restriction by advancing the turn sequence.
- The undo history and pending request are persisted, so a restart does not automatically discard them.

### 2.6 Draw request

- A player may request a draw only on their own turn, with both players present and no other pending request.
- The opponent must explicitly accept the request in the in-page dialog. Acceptance ends the match as a draw; rejection leaves the match running.
- After a rejection, the requester cannot request another draw during that same turn. Undo and draw have separate denial locks.

### 2.7 Surrender and match end

- Either player may surrender once both players have joined. Surrender ends the match immediately and awards the win to the opponent; the opponent does not need to accept.
- A successful capture of the opposing General also ends the match immediately.
- An accepted draw ends the match with the result `draw`.
- Once the match ends, active-game markers and the persisted unfinished-game row are removed. A finished match is not automatically resumed.

### 2.8 Last move, history, and request blocking

- Green dashed circles identify the start and destination of the most recent move.
- The move log and captured-piece lists are shown in the sidebar.
- While an undo/draw request is pending, neither player can make a move. Only the recipient can answer the request; stale or mismatched request IDs are rejected by the server.
- Request controls are disabled client-side when unavailable, but all important conditions are checked again on the server.

### 2.9 Saved replays

- The server records the public board after every successful move. An accepted two-move undo removes the corresponding two replay frames.
- When a match ends by general capture, surrender, or an accepted draw, each participant is asked whether to keep a replay.
- Choosing **Keep replay** saves the recording to that player's own replay list; choosing **Don't keep** does not create a saved replay for that player. Each participant can make this choice independently.
- Open a saved replay from the **Replays** panel on the lobby screen. The slider changes the displayed step, and the left/right triangle buttons move one step backward or forward.
- Replay snapshots use the same privacy boundary as live states. A hidden piece's identity is not stored in browser-visible board frames. When a hidden piece is captured, only the capturing player's saved replay reveals its true kind.
- Saved replays are scoped to the signed-in browser session that kept them. Clearing browser cookies or replacing `.flask_secret_key` can make those saved replays inaccessible to that browser.

### 2.10 Rules deliberately not implemented

This is a custom casual game, not a complete tournament Xiangqi engine. In particular:

- It does **not** calculate check/checkmate or require a move to protect one's own General.
- It does not implement repetition, move-count, or other formal Xiangqi draw adjudication. A draw is negotiated through the request/accept flow.
- It does not implement timers, AI opponents, spectators, or matchmaking beyond creating/joining a room by code.

## 3. Codebase handoff / maintainer notes

### 3.1 File map

- `app.py` — authoritative game server: Flask routes, Socket.IO events, board setup, move validation, room management, request flow, serialization, and SQLite persistence.
- `config.yaml` — server bind address and port loaded by `app.py`.
- `templates/index.html` — page structure, board line SVG, actions, sidebar, and request dialog.
- `static/app.js` — Socket.IO client, local UI state, board rendering, request dialog, and user interactions. It should not be treated as the authority for legal moves.
- `static/style.css` — wooden board and piece styling, responsive layout, state markers, dialog, and toast appearance.
- `static/socket.io.js` — locally served Socket.IO 4.8.1 browser client; no online CDN is used.
- `requirements.txt` — Python dependency ranges.
- `CHANGELOG.md` — notable user-visible and maintenance changes.
- `README.zh-CN.md` — Chinese-language version of the operational and rules guide.
- `LICENSE` — MIT license.
- `AGENTS.md` — working invariants and checklist for future coding agents.

### 3.2 Server data model

- `Piece(kind, side, face_up, move_kind)`: `kind` is the true identity; `move_kind` is the starting square's movement pattern; `face_up` decides which is used by `legal_move()`.
- `GameRoom`: board, players/seats, turn, captures, log, undo snapshots, replay frames, last move, move/turn counters, pending request and denial locks.
- `rooms`: process-local dictionary keyed by room code. SQLite is the recovery source on process startup, but live state is still kept in memory.
- `players` table: stores a stable browser/session `player_id`, nickname, `in_game`, `room_code`, and `side`. **The column is named `sid`, but it stores the persistent player ID, not the current Socket.IO SID.** The live socket SID is replaced when the browser reconnects.
- `games` table: stores one serialized unfinished match per room in `state_json`.

`db()` creates missing tables and migrates the game-status columns from older versions. `serialize_game()` / `deserialize_game()` define the persisted game-state schema. `load_persisted_rooms()` restores rooms at import/startup; `restore_player_room()` reattaches a reconnecting browser after its Socket.IO connection is established.

`replays` stores one row per player who retained a finished match. The row includes the owner's persistent player ID, room/result metadata, and owner-filtered replay JSON, so one participant's decision or hidden-piece knowledge cannot affect the other participant's saved copy.

### 3.3 Socket.IO event contract

Client to server:

| Event | Purpose |
|---|---|
| `set_nickname` | Set/update the player's nickname in the current Flask session. |
| `create_room` | Create a room and reserve the creator as Red. |
| `join_room` | Join an existing room code; the second player becomes Black. |
| `move` | Submit `from`/`to` board coordinates for server validation. |
| `request_undo` | Ask the opponent to undo the latest two moves. |
| `request_draw` | Ask the opponent to agree to a draw. |
| `respond_request` | Accept/reject a pending request using its ID. |
| `surrender` | End the match and award the win to the opponent. |

Server to client:

| Event | Purpose |
|---|---|
| `nickname_ok` | Nickname accepted. |
| `room_joined` | Room code and side assigned/restored. |
| `state` | Player-specific public snapshot of the match. |
| `notice` | Human-readable operation result/error. |
| `request_result` | Result of a request sent by the player. |
| `room_ended` | Match ended; the page returns to the lobby and receives an optional `replay_offer` prompt. |

HTTP endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/replays` | List replays owned by the current signed-in player. |
| `GET /api/replays/<id>` | Load one owner-filtered replay. |
| `POST /api/replays/offers/<id>` | Accept or decline the post-game replay offer for the current player. |

The `state` payload is viewer-specific. Keep its hidden-piece and captured-piece privacy rules intact. When adding a new persistent `GameRoom` field, update both serialization and deserialization, choose a safe default for old saved JSON, and consider how undo snapshots should restore it.

### 3.4 Suggested change/test workflow

1. Back up `players.sqlite3` and `.flask_secret_key` before running test code against a real saved game.
2. Make rule validation changes in `legal_move()` or the relevant server event, not only in JavaScript.
3. If adding persistent state, update `GameRoom`, `serialize_game()`, `deserialize_game()`, snapshot output as appropriate, and undo-history capture/restore.
4. Check syntax: `python -m py_compile app.py`; if Node.js is installed, check client syntax with `node --check static/app.js`.
5. Run two browsers/devices and test room creation/joining, legal/illegal movement, hidden information, captures, request accept/reject, undoing two moves, reload/reconnect, and restarting the server.
6. Before committing, verify no SQLite database, session secret, browser state dump, or other local runtime data is included.

Automated syntax checks are not a substitute for the two-player browser test, especially for Socket.IO event order and reconnection flows.
