from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "tactics.db"))).resolve()

DEFAULT_MAPS = ["Mirage", "Dust2", "Ancient", "Nuke", "Overpass", "Anubis"]
SIDES = {"T", "CT"}
ECONOMY_OPTIONS = {"手枪局", "eco局", "半起局", "反eco局", "长枪局", "通用", "自定义"}
TACTIC_TAG_OPTIONS = {"常规默认", "非常规", "爆弹", "rush", "自定义"}


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS maps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tactics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                map_id INTEGER NOT NULL REFERENCES maps(id) ON DELETE CASCADE,
                side TEXT NOT NULL CHECK(side IN ('T', 'CT')),
                title TEXT NOT NULL,
                economy TEXT NOT NULL,
                economy_custom TEXT NOT NULL DEFAULT '',
                tactic_tags TEXT NOT NULL DEFAULT '[]',
                tactic_custom_tags TEXT NOT NULL DEFAULT '[]',
                early_commands TEXT NOT NULL DEFAULT '[]',
                decision_tree TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                map_id INTEGER NOT NULL REFERENCES maps(id) ON DELETE CASCADE,
                side TEXT NOT NULL CHECK(side IN ('T', 'CT')),
                body TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        count = conn.execute("SELECT COUNT(*) FROM maps").fetchone()[0]
        if count == 0:
            stamp = now()
            conn.executemany(
                "INSERT INTO maps (name, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?)",
                [(name, index + 1, stamp, stamp) for index, name in enumerate(DEFAULT_MAPS)],
            )


def decode_json(value: str, fallback):
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def normalize_command(command: dict, index: int) -> dict:
    try:
        priority = int(command.get("priority", 1))
    except (TypeError, ValueError):
        priority = 1
    return {
        "id": str(command.get("id") or f"cmd-{int(time.time() * 1000)}-{index}"),
        "priority": max(1, priority),
        "text": str(command.get("text", "")).strip(),
    }


def normalize_commands(commands) -> list[dict]:
    if not isinstance(commands, list):
        return []
    normalized = [normalize_command(item if isinstance(item, dict) else {}, index) for index, item in enumerate(commands)]
    normalized = [item for item in normalized if item["text"]]
    return sorted(normalized, key=lambda item: (item["priority"], item["id"]))


def empty_decision_node() -> dict:
    return {
        "id": f"node-{int(time.time() * 1000)}",
        "condition": "",
        "thenAction": "",
        "children": [],
    }


def normalize_decision_node(node) -> dict:
    if not isinstance(node, dict):
        node = {}
    legacy_children = node.get("thenChildren", [])
    children = node.get("children", legacy_children)
    normalized = {
        "id": str(node.get("id") or f"node-{int(time.time() * 1000)}"),
        "condition": str(node.get("condition", "")).strip(),
        "thenAction": str(node.get("thenAction", "")).strip(),
        "children": [],
    }
    if isinstance(children, list):
        normalized["children"] = [normalize_decision_node(child) for child in children]
    return normalized


def normalize_decision_tree(tree) -> list[dict]:
    if isinstance(tree, list):
        return [normalize_decision_node(node) for node in tree]
    if isinstance(tree, dict):
        nodes = tree.get("nodes")
        if isinstance(nodes, list):
            return [normalize_decision_node(node) for node in nodes]
        if tree.get("condition") or tree.get("thenAction") or tree.get("children") or tree.get("thenChildren"):
            return [normalize_decision_node(tree)]
    return []


