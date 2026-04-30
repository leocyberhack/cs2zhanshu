from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import time
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from xml.sax.saxutils import escape as xml_escape


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "tactics.db"))).resolve()
ACCESS_KEY_ENV = "APP_ACCESS_KEY"
AUTH_COOKIE = "cs2_tactics_session"
SESSION_MAX_AGE = 30 * 24 * 60 * 60

DEFAULT_MAPS = ["Mirage", "Dust2", "Ancient", "Nuke", "Overpass", "Anubis"]
SIDES = {"T", "CT"}
ECONOMY_OPTIONS = {"手枪局", "eco局", "半起局", "反eco局", "长枪局", "通用", "自定义"}
TACTIC_TAG_OPTIONS = {"常规默认", "非常规", "爆弹", "rush", "自定义"}


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def get_access_key() -> str:
    value = os.environ.get(ACCESS_KEY_ENV, "").strip()
    if value:
        return value
    if not os.environ.get("PORT"):
        return "local-dev-key"
    return ""


def auth_configured() -> bool:
    return bool(get_access_key())


def sign_session(timestamp: str, nonce: str) -> str:
    secret = get_access_key().encode("utf-8")
    message = f"{timestamp}:{nonce}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def make_session_token() -> str:
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(18)
    signature = sign_session(timestamp, nonce)
    raw = f"{timestamp}:{nonce}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def verify_session_token(token: str) -> bool:
    if not auth_configured() or not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        timestamp, nonce, signature = raw.split(":", 2)
        created_at = int(timestamp)
    except (ValueError, UnicodeDecodeError):
        return False
    if int(time.time()) - created_at > SESSION_MAX_AGE:
        return False
    expected = sign_session(timestamp, nonce)
    return hmac.compare_digest(signature, expected)


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


def tactic_display_meta(tactic: dict) -> tuple[str, str]:
    economy = tactic["economy_custom"] if tactic["economy"] == "自定义" else tactic["economy"]
    tags = [tag for tag in tactic.get("tactic_tags", []) if tag != "自定义"]
    tags.extend(tactic.get("tactic_custom_tags", []))
    return economy or "通用", "、".join(tags) if tags else "未标记"


def grouped_commands(commands: list[dict]) -> dict[int, list[str]]:
    grouped: dict[int, list[str]] = {}
    for command in commands:
        priority = max(1, int(command.get("priority", 1)))
        grouped.setdefault(priority, []).append(command.get("text", ""))
    return grouped


def add_decision_blocks(blocks: list[dict], nodes: list[dict], level: int) -> None:
    if not nodes:
        blocks.append({"text": "暂无中期判断", "level": level, "kind": "muted"})
        return
    for index, node in enumerate(nodes, start=1):
        condition = node.get("condition") or "未填写条件"
        then_action = node.get("thenAction") or "未填写行动"
        blocks.append({"text": f"{index}. 如果：{condition}", "level": level, "kind": "body"})
        blocks.append({"text": f"那么：{then_action}", "level": level + 1, "kind": "body"})
        children = node.get("children", [])
        if children:
            blocks.append({"text": "下级判断", "level": level + 1, "kind": "label"})
            add_decision_blocks(blocks, children, level + 2)


def add_tactic_blocks(blocks: list[dict], title: str, tactics: list[dict]) -> None:
    blocks.append({"text": title, "level": 1, "kind": "heading2"})
    if not tactics:
        blocks.append({"text": "暂无战术", "level": 2, "kind": "muted"})
        return
    for index, tactic in enumerate(tactics, start=1):
        economy, tags = tactic_display_meta(tactic)
        blocks.append({"text": f"{index}. {tactic['title']}", "level": 2, "kind": "heading3"})
        blocks.append({"text": f"经济属性：{economy}", "level": 3, "kind": "meta"})
        blocks.append({"text": f"战术属性：{tags}", "level": 3, "kind": "meta"})
        blocks.append({"text": "前期战术展开", "level": 3, "kind": "label"})
        commands = grouped_commands(tactic.get("early_commands", []))
        if commands:
            for priority in sorted(commands):
                blocks.append({"text": f"P{priority} 同步执行", "level": 4, "kind": "label"})
                for text in commands[priority]:
                    blocks.append({"text": f"- {text}", "level": 5, "kind": "body"})
        else:
            blocks.append({"text": "暂无前期命令", "level": 4, "kind": "muted"})
        blocks.append({"text": "中期决策", "level": 3, "kind": "label"})
        add_decision_blocks(blocks, tactic.get("decision_tree", []), 4)


