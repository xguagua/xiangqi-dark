"""Dark Xiangqi LAN server.

Architecture overview for maintainers:
- Flask serves the single HTML page and the nickname/session HTTP endpoints.
- Flask-SocketIO carries room, move, undo, draw, surrender, and state events.
- The server is authoritative: never trust the browser to validate moves or outcomes.
- ``rooms`` is the live in-memory state; SQLite serializes unfinished matches so
  rooms and player seats can be restored after a process restart.
- Player-specific snapshots deliberately hide the identity of face-down pieces.

This application is designed for one trusted-LAN server process. SQLite persistence
supports restart recovery, but the in-memory room dictionary is not a multi-worker
shared-state mechanism.
"""

import copy
import os
import json
import random
import secrets
import sqlite3
import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
import yaml
from flask import Flask, render_template, request, session, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room

# ---- Paths and runtime configuration ---------------------------------------
# Keep local SQLite data and the automatically generated signing key beside app.py.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "players.sqlite3")
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
REPLAY_FILE_FORMAT = "xiangqi-dark-replay"
REPLAY_FILE_VERSION = 1
REPLAY_RULESET = "dark-xiangqi-custom-v1"
MAX_REPLAY_FILE_BYTES = 8 * 1024 * 1024
MAX_REPLAY_FRAMES = 5000
MAX_PENDING_REPLAYS = 64


def load_config():
    """Load and validate the server bind address and port from config.yaml."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file) or {}
    except FileNotFoundError as exc:
        raise RuntimeError(f"配置文件不存在：{CONFIG_PATH}") from exc
    except yaml.YAMLError as exc:
        raise RuntimeError(f"配置文件 YAML 语法无效：{CONFIG_PATH}") from exc

    if not isinstance(config, dict):
        raise RuntimeError("config.yaml 顶层必须是映射")
    server = config.get("server")
    if not isinstance(server, dict):
        raise RuntimeError("config.yaml 必须包含 server 配置段")

    host = server.get("ip")
    port = server.get("port")
    if not isinstance(host, str) or not host.strip():
        raise RuntimeError("config.yaml 中的 server.ip 必须是非空字符串")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RuntimeError("config.yaml 中的 server.port 必须是 1 到 65535 之间的整数")

    # Keep the established PORT override for temporary deployments.
    if "PORT" in os.environ:
        try:
            port = int(os.environ["PORT"])
        except ValueError as exc:
            raise RuntimeError("环境变量 PORT 必须是整数") from exc
        if not 1 <= port <= 65535:
            raise RuntimeError("环境变量 PORT 必须在 1 到 65535 之间")

    return {"ip": host.strip(), "port": port}


def load_secret_key():
    """Keep Flask's cookie-signing key stable across server restarts by default."""
    configured = os.environ.get("SECRET_KEY")
    if configured:
        return configured
    key_path = os.path.join(BASE_DIR, ".flask_secret_key")
    try:
        with open(key_path, "r", encoding="utf-8") as key_file:
            key = key_file.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    key = secrets.token_hex(32)
    try:
        with open(key_path, "x", encoding="utf-8") as key_file:
            key_file.write(key)
    except FileExistsError:
        with open(key_path, "r", encoding="utf-8") as key_file:
            return key_file.read().strip()
    return key


# ---- Flask / Socket.IO application -----------------------------------------
# async_mode matches the Eventlet dependency declared in requirements.txt.
app = Flask(__name__)
app.config["SECRET_KEY"] = load_secret_key()
app.config["MAX_CONTENT_LENGTH"] = MAX_REPLAY_FILE_BYTES + 512 * 1024
socketio = SocketIO(app, async_mode="eventlet")
lock = RLock()
rooms = {}
pending_replays = {}

# Internal piece codes: uppercase is Red, lowercase is Black. These codes are
# also the stable representation written into SQLite; avoid renaming them without
# migrating saved games.
NAMES = {
    "K": "帅", "A": "仕", "B": "相", "N": "马",
    "R": "车", "C": "炮", "P": "兵",
    "k": "将", "a": "士", "b": "象", "n": "马",
    "r": "车", "c": "炮", "p": "卒",
}
RED_TYPES = ["A", "A", "B", "B", "N", "N", "R", "R", "C", "C", "P", "P", "P", "P", "P"]
BLACK_TYPES = ["a", "a", "b", "b", "n", "n", "r", "r", "c", "c", "p", "p", "p", "p", "p"]