def tactic_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "map_id": row["map_id"],
        "side": row["side"],
        "title": row["title"],
        "economy": row["economy"],
        "economy_custom": row["economy_custom"],
        "tactic_tags": decode_json(row["tactic_tags"], []),
        "tactic_custom_tags": decode_json(row["tactic_custom_tags"], []),
        "early_commands": normalize_commands(decode_json(row["early_commands"], [])),
        "decision_tree": normalize_decision_tree(decode_json(row["decision_tree"], [])),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def note_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "map_id": row["map_id"],
        "side": row["side"],
        "body": row["body"],
        "sort_order": row["sort_order"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def map_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    tactic_counts = {
        side: conn.execute("SELECT COUNT(*) FROM tactics WHERE map_id = ? AND side = ?", (row["id"], side)).fetchone()[0]
        for side in SIDES
    }
    note_counts = {
        side: conn.execute("SELECT COUNT(*) FROM notes WHERE map_id = ? AND side = ?", (row["id"], side)).fetchone()[0]
        for side in SIDES
    }
    return {
        "id": row["id"],
        "name": row["name"],
        "sort_order": row["sort_order"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "counts": {
            "t_tactics": tactic_counts["T"],
            "ct_tactics": tactic_counts["CT"],
            "t_notes": note_counts["T"],
            "ct_notes": note_counts["CT"],
        },
    }


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def require_map(conn: sqlite3.Connection, map_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM maps WHERE id = ?", (map_id,)).fetchone()
    if row is None:
        raise ApiError(404, "地图不存在")
    return row


def require_tactic(conn: sqlite3.Connection, tactic_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tactics WHERE id = ?", (tactic_id,)).fetchone()
    if row is None:
        raise ApiError(404, "战术不存在")
    return row


def require_note(conn: sqlite3.Connection, note_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise ApiError(404, "注意事项不存在")
    return row


def clean_name(name: str) -> str:
    value = re.sub(r"\s+", " ", str(name or "")).strip()
    if not value:
        raise ApiError(400, "地图名称不能为空")
    if len(value) > 60:
        raise ApiError(400, "地图名称不能超过 60 个字符")
    return value


def validate_side(side: str) -> str:
    side = str(side or "").upper()
    if side not in SIDES:
        raise ApiError(400, "阵营必须是 T 或 CT")
    return side


def validate_tactic_payload(data: dict) -> dict:
    title = str(data.get("title", "")).strip()
    if not title:
        raise ApiError(400, "战术名称不能为空")
    if len(title) > 100:
        raise ApiError(400, "战术名称不能超过 100 个字符")

    economy = str(data.get("economy", "")).strip()
    if economy not in ECONOMY_OPTIONS:
        raise ApiError(400, "请选择经济属性")

    economy_custom = str(data.get("economy_custom", "")).strip()
    if economy == "自定义" and not economy_custom:
        raise ApiError(400, "请输入自定义经济属性")

    tactic_tags = data.get("tactic_tags", [])
    tactic_custom_tags = data.get("tactic_custom_tags", [])
    if not isinstance(tactic_tags, list):
        tactic_tags = []
    if not isinstance(tactic_custom_tags, list):
        tactic_custom_tags = []

    tactic_tags = [str(item).strip() for item in tactic_tags if str(item).strip() in TACTIC_TAG_OPTIONS]
    tactic_custom_tags = [str(item).strip() for item in tactic_custom_tags if str(item).strip()]
    if "自定义" in tactic_tags and not tactic_custom_tags:
        raise ApiError(400, "请输入自定义战术属性")
    if not tactic_tags and not tactic_custom_tags:
        raise ApiError(400, "至少选择一个战术属性")

    commands = normalize_commands(data.get("early_commands", []))
    decision_tree = normalize_decision_tree(data.get("decision_tree", []))

    return {
        "title": title,
        "economy": economy,
        "economy_custom": economy_custom if economy == "自定义" else "",
        "tactic_tags": tactic_tags,
        "tactic_custom_tags": tactic_custom_tags,
        "early_commands": commands,
        "decision_tree": decision_tree,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "CS2Tactics/1.0"

    def log_message(self, format: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), format % args))

    def do_GET(self) -> None:
        self.handle_request("GET")

    def do_POST(self) -> None:
        self.handle_request("POST")

    def do_PUT(self) -> None:
        self.handle_request("PUT")

    def do_DELETE(self) -> None:
        self.handle_request("DELETE")

    def handle_request(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path.startswith("/api/"):
                self.handle_api(method, path)
            elif method == "GET":
                self.serve_static(path)
            else:
                raise ApiError(405, "Method not allowed")
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except sqlite3.IntegrityError as exc:
            message = "数据已存在或违反约束"
            if "UNIQUE" in str(exc).upper():
                message = "名称已存在"
            self.send_json({"error": message}, 409)
        except Exception as exc:
            self.send_json({"error": f"服务器错误: {exc}"}, 500)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError(400, "JSON 格式错误")
        if not isinstance(value, dict):
            raise ApiError(400, "请求体必须是对象")
        return value

    def send_json(self, value, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_static(self, path: str) -> None:
        if path in ("", "/"):
            file_path = STATIC_DIR / "index.html"
        else:
            relative_path = path.removeprefix("/static/").lstrip("/")
            requested = (STATIC_DIR / relative_path).resolve()
            if not str(requested).startswith(str(STATIC_DIR.resolve())):
                raise ApiError(403, "Forbidden")
            file_path = requested

        if not file_path.exists() or not file_path.is_file():
            file_path = STATIC_DIR / "index.html"

        content = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if file_path.suffix in {".html", ".css", ".js"}:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def handle_api(self, method: str, path: str) -> None:
        parts = [part for part in path.split("/") if part]

        if method == "GET" and parts == ["api", "health"]:
            self.send_json({"ok": True})
            return

        if method == "GET" and parts == ["api", "maps"]:
            with connect() as conn:
                rows = conn.execute("SELECT * FROM maps ORDER BY sort_order, id").fetchall()
                self.send_json({"maps": [map_to_dict(conn, row) for row in rows]})
            return

        if method == "POST" and parts == ["api", "maps"]:
            data = self.read_json()
            with connect() as conn:
                sort_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM maps").fetchone()[0]
                stamp = now()
                cursor = conn.execute(
                    "INSERT INTO maps (name, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (clean_name(data.get("name")), sort_order, stamp, stamp),
                )
                row = require_map(conn, cursor.lastrowid)
                self.send_json({"map": map_to_dict(conn, row)}, 201)
            return

        map_match = re.fullmatch(r"/api/maps/(\d+)", path)
        if map_match and method == "PUT":
            map_id = int(map_match.group(1))
            data = self.read_json()
            with connect() as conn:
                require_map(conn, map_id)
                conn.execute(
                    "UPDATE maps SET name = ?, updated_at = ? WHERE id = ?",
                    (clean_name(data.get("name")), now(), map_id),
                )
                row = require_map(conn, map_id)
                self.send_json({"map": map_to_dict(conn, row)})
            return

        if map_match and method == "DELETE":
            map_id = int(map_match.group(1))
            with connect() as conn:
                require_map(conn, map_id)
                conn.execute("DELETE FROM maps WHERE id = ?", (map_id,))
                self.send_json({"ok": True})
            return

        content_match = re.fullmatch(r"/api/maps/(\d+)/content", path)
        if content_match and method == "GET":
            map_id = int(content_match.group(1))
            with connect() as conn:
                row = require_map(conn, map_id)
                tactics = conn.execute(
                    "SELECT * FROM tactics WHERE map_id = ? ORDER BY updated_at DESC, id DESC", (map_id,)
                ).fetchall()
                notes = conn.execute(
                    "SELECT * FROM notes WHERE map_id = ? ORDER BY sort_order, id", (map_id,)
                ).fetchall()
                self.send_json(
                    {
                        "map": map_to_dict(conn, row),
                        "tactics": {
                            "T": [tactic_to_dict(item) for item in tactics if item["side"] == "T"],
                            "CT": [tactic_to_dict(item) for item in tactics if item["side"] == "CT"],
                        },
                        "notes": {
                            "T": [note_to_dict(item) for item in notes if item["side"] == "T"],
                            "CT": [note_to_dict(item) for item in notes if item["side"] == "CT"],
                        },
                    }
                )
            return

        if method == "POST" and parts == ["api", "tactics"]:
            data = self.read_json()
            map_id = int(data.get("map_id", 0) or 0)
            side = validate_side(data.get("side"))
            payload = validate_tactic_payload(data)
            stamp = now()
            with connect() as conn:
                require_map(conn, map_id)
                cursor = conn.execute(
                    """
                    INSERT INTO tactics (
                        map_id, side, title, economy, economy_custom, tactic_tags,
                        tactic_custom_tags, early_commands, decision_tree, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        map_id,
                        side,
                        payload["title"],
                        payload["economy"],
                        payload["economy_custom"],
                        json.dumps(payload["tactic_tags"], ensure_ascii=False),
                        json.dumps(payload["tactic_custom_tags"], ensure_ascii=False),
                        json.dumps(payload["early_commands"], ensure_ascii=False),
                        json.dumps(payload["decision_tree"], ensure_ascii=False),
                        stamp,
                        stamp,
                    ),
                )
                row = require_tactic(conn, cursor.lastrowid)
                self.send_json({"tactic": tactic_to_dict(row)}, 201)
            return

        tactic_match = re.fullmatch(r"/api/tactics/(\d+)", path)
        if tactic_match and method == "PUT":
            tactic_id = int(tactic_match.group(1))
            data = self.read_json()
            payload = validate_tactic_payload(data)
            with connect() as conn:
                require_tactic(conn, tactic_id)
                conn.execute(
                    """
                    UPDATE tactics
                    SET title = ?, economy = ?, economy_custom = ?, tactic_tags = ?,
                        tactic_custom_tags = ?, early_commands = ?, decision_tree = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        payload["title"],
                        payload["economy"],
                        payload["economy_custom"],
                        json.dumps(payload["tactic_tags"], ensure_ascii=False),
                        json.dumps(payload["tactic_custom_tags"], ensure_ascii=False),
                        json.dumps(payload["early_commands"], ensure_ascii=False),
                        json.dumps(payload["decision_tree"], ensure_ascii=False),
                        now(),
                        tactic_id,
                    ),
                )
                row = require_tactic(conn, tactic_id)
                self.send_json({"tactic": tactic_to_dict(row)})
            return

        if tactic_match and method == "DELETE":
            tactic_id = int(tactic_match.group(1))
            with connect() as conn:
                require_tactic(conn, tactic_id)
                conn.execute("DELETE FROM tactics WHERE id = ?", (tactic_id,))
                self.send_json({"ok": True})
            return

        if method == "POST" and parts == ["api", "notes"]:
            data = self.read_json()
            map_id = int(data.get("map_id", 0) or 0)
            side = validate_side(data.get("side"))
            body = str(data.get("body", "")).strip()
            if not body:
                raise ApiError(400, "内容不能为空")
            stamp = now()
            with connect() as conn:
                require_map(conn, map_id)
                sort_order = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM notes WHERE map_id = ? AND side = ?",
                    (map_id, side),
                ).fetchone()[0]
                cursor = conn.execute(
                    """
                    INSERT INTO notes (map_id, side, body, sort_order, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (map_id, side, body, sort_order, stamp, stamp),
                )
                row = require_note(conn, cursor.lastrowid)
                self.send_json({"note": note_to_dict(row)}, 201)
            return

        note_match = re.fullmatch(r"/api/notes/(\d+)", path)
        if note_match and method == "PUT":
            note_id = int(note_match.group(1))
            data = self.read_json()
            body = str(data.get("body", "")).strip()
            if not body:
                raise ApiError(400, "内容不能为空")
            with connect() as conn:
                require_note(conn, note_id)
                conn.execute("UPDATE notes SET body = ?, updated_at = ? WHERE id = ?", (body, now(), note_id))
                row = require_note(conn, note_id)
                self.send_json({"note": note_to_dict(row)})
            return

        if note_match and method == "DELETE":
            note_id = int(note_match.group(1))
            with connect() as conn:
                require_note(conn, note_id)
                conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
                self.send_json({"ok": True})
            return

        raise ApiError(404, "API 不存在")


def find_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("没有找到可用端口")


def run() -> None:
    init_db()
    env_port = os.environ.get("PORT")
    preferred = int(env_port or "8000")
    port = preferred if env_port else find_port(preferred)
    host = os.environ.get("HOST") or ("0.0.0.0" if env_port else "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    print(f"CS2 战术本已启动: http://{display_host}:{port}", flush=True)
    print("按 Ctrl+C 停止服务", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run()