def add_note_blocks(blocks: list[dict], title: str, notes: list[dict]) -> None:
    blocks.append({"text": title, "level": 1, "kind": "heading2"})
    if not notes:
        blocks.append({"text": "暂无内容", "level": 2, "kind": "muted"})
        return
    for index, note in enumerate(notes, start=1):
        body = " ".join(note["body"].splitlines()).strip()
        blocks.append({"text": f"{index}. {body}", "level": 2, "kind": "body"})


def build_export_blocks() -> list[dict]:
    with connect() as conn:
        maps = conn.execute("SELECT * FROM maps ORDER BY sort_order, id").fetchall()
        blocks = [
            {"text": "CS2 战术本导出", "level": 0, "kind": "title"},
            {"text": f"导出时间：{now()}", "level": 0, "kind": "meta"},
        ]
        for map_row in maps:
            map_id = map_row["id"]
            tactics = conn.execute(
                "SELECT * FROM tactics WHERE map_id = ? ORDER BY side, updated_at DESC, id DESC", (map_id,)
            ).fetchall()
            notes = conn.execute(
                "SELECT * FROM notes WHERE map_id = ? ORDER BY side, sort_order, id", (map_id,)
            ).fetchall()
            tactics_by_side = {
                "T": [tactic_to_dict(item) for item in tactics if item["side"] == "T"],
                "CT": [tactic_to_dict(item) for item in tactics if item["side"] == "CT"],
            }
            notes_by_side = {
                "T": [note_to_dict(item) for item in notes if item["side"] == "T"],
                "CT": [note_to_dict(item) for item in notes if item["side"] == "CT"],
            }
            blocks.append({"text": map_row["name"], "level": 0, "kind": "heading1"})
            add_tactic_blocks(blocks, "T 方战术", tactics_by_side["T"])
            add_tactic_blocks(blocks, "CT 方战术", tactics_by_side["CT"])
            add_note_blocks(blocks, "T 方注意事项和技巧", notes_by_side["T"])
            add_note_blocks(blocks, "CT 方注意事项和技巧", notes_by_side["CT"])
        return blocks


def docx_paragraph(text: str, kind: str, level: int) -> str:
    size_map = {"title": 36, "heading1": 30, "heading2": 24, "heading3": 21, "label": 20, "meta": 18}
    size = size_map.get(kind, 19)
    bold = kind in {"title", "heading1", "heading2", "heading3", "label"}
    spacing_before = 220 if kind in {"heading1", "heading2"} else 70
    spacing_after = 90 if kind in {"title", "heading1", "heading2"} else 40
    indent = max(0, level) * 360
    bold_xml = "<w:b/>" if bold else ""
    color = "66756E" if kind in {"meta", "muted"} else "18211D"
    return (
        "<w:p>"
        f"<w:pPr><w:spacing w:before=\"{spacing_before}\" w:after=\"{spacing_after}\"/>"
        f"<w:ind w:left=\"{indent}\"/></w:pPr>"
        "<w:r>"
        f"<w:rPr>{bold_xml}<w:color w:val=\"{color}\"/><w:sz w:val=\"{size}\"/></w:rPr>"
        f"<w:t xml:space=\"preserve\">{xml_escape(text)}</w:t>"
        "</w:r></w:p>"
    )


def build_docx(blocks: list[dict]) -> bytes:
    paragraphs = "\n".join(docx_paragraph(item["text"], item["kind"], item["level"]) for item in blocks)
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {paragraphs}
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="708" w:footer="708" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document_xml)
    return output.getvalue()


def pdf_units(text: str) -> float:
    return sum(1.0 if ord(char) > 127 else 0.55 for char in text)


def wrap_pdf_text(text: str, max_units: float) -> list[str]:
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and pdf_units(candidate) > max_units:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def pdf_text_command(text: str, x: int, y: int, size: int) -> str:
    hex_text = text.encode("utf-16-be").hex().upper()
    return f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm <{hex_text}> Tj ET\n"