def db():
    """Open SQLite and migrate older player tables without discarding match status.

    The ``players.sid`` column name is historical: it stores a stable player ID from
    the signed Flask session, not the current Socket.IO connection SID.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS players (
        sid TEXT PRIMARY KEY,
        nickname TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        in_game INTEGER NOT NULL DEFAULT 0,
        room_code TEXT,
        side TEXT
    )""")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(players)")}
    if "in_game" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN in_game INTEGER NOT NULL DEFAULT 0")
    if "room_code" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN room_code TEXT")
    if "side" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN side TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS games (
        code TEXT PRIMARY KEY,
        state_json TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    return conn


def clean_nickname(value):
    """Normalize a display nickname and reject empty, too-long, or control text."""
    value = " ".join(str(value or "").split()).strip()
    if not value or len(value) > 16 or any(ord(c) < 32 for c in value):
        return None
    return value


# ---- Game model -------------------------------------------------------------
@dataclass
class Piece:
    # ``kind`` is the true, randomly assigned piece identity. ``move_kind`` is the
    # standard piece pattern assigned to the square where this piece began.
    # While face-down, legal_move() uses move_kind. After the first successful move
    # turns the piece face-up, legal_move() uses the actual identity in kind.
    kind: str
    side: str
    face_up: bool = False
    move_kind: str = None


@dataclass
class GameRoom:
    """All mutable state for one match; persisted by serialize_game()."""

    code: str
    board: list  # 10 rows x 9 columns; each entry is None or a Piece.
    players: dict = field(default_factory=dict)  # side -> {sid, player_id, nickname}
    turn: str = "red"  # Side allowed to move when no request is pending.
    winner: str = None  # None while active; red / black / draw when finished.
    captured: dict = field(default_factory=lambda: {"red": [], "black": []})  # by capturer
    log: list = field(default_factory=list)  # Human-readable match log, newest at end.
    history: list = field(default_factory=list)  # Full snapshot captured before each move.
    last_move: dict = None  # Coordinates used for green dashed start/end markers.
    move_seq: int = 0  # Monotonic move marker ID; it does not roll back on undo.
    turn_seq: int = 0  # Advances after a move/undo; scopes same-turn rejection locks.
    pending_request: dict = None  # At most one undo/draw request can be pending.
    denied_request_turns: dict = field(default_factory=dict)  # request type -> turn_seq
    replay_frames: list = field(default_factory=list)  # Public states after each successful move.


# ---- SQLite persistence -----------------------------------------------------
def set_player_game_status(player_id, nickname=None, in_game=False, room_code=None, side=None):
    """Persist whether a player has an unfinished match, keeping player_id in players.sid."""
    if not player_id:
        return
    conn = db()
    try:
        if nickname is None:
            row = conn.execute("SELECT nickname FROM players WHERE sid = ?", (player_id,)).fetchone()
            nickname = row["nickname"] if row else "玩家"
        conn.execute(
            """INSERT INTO players(sid, nickname, in_game, room_code, side)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(sid) DO UPDATE SET
                 nickname = excluded.nickname,
                 in_game = excluded.in_game,
                 room_code = excluded.room_code,
                 side = excluded.side""",
            (player_id, nickname, 1 if in_game else 0, room_code if in_game else None, side if in_game else None),
        )
        conn.commit()
    finally:
        conn.close()


def clear_player_game_status(player_id):
    set_player_game_status(player_id, in_game=False)


def player_record(player_id):
    """Fetch a persistent player row by stable player ID (not Socket.IO sid)."""
    if not player_id:
        return None
    conn = db()
    try:
        row = conn.execute(
            "SELECT sid, nickname, in_game, room_code, side FROM players WHERE sid = ?",
            (player_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def serialize_game(game):
    """Convert a GameRoom to JSON-compatible primitives for SQLite storage.

    The serialized format is part of the persistence contract. If GameRoom changes,
    update this function and deserialize_game(), including defaults for older rows.
    """
    return {
        "code": game.code,
        "board": [[None if piece is None else {
            "kind": piece.kind, "side": piece.side, "face_up": piece.face_up,
            "move_kind": piece.move_kind,
        } for piece in row] for row in game.board],
        "players": {side: {
            "sid": None,
            "player_id": player.get("player_id"),
            "nickname": player["nickname"],
        } for side, player in game.players.items()},
        "turn": game.turn,
        "winner": game.winner,
        "captured": game.captured,
        "log": game.log,
        "history": game.history,
        "last_move": game.last_move,
        "move_seq": game.move_seq,
        "turn_seq": game.turn_seq,
        "pending_request": game.pending_request,
        "denied_request_turns": game.denied_request_turns,
        "replay_frames": game.replay_frames,
    }


def deserialize_game(data):
    """Rebuild dataclass instances from a previously serialized game dictionary."""
    board = [[None if piece is None else Piece(
        piece["kind"], piece["side"], piece.get("face_up", False), piece.get("move_kind")
    ) for piece in row] for row in data["board"]]
    players = {side: {
        "sid": None,
        "player_id": player.get("player_id"),
        "nickname": player.get("nickname", "玩家"),
    } for side, player in data.get("players", {}).items()}
    game = GameRoom(
        code=data["code"], board=board, players=players,
        turn=data.get("turn", "red"), winner=data.get("winner"),
        captured=data.get("captured", {"red": [], "black": []}),
        log=data.get("log", []),
        history=data.get("history", []),
        last_move=data.get("last_move"),
        move_seq=data.get("move_seq", 0),
        turn_seq=data.get("turn_seq", 0),
        pending_request=data.get("pending_request"),
        denied_request_turns=data.get("denied_request_turns", {}),
        replay_frames=data.get("replay_frames", []),
    )
    if not game.replay_frames:
        game.replay_frames = [make_replay_frame(game)]
    return game


def persist_game(game):
    """Persist an unfinished room's full board and player seats for restart recovery.

    The full hidden identities are stored server-side in SQLite. Only snapshot() and
    public_board() may shape data sent to a browser; never send serialize_game() output
    directly to a client.
    """
    data = json.dumps(serialize_game(game), ensure_ascii=False, separators=(",", ":"))
    conn = db()
    try:
        conn.execute(
            "INSERT INTO games(code, state_json, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(code) DO UPDATE SET state_json = excluded.state_json, updated_at = CURRENT_TIMESTAMP",
            (game.code, data),
        )
        conn.commit()
    finally:
        conn.close()
    for side, player in game.players.items():
        set_player_game_status(player.get("player_id"), player.get("nickname"), True, game.code, side)


def delete_persisted_game(code):
    """Delete a finished/removed room from the durable game table."""
    conn = db()
    try:
        conn.execute("DELETE FROM games WHERE code = ?", (code,))
        conn.commit()
    finally:
        conn.close()


def load_persisted_rooms():
    """Restore rooms after a server restart; a finished game is never resumed.

    Corrupt saved rows are discarded and stale players.in_game flags are cleared.
    This is called once during module initialization, before Socket.IO accepts clients.
    """
    conn = db()
    try:
        rows = conn.execute("SELECT code, state_json FROM games").fetchall()
        active_ids = set()
        for row in rows:
            try:
                game = deserialize_game(json.loads(row["state_json"]))
            except (ValueError, TypeError, KeyError):
                conn.execute("DELETE FROM games WHERE code = ?", (row["code"],))
                continue
            if game.winner:
                conn.execute("DELETE FROM games WHERE code = ?", (game.code,))
                for player in game.players.values():
                    if player.get("player_id"):
                        active_ids.discard(player["player_id"])
                continue
            rooms[game.code] = game
            for side, player in game.players.items():
                player_id = player.get("player_id")
                if player_id:
                    active_ids.add(player_id)
                    conn.execute(
                        """INSERT INTO players(sid, nickname, in_game, room_code, side)
                           VALUES (?, ?, 1, ?, ?)
                           ON CONFLICT(sid) DO UPDATE SET nickname=excluded.nickname,
                             in_game=1, room_code=excluded.room_code, side=excluded.side""",
                        (player_id, player.get("nickname", "玩家"), game.code, side),
                    )
        # Clear stale markers left by an older version or a removed/corrupt game row.
        marked = conn.execute("SELECT sid FROM players WHERE in_game = 1").fetchall()
        for row in marked:
            if row["sid"] not in active_ids:
                conn.execute("UPDATE players SET in_game=0, room_code=NULL, side=NULL WHERE sid=?", (row["sid"],))
        conn.commit()
    finally:
        conn.close()


# ---- Board construction and authoritative rules ---------------------------
def mt19937_shuffle(values):
    """Shuffle with CPython's MT19937 RNG seeded from OS-backed secret entropy."""
    shuffled = list(values)
    random.Random(secrets.randbits(256)).shuffle(shuffled)
    return shuffled


def new_board():
    """Build a 10x9 board with fixed visible Generals and shuffled hidden types.

    ``move_kind`` is derived from the original square; ``kind`` is randomly assigned.
    This intentional mismatch implements the hidden-piece movement variant.
    """
    board = [[None for _ in range(9)] for _ in range(10)]

    # Each occupied starting square has a standard Xiangqi movement pattern.
    # Actual identities are shuffled independently within each side. Therefore, a
    # face-down piece on the Advisor square initially moves diagonally like an
    # Advisor even if its hidden true identity is, for example, a Horse or Soldier.
    red_slots = [(9, x) for x in range(9)] + [(7, x) for x in (1, 7)] + [(6, x) for x in (0, 2, 4, 6, 8)]
    black_slots = [(0, x) for x in range(9)] + [(2, x) for x in (1, 7)] + [(3, x) for x in (0, 2, 4, 6, 8)]
    red_general, black_general = (9, 4), (0, 4)

    red_move_kinds = {
        **{(9, x): kind for x, kind in enumerate(["R", "N", "B", "A", "K", "A", "B", "N", "R"])},
        (7, 1): "C", (7, 7): "C",
        **{(6, x): "P" for x in (0, 2, 4, 6, 8)},
    }
    black_move_kinds = {
        **{(0, x): kind for x, kind in enumerate(["r", "n", "b", "a", "k", "a", "b", "n", "r"])},
        (2, 1): "c", (2, 7): "c",
        **{(3, x): "p" for x in (0, 2, 4, 6, 8)},
    }

    board[red_general[0]][red_general[1]] = Piece("K", "red", True, "K")
    board[black_general[0]][black_general[1]] = Piece("k", "black", True, "k")
    red_slots.remove(red_general)
    black_slots.remove(black_general)

    red_types = mt19937_shuffle(RED_TYPES)
    black_types = mt19937_shuffle(BLACK_TYPES)
    for (y, x), kind in zip(red_slots, red_types):
        board[y][x] = Piece(kind, "red", False, red_move_kinds[(y, x)])
    for (y, x), kind in zip(black_slots, black_types):
        board[y][x] = Piece(kind, "black", False, black_move_kinds[(y, x)])
    return board


def in_bounds(y, x):
    """Return whether a zero-based (row, column) coordinate is on the board."""
    return 0 <= y < 10 and 0 <= x < 9


def path_count(board, y1, x1, y2, x2):
    """Count occupied squares strictly between two aligned coordinates."""
    dy = (y2 > y1) - (y2 < y1)
    dx = (x2 > x1) - (x2 < x1)
    y, x, count = y1 + dy, x1 + dx, 0
    while (y, x) != (y2, x2):
        if board[y][x] is not None:
            count += 1
        y, x = y + dy, x + dx
    return count


def legal_move(board, y1, x1, y2, x2, piece, target):
    """Validate geometry/blocking for one move; caller checks ownership and turn.

    A face-down piece uses its square's assigned ``move_kind``; a face-up piece uses
    its actual ``kind``. Custom Advisor/Elephant/Soldier rules are documented in the
    README files. This function intentionally does not implement check/checkmate.
    """
    if (y1, x1) == (y2, x2) or not in_bounds(y2, x2):
        return False
    if target and target.side == piece.side:
        return False
    dy, dx = y2 - y1, x2 - x1
    ay, ax = abs(dy), abs(dx)
    # 暗棋：按初始位置对应的标准棋子走法。
    # 明棋：翻开后按棋子真实种类走法。
    movement_kind = piece.kind if piece.face_up else (piece.move_kind or piece.kind)
    k = movement_kind.lower()
    if k == "r":
        return (dy == 0 or dx == 0) and path_count(board, y1, x1, y2, x2) == 0
    if k == "c":
        if not (dy == 0 or dx == 0):
            return False
        between = path_count(board, y1, x1, y2, x2)
        return between == (1 if target else 0)
    if k == "n":
        if sorted((ay, ax)) != [1, 2]:
            return False
        leg_y, leg_x = (y1 + (1 if dy > 0 else -1), x1) if ay == 2 else (y1, x1 + (1 if dx > 0 else -1))
        return board[leg_y][leg_x] is None
    if k == "b":
        # 本项目的规则：象可以过河，但仍然必须走田字并且不能被象眼阻挡。
        if ay != 2 or ax != 2:
            return False
        return board[(y1 + y2)//2][(x1 + x2)//2] is None
    if k == "a":
        # 本项目的规则：士/仕可以离开九宫格、跨河；仍然只能斜走一格。
        return ay == 1 and ax == 1
    if k == "k":
        palace = (3 <= x2 <= 5) and ((7 <= y2 <= 9) if piece.side == "red" else (0 <= y2 <= 2))
        if palace and ay + ax == 1:
            return True
        # 将帅照面：允许帅/将沿同列直接吃对方将帅。
        if dx == 0 and target and target.kind.lower() == "k":
            return path_count(board, y1, x1, y2, x2) == 0
        return False
    if k == "p":
        # 变体规则：卒/兵位于己方九宫格内时，可以上下左右走一格。
        # 红方九宫格为 y=7..9、x=3..5；黑方为 y=0..2、x=3..5。
        in_own_palace = (
            3 <= x1 <= 5
            and ((7 <= y1 <= 9) if piece.side == "red" else (0 <= y1 <= 2))
        )
        if in_own_palace:
            return ay + ax == 1

        # 九宫格外仍按普通兵卒规则：未过河只能向前，过河后可横走。
        forward = -1 if piece.side == "red" else 1
        if dy == forward and dx == 0:
            return True
        crossed = y1 <= 4 if piece.side == "red" else y1 >= 5
        return crossed and dy == 0 and ax == 1
    return False


# ---- Viewer-specific state snapshots ---------------------------------------
def public_board(game, viewer_side=None):
    """Expose only public piece fields; never send a hidden piece's true kind."""
    out = []
    for y, row in enumerate(game.board):
        line = []
        for x, p in enumerate(row):
            if p is None:
                line.append(None)
            else:
                # 自己的暗棋也不显示种类，避免破坏规则；客户端只知道颜色与朝下状态。
                line.append({"side": p.side, "face_up": p.face_up, "kind": p.kind if p.face_up else None})
        out.append(line)
    return out


def make_replay_frame(game):
    """Capture one public replay frame after the initial position or a successful move."""
    return {
        "board": public_board(game),
        "turn": game.turn,
        "winner": game.winner,
        "captured": copy.deepcopy(game.captured),
        "log": list(game.log),
        "last_move": copy.deepcopy(game.last_move),
    }


def replay_captured_for_viewer(captured, viewer_side):
    """Filter hidden captured-piece identities to the selected replay viewer."""
    return {
        side: [
            {
                "kind": item["kind"] if (item["face_up"] or side == viewer_side) else None,
                "side": item["side"],
                "face_up": item["face_up"],
            }
            for item in items
        ]
        for side, items in captured.items()
    }


def replay_frames_for_viewer(frames, viewer_side):
    """Remove the true identities of hidden captures the viewer is not entitled to know."""
    return [
        {
            **frame,
            "captured": replay_captured_for_viewer(frame.get("captured", {}), viewer_side),
        }
        for frame in frames
    ]


def replay_export_document(data, created_at):
    """Build a versioned, player-observation replay file for download or training."""
    frames = []
    for ply, frame in enumerate(data.get("frames", [])):
        last_move = copy.deepcopy(frame.get("last_move"))
        action = copy.deepcopy(frame.get("action"))
        if action is None and last_move:
            action = {
                "from": copy.deepcopy(last_move["from"]),
                "to": copy.deepcopy(last_move["to"]),
                "side": last_move.get("side"),
            }
        frames.append({
            "ply": ply,
            "action": action,
            "turn": frame.get("turn"),
            "winner": frame.get("winner"),
            "last_move": last_move,
            "board": copy.deepcopy(frame.get("board", [])),
            "captured": copy.deepcopy(frame.get("captured", {})),
            "log": list(frame.get("log", [])),
        })
    return {
        "format": REPLAY_FILE_FORMAT,
        "version": REPLAY_FILE_VERSION,
        "ruleset": REPLAY_RULESET,
        "privacy": "player_observation",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "game": {
            "room_code": data.get("room_code", "-----"),
            "players": copy.deepcopy(data.get("players", {})),
            "winner": data.get("winner"),
            "viewer_side": data.get("viewer_side"),
            "move_count": max(0, len(frames) - 1),
            "origin": data.get("origin", "server"),
            "recorded_at": data.get("recorded_at", created_at),
        },
        "frames": frames,
    }


def normalize_replay_piece(piece):
    """Validate one player-observation board cell from an uploaded replay."""
    if piece is None:
        return None
    if not isinstance(piece, dict):
        raise ValueError("回放棋盘格格式无效")
    side = piece.get("side")
    face_up = piece.get("face_up")
    if side not in ("red", "black") or not isinstance(face_up, bool):
        raise ValueError("回放棋子缺少有效的阵营或翻面状态")
    kind = piece.get("kind")
    if not face_up:
        if kind is not None:
            raise ValueError("回放文件不得包含未翻面棋子的真实棋种")
        return {"side": side, "face_up": False, "kind": None}
    if kind not in NAMES or (side == "red") != kind.isupper():
        raise ValueError("回放中的明棋棋种与阵营不匹配")
    return {"side": side, "face_up": True, "kind": kind}


def normalize_replay_last_move(last_move):
    """Validate and normalize a move marker/action coordinate pair."""
    if last_move is None:
        return None
    if not isinstance(last_move, dict):
        raise ValueError("回放着法格式无效")
    try:
        from_square = last_move["from"]
        to_square = last_move["to"]
        y1, x1 = from_square["y"], from_square["x"]
        y2, x2 = to_square["y"], to_square["x"]
    except (TypeError, ValueError, KeyError):
        raise ValueError("回放着法坐标无效") from None
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (y1, x1, y2, x2)
    ):
        raise ValueError("回放着法坐标必须是整数")
    if not (in_bounds(y1, x1) and in_bounds(y2, x2)):
        raise ValueError("回放着法坐标超出棋盘")
    normalized = {
        "from": {"y": y1, "x": x1},
        "to": {"y": y2, "x": x2},
    }
    side = last_move.get("side")
    if side is not None:
        if side not in ("red", "black"):
            raise ValueError("回放着法阵营无效")
        normalized["side"] = side
    move_id = last_move.get("id")
    if isinstance(move_id, int) and not isinstance(move_id, bool):
        normalized["id"] = move_id
    return normalized


def normalize_uploaded_replay(document):
    """Validate an uploaded replay file and convert it to the viewer response shape."""
    if not isinstance(document, dict):
        raise ValueError("回放文件顶层必须是 JSON 对象")
    if document.get("format") != REPLAY_FILE_FORMAT:
        raise ValueError("不是本项目的回放文件")
    version = document.get("version")
    if isinstance(version, bool) or version != REPLAY_FILE_VERSION:
        raise ValueError("不支持的回放文件版本")
    if document.get("ruleset") != REPLAY_RULESET:
        raise ValueError("回放规则版本不兼容")
    if document.get("privacy") != "player_observation":
        raise ValueError("回放隐私类型不兼容")
    game_meta = document.get("game")
    raw_frames = document.get("frames")
    if not isinstance(game_meta, dict) or not isinstance(raw_frames, list):
        raise ValueError("回放文件缺少 game 或 frames")
    if not 1 <= len(raw_frames) <= MAX_REPLAY_FRAMES:
        raise ValueError(f"回放帧数必须在 1 到 {MAX_REPLAY_FRAMES} 之间")

    winner = game_meta.get("winner")
    if winner not in (None, "red", "black", "draw"):
        raise ValueError("回放结果无效")
    viewer_side = game_meta.get("viewer_side")
    if viewer_side not in ("red", "black"):
        raise ValueError("回放视角无效")
    room_code = str(game_meta.get("room_code") or "UPLOAD").strip().upper()[:16]
    players = game_meta.get("players") if isinstance(game_meta.get("players"), dict) else {}
    normalized_players = {}
    for side, fallback in (("red", "红方"), ("black", "黑方")):
        normalized_players[side] = clean_nickname(players.get(side)) or fallback

    frames = []
    for index, raw_frame in enumerate(raw_frames):
        if not isinstance(raw_frame, dict):
            raise ValueError(f"第 {index} 帧格式无效")
        board_data = raw_frame.get("board")
        if not isinstance(board_data, list) or len(board_data) != 10:
            raise ValueError(f"第 {index} 帧棋盘行数无效")
        board = []
        for row in board_data:
            if not isinstance(row, list) or len(row) != 9:
                raise ValueError(f"第 {index} 帧棋盘列数无效")
            board.append([normalize_replay_piece(piece) for piece in row])

        raw_captured = raw_frame.get("captured", {})
        if not isinstance(raw_captured, dict):
            raise ValueError(f"第 {index} 帧吃子列表无效")
        captured = {}
        for capturer in ("red", "black"):
            items = raw_captured.get(capturer, [])
            if not isinstance(items, list):
                raise ValueError(f"第 {index} 帧吃子列表格式无效")
            captured[capturer] = []
            for item in items:
                if not isinstance(item, dict) or item.get("side") not in ("red", "black"):
                    raise ValueError(f"第 {index} 帧吃子记录无效")
                face_up = item.get("face_up")
                if not isinstance(face_up, bool):
                    raise ValueError(f"第 {index} 帧吃子翻面状态无效")
                kind = item.get("kind")
                if kind is not None and (
                    kind not in NAMES or (item["side"] == "red") != kind.isupper()
                ):
                    raise ValueError(f"第 {index} 帧吃子棋种无效")
                captured[capturer].append({
                    "kind": kind,
                    "side": item["side"],
                    "face_up": face_up,
                })

        turn = raw_frame.get("turn")
        frame_winner = raw_frame.get("winner")
        if turn not in ("red", "black"):
            raise ValueError(f"第 {index} 帧回合无效")
        if frame_winner not in (None, "red", "black", "draw"):
            raise ValueError(f"第 {index} 帧结果无效")
        if frame_winner is not None and index != len(raw_frames) - 1:
            raise ValueError(f"第 {index} 帧在终局前包含结果")
        raw_log = raw_frame.get("log", [])
        if not isinstance(raw_log, list) or any(not isinstance(line, str) for line in raw_log):
            raise ValueError(f"第 {index} 帧日志格式无效")
        if any(len(line) > 2000 for line in raw_log):
            raise ValueError(f"第 {index} 帧日志过长")
        last_move = normalize_replay_last_move(raw_frame.get("last_move"))
        action = normalize_replay_last_move(raw_frame.get("action"))
        if index == 0 and (last_move or action):
            raise ValueError("初始回放帧不能包含着法")
        if action is None and last_move:
            action = {
                "from": copy.deepcopy(last_move["from"]),
                "to": copy.deepcopy(last_move["to"]),
                "side": last_move.get("side"),
            }
        elif last_move is None and action:
            last_move = copy.deepcopy(action)
        if index > 0:
            if not action or action.get("side") not in ("red", "black"):
                raise ValueError(f"第 {index} 帧缺少有效着法")
            if not last_move or (
                action["from"] != last_move["from"]
                or action["to"] != last_move["to"]
                or ("side" in last_move and last_move["side"] != action["side"])
            ):
                raise ValueError(f"第 {index} 帧 action 与 last_move 不一致")
            previous_board = frames[-1]["board"]
            from_y, from_x = action["from"]["y"], action["from"]["x"]
            to_y, to_x = action["to"]["y"], action["to"]["x"]
            moving_piece = previous_board[from_y][from_x]
            if not moving_piece or moving_piece["side"] != action["side"]:
                raise ValueError(f"第 {index} 帧着法起点棋子无效")
            if board[from_y][from_x] is not None:
                raise ValueError(f"第 {index} 帧着法起点未清空")
            landed_piece = board[to_y][to_x]
            if not landed_piece or landed_piece["side"] != action["side"]:
                raise ValueError(f"第 {index} 帧着法终点棋子无效")
        frames.append({
            "board": board,
            "turn": turn,
            "winner": frame_winner,
            "captured": captured,
            "log": list(raw_log),
            "last_move": last_move,
            "action": action,
        })

    if winner is not None:
        frames[-1]["winner"] = winner
    elif frames[-1]["winner"] is not None:
        raise ValueError("未结束对局的最终帧不能包含结果")
    return {
        "room_code": room_code,
        "winner": winner,
        "viewer_side": viewer_side,
        "players": normalized_players,
        "frames": frames,
        "origin": "uploaded",
        "recorded_at": str(game_meta.get("recorded_at") or "")[:64] or None,
    }


def snapshot(game, sid):
    """Create the state payload for one socket, with capture knowledge filtered.

    The current side is derived from the live socket ID. Keep this function as the
    privacy boundary between the complete server-side board and browser-visible state.
    """
    side = next((s for s, p in game.players.items() if p["sid"] == sid), None)
    return {
        "code": game.code,
        "board": public_board(game, side),
        "side": side,
        "turn": game.turn,
        "winner": game.winner,
        "players": {s: p["nickname"] for s, p in game.players.items()},
        "captured": {
            # s 是吃子方。被吃时仍朝下的棋子，只向吃子方公开真实种类；
            # 被吃方仍只能看到“暗子”。原本已经翻开的棋子双方都能识别。
            s: [{"kind": item["kind"] if (item["face_up"] or side == s) else None,
                 "side": item["side"], "face_up": item["face_up"]} for item in arr]
            for s, arr in game.captured.items()
        },
        "log": game.log[-30:],
        "last_move": game.last_move,
        "can_undo": len(game.history) >= 2,
        "pending_request": game.pending_request,
        "request_locks": {kind: (seq == game.turn_seq) for kind, seq in game.denied_request_turns.items()},
        "started": len(game.players) == 2,
    }


def broadcast(game):
    """Send a separately filtered snapshot to each currently connected player."""
    # During restart recovery, seats that have not reconnected yet have sid=None.
    for player in game.players.values():
        sid = player.get("sid")
        if sid:
            socketio.emit("state", snapshot(game, sid), to=sid)


# ---- Player / room lookup and connection restoration ------------------------
def find_player_room(sid):
    """Find a room by the current, ephemeral Socket.IO connection ID."""
    for game in rooms.values():
        for side, player in game.players.items():
            if player["sid"] == sid:
                return game, side
    return None, None


def find_player_room_by_id(player_id):
    """Find a reserved seat by the stable ID stored in the Flask session."""
    if not player_id:
        return None, None
    for game in rooms.values():
        for side, player in game.players.items():
            if player.get("player_id") == player_id:
                return game, side
    return None, None


# Restore unfinished rooms and their player reservations when the process starts.
load_persisted_rooms()


@socketio.on("connect")
def restore_player_room(auth=None):
    """Restore the player's persisted room and seat when the browser reconnects."""
    player_id = session.get("player_id")
    if not player_id:
        return
    with lock:
        record = player_record(player_id)
        game = None
        side = None
        if record and record.get("in_game") and record.get("room_code"):
            game = rooms.get(record["room_code"])
            side = record.get("side")
            if not game or side not in game.players or game.players[side].get("player_id") != player_id:
                game, side = find_player_room_by_id(player_id)
        else:
            game, side = find_player_room_by_id(player_id)
        if not game or side is None or game.winner:
            if record and record.get("in_game"):
                clear_player_game_status(player_id)
            return
        # Socket.IO allocates a new sid after refresh/reconnect; keep the reserved seat.
        game.players[side]["sid"] = request.sid
        join_room(game.code)
        emit("room_joined", {"code": game.code, "side": side})
        emit("state", snapshot(game, request.sid))


# ---- HTTP endpoints --------------------------------------------------------
@app.get("/")
def index():
    """Serve the browser client."""
    return render_template("index.html")


@app.get("/api/me")
def me():
    """Return the nickname and active-game marker for this signed session."""
    nickname = session.get("nickname")
    record = player_record(session.get("player_id"))
    return jsonify({
        "nickname": nickname,
        "in_game": bool(record and record.get("in_game")),
        "room_code": record.get("room_code") if record else None,
        "side": record.get("side") if record else None,
    })


@app.post("/api/nickname")
def save_nickname_http():
    """Persist nickname/session identity before the client reconnects over Socket.IO."""
    data = request.get_json(silent=True) or {}
    nickname = clean_nickname(data.get("nickname"))
    if not nickname:
        return jsonify({"error": "昵称不能为空，且最多 16 个字符。"}), 400
    session["nickname"] = nickname
    player_id = session.get("player_id")
    if not player_id:
        player_id = secrets.token_urlsafe(18)
        session["player_id"] = player_id
    # Update the nickname without replacing the row, so an active-game marker survives login.
    conn = db()
    try:
        conn.execute(
            """INSERT INTO players(sid, nickname) VALUES (?, ?)
               ON CONFLICT(sid) DO UPDATE SET nickname=excluded.nickname""",
            (player_id, nickname),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"nickname": nickname})


@app.post("/api/replays/import")
def import_replay():
    """Validate an uploaded replay file and return it for immediate viewing."""
    player_id = session.get("player_id")
    if not player_id:
        return jsonify({"error": "请先设置昵称。"}), 403
    uploaded = request.files.get("file")
    if uploaded is None:
        return jsonify({"error": "请选择要上传的回放文件。"}), 400
    raw = uploaded.read(MAX_REPLAY_FILE_BYTES + 1)
    if len(raw) > MAX_REPLAY_FILE_BYTES:
        return jsonify({"error": "回放文件不能超过 8 MB。"}), 413
    try:
        document = json.loads(raw)
        data = normalize_uploaded_replay(document)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return jsonify({"error": "回放文件不是有效的 UTF-8 JSON。"}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "message": "对局记录已载入。",
        "replay": data,
    })


@app.get("/api/replays/offers/<offer_id>/export")
def export_finished_replay(offer_id):
    """Download the just-finished match from the authenticated participant's view."""
    player_id = session.get("player_id")
    if not player_id:
        return jsonify({"error": "请先设置昵称。"}), 403
    with lock:
        offer = pending_replays.get(offer_id)
        viewer_side = participant_side(offer, player_id) if offer else None
        if not offer or viewer_side is None:
            return jsonify({"error": "该对局记录已失效或无权下载。"}), 404
        data = {
            "room_code": offer["room_code"],
            "winner": offer["winner"],
            "viewer_side": viewer_side,
            "players": {
                side: player["nickname"]
                for side, player in offer["players"].items()
            },
            "frames": replay_frames_for_viewer(offer["frames"], viewer_side),
            "origin": "server",
            "recorded_at": offer["created_at"],
        }
        mark_replay_offer_response(offer_id, offer, player_id)
    payload = json.dumps(
        replay_export_document(data, offer["created_at"]),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    response = app.response_class(payload, mimetype="application/json")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="xiangqi-replay-{offer_id}.json"'
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/replays/offers/<offer_id>/dismiss")
def dismiss_finished_replay(offer_id):
    """Dismiss the post-game download prompt and release its in-memory copy."""
    player_id = session.get("player_id")
    if not player_id:
        return jsonify({"error": "请先设置昵称。"}), 403
    with lock:
        offer = pending_replays.get(offer_id)
        if not offer or participant_side(offer, player_id) is None:
            return jsonify({"error": "该对局记录已失效。"}), 404
        mark_replay_offer_response(offer_id, offer, player_id)
    return jsonify({"message": "已关闭下载提示。"})


@app.errorhandler(413)
def request_too_large(_error):
    """Return a JSON error when an uploaded replay exceeds the request limit."""
    return jsonify({"error": "上传内容过大，回放文件不能超过 8 MB。"}), 413


# ---- Socket.IO event handlers ----------------------------------------------
@socketio.on("set_nickname")
def set_nickname(data):
    """Socket event counterpart used after the session cookie is refreshed."""
    nickname = clean_nickname((data or {}).get("nickname"))
    if not nickname:
        emit("notice", {"message": "昵称不能为空，且最多 16 个字符。"})
        return
    session["nickname"] = nickname
    player_id = session.get("player_id")
    if not player_id:
        player_id = secrets.token_urlsafe(18)
        session["player_id"] = player_id
    conn = db()
    try:
        conn.execute(
            """INSERT INTO players(sid, nickname) VALUES (?, ?)
               ON CONFLICT(sid) DO UPDATE SET nickname=excluded.nickname""",
            (player_id, nickname),
        )
        conn.commit()
    finally:
        conn.close()
    emit("nickname_ok", {"nickname": nickname})


@socketio.on("create_room")
def create_room():
    """Create a room; the creator is assigned Red and becomes the first player."""
    nickname = session.get("nickname")
    if not nickname:
        emit("notice", {"message": "请先设置昵称。"})
        return
    with lock:
        if find_player_room(request.sid)[0]:
            emit("notice", {"message": "你已经在一个房间中。"})
            return
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
        while code in rooms:
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
        game = GameRoom(code, new_board())
        game.players["red"] = {
            "sid": request.sid,
            "player_id": session.get("player_id"),
            "nickname": nickname,
        }
        game.replay_frames = [make_replay_frame(game)]
        rooms[code] = game
        persist_game(game)
        join_room(code)
        emit("room_joined", {"code": code, "side": "red"})
        broadcast(game)


@socketio.on("join_room")
def join_game(data):
    """Join an open room as the other side; rooms hold at most two players."""
    nickname = session.get("nickname")
    code = str((data or {}).get("code", "")).strip().upper()
    if not nickname:
        emit("notice", {"message": "请先设置昵称。"})
        return
    with lock:
        if find_player_room(request.sid)[0]:
            emit("notice", {"message": "你已经在一个房间中。"})
            return
        game = rooms.get(code)
        if not game:
            emit("notice", {"message": "房间不存在。"})
            return
        if len(game.players) >= 2:
            emit("notice", {"message": "房间已满。"})
            return
        side = "black" if "red" in game.players else "red"
        game.players[side] = {
            "sid": request.sid,
            "player_id": session.get("player_id"),
            "nickname": nickname,
        }
        persist_game(game)
        join_room(code)
        emit("room_joined", {"code": code, "side": side})
        broadcast(game)


def replay_offer_summary(offer):
    """Return the non-sensitive metadata sent in the post-game replay prompt."""
    return {
        "id": offer["id"],
        "room_code": offer["room_code"],
        "winner": offer["winner"],
        "players": {
            side: player["nickname"]
            for side, player in offer["players"].items()
        },
        "move_count": max(0, len(offer["frames"]) - 1),
    }


def create_replay_offer(game):
    """Keep a finished match in memory until each participant chooses whether to save it."""
    if len(game.players) != 2:
        return None
    if not game.replay_frames:
        game.replay_frames = [make_replay_frame(game)]
    offer_id = secrets.token_urlsafe(12)
    offer = {
        "id": offer_id,
        "room_code": game.code,
        "winner": game.winner,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "players": {
            side: {
                "player_id": player.get("player_id"),
                "nickname": player.get("nickname", "玩家"),
            }
            for side, player in game.players.items()
        },
        "frames": copy.deepcopy(game.replay_frames),
        "responses": set(),
    }
    while len(pending_replays) >= MAX_PENDING_REPLAYS:
        pending_replays.pop(next(iter(pending_replays)), None)
    pending_replays[offer_id] = offer
    return offer


def participant_side(offer, player_id):
    """Return the participant's side for one finished-match export offer."""
    return next(
        (
            side
            for side, player in offer["players"].items()
            if player.get("player_id") == player_id
        ),
        None,
    )


def mark_replay_offer_response(offer_id, offer, player_id):
    """Release a finished replay after both participants download or dismiss it."""
    offer["responses"].add(player_id)
    participant_ids = {
        player.get("player_id")
        for player in offer["players"].values()
        if player.get("player_id")
    }
    if participant_ids and participant_ids.issubset(offer["responses"]):
        pending_replays.pop(offer_id, None)


def finish_game(game, result, message=None, state_already_broadcast=False):
    """Finalize a match, offer its replay, and release durable active-game state."""
    game.winner = result
    game.pending_request = None
    if message:
        game.log.append(message)
    if game.replay_frames:
        game.replay_frames[-1]["winner"] = result
        game.replay_frames[-1]["log"] = list(game.log)
    if not state_already_broadcast:
        persist_game(game)
        broadcast(game)
    offer = create_replay_offer(game)
    code = game.code
    for player in list(game.players.values()):
        clear_player_game_status(player.get("player_id"))
        if player.get("sid"):
            socketio.emit(
                "room_ended",
                {
                    "code": code,
                    "winner": result,
                    "replay_offer": replay_offer_summary(offer) if offer else None,
                },
                to=player["sid"],
            )
    rooms.pop(code, None)
    delete_persisted_game(code)


def opponent_side(side):
    """Return the other side code; call only with a valid side."""
    return "black" if side == "red" else "red"


def clone_board(board):
    """Create a JSON-compatible board copy for an undo snapshot."""
    return [[None if p is None else {
        "kind": p.kind, "side": p.side, "face_up": p.face_up, "move_kind": p.move_kind
    } for p in row] for row in board]


def restore_board(data):
    """Rehydrate a board snapshot into Piece instances."""
    return [[None if p is None else Piece(
        p["kind"], p["side"], p.get("face_up", False), p.get("move_kind")
    ) for p in row] for row in data]


def notify_request_result(game, requester_side, request_type, accepted, message):
    """Send a request outcome only to the player who initiated that request."""
    player = game.players.get(requester_side)
    if player and player.get("sid"):
        socketio.emit("request_result", {
            "type": request_type, "accepted": accepted, "message": message
        }, to=player["sid"])


@socketio.on("request_undo")
def request_undo():
    """Open an undo request; acceptance rolls back the latest two plies."""
    with lock:
        game, side = find_player_room(request.sid)
        if not game or not side:
            emit("notice", {"message": "你不在对局中。"}); return
        if game.winner or len(game.players) != 2:
            emit("notice", {"message": "当前无法申请悔棋。"}); return
        if side != game.turn:
            emit("notice", {"message": "只能在自己的回合申请悔棋。"}); return
        if len(game.history) < 2 or not game.last_move:
            emit("notice", {"message": "至少需要双方各完成一步行棋后才能悔棋。"}); return
        if game.pending_request:
            emit("notice", {"message": "当前已有申请等待处理。"}); return
        if game.denied_request_turns.get("undo") == game.turn_seq:
            emit("notice", {"message": "悔棋申请本回合已被拒绝，不能再次申请。"}); return
        game.pending_request = {
            "id": secrets.token_urlsafe(10), "type": "undo", "from_side": side,
            "to_side": opponent_side(side), "turn_seq": game.turn_seq,
        }
        persist_game(game); broadcast(game)


@socketio.on("request_draw")
def request_draw():
    """Open a draw request, subject to turn and same-turn rejection rules."""
    with lock:
        game, side = find_player_room(request.sid)
        if not game or not side:
            emit("notice", {"message": "你不在对局中。"}); return
        if game.winner or len(game.players) != 2:
            emit("notice", {"message": "当前无法申请平局。"}); return
        if side != game.turn:
            emit("notice", {"message": "只能在自己的回合申请平局。"}); return
        if game.pending_request:
            emit("notice", {"message": "当前已有申请等待处理。"}); return
        if game.denied_request_turns.get("draw") == game.turn_seq:
            emit("notice", {"message": "平局申请本回合已被拒绝，不能再次申请。"}); return
        game.pending_request = {
            "id": secrets.token_urlsafe(10), "type": "draw", "from_side": side,
            "to_side": opponent_side(side), "turn_seq": game.turn_seq,
        }
        persist_game(game); broadcast(game)


@socketio.on("respond_request")
def respond_request(data):
    """Resolve a pending undo/draw request after validating its ID and recipient."""
    data = data or {}
    accepted = bool(data.get("accepted"))
    request_id = str(data.get("id", ""))
    with lock:
        game, side = find_player_room(request.sid)
        pending = game.pending_request if game else None
        if not game or not pending or pending.get("id") != request_id or pending.get("to_side") != side:
            emit("notice", {"message": "该申请已失效或不属于你处理。"}); return
        request_type = pending["type"]
        requester_side = pending["from_side"]
        game.pending_request = None
        if not accepted:
            game.denied_request_turns[request_type] = pending.get("turn_seq", game.turn_seq)
            message = "对方拒绝了悔棋申请。" if request_type == "undo" else "对方拒绝了平局申请。"
            notify_request_result(game, requester_side, request_type, False, message)
            game.log.append(message)
            persist_game(game); broadcast(game)
            socketio.emit("notice", {"message": "你已拒绝该申请。"}, to=request.sid)
            return
        if request_type == "draw":
            notify_request_result(game, requester_side, request_type, True, "对方同意平局，本局和棋。")
            finish_game(game, "draw", "双方同意和棋。")
            return
        if len(game.history) < 2:
            notify_request_result(game, requester_side, request_type, False, "当前不足两步行棋，无法撤销双方各一步。")
            persist_game(game); broadcast(game); return
        # 每条历史记录保存的是对应那一步行棋前的状态。倒数第二条
        # 即最近两步中较早一步执行前的状态；恢复它并删除末尾两条记录，
        # 就能让双方各撤销最近一步，同时一起恢复棋盘、吃子、回合和日志。
        previous = game.history[-2]
        del game.history[-2:]
        game.board = restore_board(previous["board"])
        game.captured = previous["captured"]
        game.turn = previous["turn"]
        game.winner = previous["winner"]
        game.log = previous["log"]
        game.last_move = previous.get("last_move")
        del game.replay_frames[-2:]
        if not game.replay_frames:
            game.replay_frames = [make_replay_frame(game)]
        # 使用新的回合序号，避免悔棋后的状态沿用旧的拒绝限制。
        game.turn_seq += 1
        game.log.append("双方同意悔棋，双方各撤销最近一步，共撤销两步。")
        notify_request_result(
            game, requester_side, request_type, True,
            "对方同意悔棋，双方各撤销最近一步，共撤销两步。",
        )
        persist_game(game); broadcast(game)
        socketio.emit("notice", {"message": "已同意悔棋，双方各撤销最近一步，共撤销两步。"}, to=request.sid)


@socketio.on("surrender")
def surrender():
    """End the current match immediately and award the opponent the win."""
    with lock:
        game, side = find_player_room(request.sid)
        if not game or not side:
            emit("notice", {"message": "你不在对局中。"}); return
        if game.winner:
            emit("notice", {"message": "本局已经结束。"}); return
        if len(game.players) != 2:
            emit("notice", {"message": "等待另一位玩家加入后才能投降。"}); return
        winner = opponent_side(side)
        finish_game(game, winner, f"{game.players[side]['nickname']} 投降，{game.players[winner]['nickname']} 获胜。")


@socketio.on("move")
def move(data):
    """Validate and apply one move, append the pre-move snapshot, then broadcast.

    All game-changing state is mutated under ``lock`` and persisted before the new
    snapshot is emitted, so reconnecting clients see the durable latest position.
    """
    sid = request.sid
    with lock:
        game, side = find_player_room(sid)
        if not game or side is None:
            emit("notice", {"message": "你不在任何房间中。"})
            return
        if len(game.players) != 2:
            emit("notice", {"message": "等待另一位玩家加入。"})
            return
        if game.winner:
            emit("notice", {"message": "本局已经结束。"})
            return
        if side != game.turn:
            emit("notice", {"message": "还没轮到你走棋。"})
            return
        if game.pending_request:
            emit("notice", {"message": "请先等待当前申请处理完毕。"})
            return
        try:
            y1, x1 = int(data["from"]["y"]), int(data["from"]["x"])
            y2, x2 = int(data["to"]["y"]), int(data["to"]["x"])
        except (TypeError, ValueError, KeyError):
            emit("notice", {"message": "落子数据无效。"})
            return
        if not (in_bounds(y1, x1) and in_bounds(y2, x2)):
            emit("notice", {"message": "坐标超出棋盘。"})
            return
        piece, target = game.board[y1][x1], game.board[y2][x2]
        if not piece or piece.side != side:
            emit("notice", {"message": "只能移动自己的棋子。"})
            return
        if not legal_move(game.board, y1, x1, y2, x2, piece, target):
            rule_name = "棋子本身" if piece.face_up else "初始位置对应"
            emit("notice", {"message": f"不符合{rule_name}的走法，或路径被阻挡。"})
            return
        # 保存本步行棋前的完整状态；悔棋时会一次恢复至最近两步之前。
        game.history.append({
            "board": clone_board(game.board),
            "captured": json.loads(json.dumps(game.captured, ensure_ascii=False)),
            "turn": game.turn, "winner": game.winner,
            "log": list(game.log), "last_move": game.last_move,
        })
        game.move_seq += 1
        game.last_move = {"from": {"y": y1, "x": x1}, "to": {"y": y2, "x": x2},
                          "side": side, "id": game.move_seq}
        captured_name = "暗子"
        if target:
            game.captured[side].append({"kind": target.kind, "side": target.side, "face_up": target.face_up})
            if target.face_up:
                captured_name = NAMES[target.kind]
            if target.kind.lower() == "k":
                game.winner = side
        piece.face_up = True
        game.board[y2][x2] = piece
        game.board[y1][x1] = None
        mover_name = NAMES[piece.kind]
        if target:
            game.log.append(f"{game.players[side]['nickname']}（{'红方' if side == 'red' else '黑方'}）以 {mover_name} 吃掉 {captured_name}")
        else:
            game.log.append(f"{game.players[side]['nickname']}（{'红方' if side == 'red' else '黑方'}）移动 {mover_name}")
        if not game.winner:
            game.turn = "black" if game.turn == "red" else "red"
        else:
            game.log.append(f"{game.players[side]['nickname']} 获胜！")
        game.turn_seq += 1
        game.replay_frames.append(make_replay_frame(game))
        # Save the latest board before broadcasting, so a server restart does not
        # discard moves or the room's player-to-seat mapping.
        persist_game(game)
        broadcast(game)
        if game.winner:
            # 棋盘吃掉将帅后直接结束对局；状态已经广播，不再重复发送。
            finish_game(game, game.winner, state_already_broadcast=True)


@socketio.on("disconnect")
def disconnected():
    """Retain reserved seats on disconnect; refresh/reconnect reattaches by player ID."""
    # A refresh briefly disconnects the old sid. Keep the seat reserved;
    # the connect handler reattaches it using the persistent player_id.
    pass


# ---- Direct development/LAN entry point -----------------------------------
if __name__ == "__main__":
    server_config = load_config()
    print(
        f"暗棋服务器启动。监听 {server_config['ip']}:{server_config['port']}；"
        f"局域网玩家访问 http://服务器IP:{server_config['port']}"
    )
    try:
        socketio.run(
            app,
            host=server_config["ip"],
            port=server_config["port"],
            debug=False,
        )
    except KeyboardInterrupt:
        print("\n正在关闭服务器...")
    finally:
        print("服务器已退出。")