def build_pdf(blocks: list[dict]) -> bytes:
    page_width, page_height = 595, 842
    margin_x, margin_y = 48, 50
    y = page_height - margin_y
    pages: list[str] = []
    current = ""
    size_map = {"title": 18, "heading1": 16, "heading2": 13, "heading3": 12, "label": 11, "meta": 10}

    def new_page() -> None:
        nonlocal current, y
        if current:
            pages.append(current)
        current = ""
        y = page_height - margin_y

    for block in blocks:
        kind = block["kind"]
        level = block["level"]
        size = size_map.get(kind, 10)
        leading = size + 6
        if kind in {"title", "heading1"}:
            y -= 8
        x = margin_x + level * 18
        max_units = max(18, (page_width - x - margin_x) / max(size * 0.58, 1))
        for line in wrap_pdf_text(block["text"], max_units):
            if y < margin_y:
                new_page()
            current += pdf_text_command(line, x, y, size)
            y -= leading
        if kind in {"title", "heading1", "heading2"}:
            y -= 4
    if current:
        pages.append(current)

    objects: list[bytes | None] = [None]
    objects.append(b"")
    objects.append(b"")
    font_object = (
        b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /UniGB-UCS2-H "
        b"/DescendantFonts [<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 2 >> "
        b"/FontDescriptor << /Type /FontDescriptor /FontName /STSong-Light /Flags 6 "
        b"/FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 880 /Descent -120 "
        b"/CapHeight 880 /StemV 80 >> >>] >>"
    )
    objects.append(font_object)
    page_object_ids: list[int] = []
    for page in pages or [""]:
        stream = page.encode("ascii")
        content_obj_id = len(objects)
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")
        page_obj_id = len(objects)
        page_object_ids.append(page_obj_id)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width} {page_height}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_obj_id} 0 R >>".encode("ascii")
        )
    kids = " ".join(f"{item} 0 R" for item in page_object_ids)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_object_ids)} >>".encode("ascii")

    output = io.BytesIO()
    output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, body in enumerate(objects[1:], start=1):
        offsets.append(output.tell())
        output.write(f"{object_id} 0 obj\n".encode("ascii"))
        output.write(body or b"")
        output.write(b"\nendobj\n")
    xref_offset = output.tell()
    output.write(f"xref\n0 {len(objects)}\n".encode("ascii"))
    output.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.write(
        f"trailer\n<< /Size {len(objects)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("ascii")
    )
    return output.getvalue()


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
                self.handle_api(method, path, parsed.query)
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

    def cookie_value(self, name: str) -> str:
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return ""
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except Exception:
            return ""
        morsel = cookies.get(name)
        return morsel.value if morsel else ""

    def is_authenticated(self) -> bool:
        return verify_session_token(self.cookie_value(AUTH_COOKIE))

    def send_auth_cookie(self, token: str) -> None:
        self.send_header(
            "Set-Cookie",
            f"{AUTH_COOKIE}={token}; Max-Age={SESSION_MAX_AGE}; Path=/; HttpOnly; SameSite=Lax",
        )

    def clear_auth_cookie(self) -> None:
        self.send_header(
            "Set-Cookie",
            f"{AUTH_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax",
        )

    def send_json(self, value, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_download(self, content: bytes, content_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

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

    def handle_api(self, method: str, path: str, query: str = "") -> None:
        parts = [part for part in path.split("/") if part]

        if method == "GET" and parts == ["api", "health"]:
            self.send_json({"ok": True})
            return

        if method == "GET" and parts == ["api", "auth"]:
            self.send_json(
                {
                    "authenticated": self.is_authenticated(),
                    "configured": auth_configured(),
                    "env": ACCESS_KEY_ENV,
                }
            )
            return

        if method == "POST" and parts == ["api", "login"]:
            data = self.read_json()
            access_key = get_access_key()
            if not access_key:
                raise ApiError(503, f"服务器未配置 {ACCESS_KEY_ENV}")
            candidate = str(data.get("key", "")).strip()
            if not candidate or not hmac.compare_digest(candidate, access_key):
                raise ApiError(401, "密钥不正确")
            token = make_session_token()
            body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_auth_cookie(token)
            self.end_headers()
            self.wfile.write(body)
            return

        if method == "POST" and parts == ["api", "logout"]:
            body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.clear_auth_cookie()
            self.end_headers()
            self.wfile.write(body)
            return

        if not self.is_authenticated():
            raise ApiError(401, "请先输入密钥登录")

        if method == "GET" and parts == ["api", "export"]:
            params = parse_qs(query)
            export_format = (params.get("format", ["docx"])[0] or "docx").lower()
            blocks = build_export_blocks()
            if export_format == "docx":
                self.send_download(
                    build_docx(blocks),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "cs2-tactics-book.docx",
                )
                return
            if export_format == "pdf":
                self.send_download(build_pdf(blocks), "application/pdf", "cs2-tactics-book.pdf")
                return
            raise ApiError(400, "导出格式必须是 docx 或 pdf")

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
    if not os.environ.get(ACCESS_KEY_ENV) and not env_port:
        print(f"本地测试密钥: {get_access_key()}", flush=True)
    print("按 Ctrl+C 停止服务", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run()
