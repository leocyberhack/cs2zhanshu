from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import io
import json
import mimetypes
import os
import random
import re
import secrets
import socket
import signal
import sqlite3
import threading
import time
import traceback
import zipfile
from email.utils import formatdate, parsedate_to_datetime
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
MAX_JSON_BODY_SIZE = 2 * 1024 * 1024
GZIP_MIN_BODY_SIZE = 1024
KEEP_ALIVE_TIMEOUT = 15
VERSIONED_ASSETS = ("styles.css", "app.js")
EXPORT_JOB_TTL = 15 * 60
EXPORT_JOB_LIMIT = 16

DEFAULT_MAPS = ["Mirage", "Dust2", "Ancient", "Nuke", "Overpass", "Anubis"]
SIDES = {"T", "CT"}
ECONOMY_OPTIONS = {"手枪局", "eco局", "半起局", "反eco局", "长枪局", "通用", "自定义"}
TACTIC_TAG_OPTIONS = {"常规默认", "非常规", "爆弹", "rush", "自定义"}

_RE_MAP = re.compile(r"/api/maps/(\d+)$")
_RE_MAP_CONTENT = re.compile(r"/api/maps/(\d+)/content$")
_RE_TACTIC = re.compile(r"/api/tactics/(\d+)$")
_RE_NOTE = re.compile(r"/api/notes/(\d+)$")


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def get_access_key() -> str:
    value = os.environ.get(ACCESS_KEY_ENV, "").strip()
    if value:
        return value
    if not os.environ.get("PORT"):
        return "local-dev-key"
    return ""


def auth_configured() -> bool:
    return bool(get_access_key())


def is_secure_cookie_context() -> bool:
    env_value = os.environ.get("COOKIE_SECURE", "").strip().lower()
    return bool(os.environ.get("PORT")) or env_value in {"1", "true", "yes"}


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


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback_obj):
        try:
            return super().__exit__(exc_type, exc_value, traceback_obj)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


_thread_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """Return a thread-local connection, creating one if needed."""
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    _thread_local.conn = conn
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
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

            CREATE INDEX IF NOT EXISTS idx_tactics_map_side_updated
            ON tactics(map_id, side, updated_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_notes_map_side_sort
            ON notes(map_id, side, sort_order, id);
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


def unique_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{random.randint(0, 0xffff):04x}"


def normalize_command(command: dict, index: int) -> dict:
    try:
        priority = int(command.get("priority", 1))
    except (TypeError, ValueError):
        priority = 1
    return {
        "id": str(command.get("id") or unique_id(f"cmd-{index}")),
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
        "id": unique_id("node"),
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
        "id": str(node.get("id") or unique_id("node")),
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


def empty_counts() -> dict:
    return {
        "t_tactics": 0,
        "ct_tactics": 0,
        "t_notes": 0,
        "ct_notes": 0,
    }


def load_map_counts(conn: sqlite3.Connection, map_ids: list[int] | None = None) -> dict[int, dict]:
    counts_by_map: dict[int, dict] = {}
    where = ""
    params: tuple[int, ...] = ()
    if map_ids is not None:
        if not map_ids:
            return counts_by_map
        placeholders = ", ".join("?" for _ in map_ids)
        where = f" WHERE map_id IN ({placeholders})"
        params = tuple(map_ids)

    for row in conn.execute(
        f"SELECT map_id, side, COUNT(*) AS count FROM tactics{where} GROUP BY map_id, side",
        params,
    ):
        key = "t_tactics" if row["side"] == "T" else "ct_tactics"
        counts_by_map.setdefault(row["map_id"], empty_counts())[key] = row["count"]

    for row in conn.execute(
        f"SELECT map_id, side, COUNT(*) AS count FROM notes{where} GROUP BY map_id, side",
        params,
    ):
        key = "t_notes" if row["side"] == "T" else "ct_notes"
        counts_by_map.setdefault(row["map_id"], empty_counts())[key] = row["count"]

    return counts_by_map


def map_to_dict(conn: sqlite3.Connection, row: sqlite3.Row, counts: dict | None = None) -> dict:
    if counts is None:
        counts = load_map_counts(conn, [row["id"]]).get(row["id"], empty_counts())
    return {
        "id": row["id"],
        "name": row["name"],
        "sort_order": row["sort_order"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "counts": counts,
    }


def load_maps(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM maps ORDER BY sort_order, id").fetchall()
    counts_by_map = load_map_counts(conn)
    return [map_to_dict(conn, row, counts_by_map.get(row["id"], empty_counts())) for row in rows]


def map_content_to_dict(conn: sqlite3.Connection, map_id: int, preloaded_counts: dict | None = None) -> dict:
    row = require_map(conn, map_id)
    tactics = conn.execute(
        "SELECT * FROM tactics WHERE map_id = ? ORDER BY updated_at DESC, id DESC", (map_id,)
    ).fetchall()
    notes = conn.execute("SELECT * FROM notes WHERE map_id = ? ORDER BY sort_order, id", (map_id,)).fetchall()
    counts = preloaded_counts if preloaded_counts is not None else None
    return {
        "map": map_to_dict(conn, row, counts),
        "tactics": {
            "T": [tactic_to_dict(item) for item in tactics if item["side"] == "T"],
            "CT": [tactic_to_dict(item) for item in tactics if item["side"] == "CT"],
        },
        "notes": {
            "T": [note_to_dict(item) for item in notes if item["side"] == "T"],
            "CT": [note_to_dict(item) for item in notes if item["side"] == "CT"],
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
        blocks.append({"text": "无", "level": level, "kind": "muted"})
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
        blocks.append({"text": "无", "level": 2, "kind": "muted"})
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
            blocks.append({"text": "无", "level": 4, "kind": "muted"})
        blocks.append({"text": "中期决策", "level": 3, "kind": "label"})
        add_decision_blocks(blocks, tactic.get("decision_tree", []), 4)


def add_note_blocks(blocks: list[dict], title: str, notes: list[dict]) -> None:
    blocks.append({"text": title, "level": 1, "kind": "heading2"})
    if not notes:
        blocks.append({"text": "无", "level": 2, "kind": "muted"})
        return
    for index, note in enumerate(notes, start=1):
        body = " ".join(note["body"].splitlines()).strip()
        blocks.append({"text": f"{index}. {body}", "level": 2, "kind": "body"})


def build_export_blocks() -> list[dict]:
    with connect() as conn:
        maps = conn.execute("SELECT * FROM maps ORDER BY sort_order, id").fetchall()
        tactics_by_map = {row["id"]: {"T": [], "CT": []} for row in maps}
        notes_by_map = {row["id"]: {"T": [], "CT": []} for row in maps}
        for tactic_row in conn.execute("SELECT * FROM tactics ORDER BY map_id, side, updated_at DESC, id DESC"):
            tactics_by_map.setdefault(tactic_row["map_id"], {"T": [], "CT": []})[tactic_row["side"]].append(
                tactic_to_dict(tactic_row)
            )
        for note_row in conn.execute("SELECT * FROM notes ORDER BY map_id, side, sort_order, id"):
            notes_by_map.setdefault(note_row["map_id"], {"T": [], "CT": []})[note_row["side"]].append(
                note_to_dict(note_row)
            )
        blocks = []
        for map_index, map_row in enumerate(maps):
            tactics_by_side = tactics_by_map.get(map_row["id"], {"T": [], "CT": []})
            notes_by_side = notes_by_map.get(map_row["id"], {"T": [], "CT": []})
            blocks.append({"text": map_row["name"], "level": 0, "kind": "heading1", "page_break": map_index > 0})
            add_tactic_blocks(blocks, "T方战术", tactics_by_side["T"])
            add_tactic_blocks(blocks, "CT方战术", tactics_by_side["CT"])
            add_note_blocks(blocks, "T方注意事项和技巧", notes_by_side["T"])
            add_note_blocks(blocks, "CT方注意事项和技巧", notes_by_side["CT"])
        return blocks


def docx_paragraph(text: str, kind: str, level: int, page_break: bool = False) -> str:
    size_map = {"heading1": 40, "heading2": 30, "heading3": 23, "label": 21, "meta": 20}
    size = size_map.get(kind, 19)
    bold = kind in {"heading1", "heading2", "heading3", "label"}
    italic = kind == "heading1"
    centered = kind == "heading1"
    spacing_before = 40
    spacing_after = 40
    indent = 0 if centered else max(0, level) * 360
    align_xml = "<w:jc w:val=\"center\"/>" if centered else ""
    bold_xml = "<w:b/>" if bold else ""
    italic_xml = "<w:i/>" if italic else ""
    color = "66756E" if kind in {"meta", "muted"} else "18211D"
    break_xml = "<w:r><w:br w:type=\"page\"/></w:r>" if page_break else ""
    return (
        "<w:p>"
        f"<w:pPr><w:spacing w:before=\"{spacing_before}\" w:after=\"{spacing_after}\" w:line=\"300\" w:lineRule=\"auto\"/>"
        f"<w:ind w:left=\"{indent}\"/>{align_xml}</w:pPr>"
        f"{break_xml}"
        "<w:r>"
        f"<w:rPr>{bold_xml}{italic_xml}<w:color w:val=\"{color}\"/><w:sz w:val=\"{size}\"/></w:rPr>"
        f"<w:t xml:space=\"preserve\">{xml_escape(text)}</w:t>"
        "</w:r></w:p>"
    )


def build_docx(blocks: list[dict]) -> bytes:
    paragraphs = "\n".join(
        docx_paragraph(item["text"], item["kind"], item["level"], item.get("page_break", False)) for item in blocks
    )
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


def _find_cjk_font() -> str:
    """Find a CJK font file, checking bundled font first, then system fonts."""
    candidates = [
        BASE_DIR / "fonts" / "SimHei.ttf",
        BASE_DIR / "fonts" / "NotoSansSC-Regular.otf",
        BASE_DIR / "fonts" / "NotoSansSC-Regular.ttf",
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        Path("/usr/share/fonts/noto-cjk/NotoSansSC-Regular.otf"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\msyh.ttc"),
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise FileNotFoundError(
        "找不到可用的中文字体。请将 SimHei.ttf 或 NotoSansSC-Regular.otf 放入 fonts/ 目录。"
    )


def _register_pdf_font() -> str:
    """Register a CJK font with reportlab and return the font name."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font_name = "CJKFont"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        font_path = _find_cjk_font()
        pdfmetrics.registerFont(TTFont(font_name, font_path))
    return font_name


def build_pdf(blocks: list[dict]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    font_name = _register_pdf_font()
    page_width, page_height = A4  # 595 x 842
    margin_x, margin_y = 48, 50
    line_height = 22
    size_map = {"heading1": 22, "heading2": 16, "heading3": 12, "label": 11, "meta": 10}

    output = io.BytesIO()
    c = canvas.Canvas(output, pagesize=A4)
    c.setTitle("CS2 战术本")
    y = page_height - margin_y

    def new_page() -> None:
        nonlocal y
        c.showPage()
        y = page_height - margin_y

    for block in blocks:
        kind = block["kind"]
        if block.get("page_break"):
            new_page()
        level = block["level"]
        size = size_map.get(kind, 10)
        bold = kind in {"heading1", "heading2", "heading3", "label"}
        centered = kind == "heading1"
        x = margin_x + level * 18
        max_width = page_width - x - margin_x

        text = block["text"]
        if not text:
            y -= line_height
            continue

        # Simple text wrapping
        lines = _wrap_text_for_pdf(c, font_name, size, text, max_width)
        for line in lines:
            if y < margin_y:
                new_page()
            c.setFont(font_name, size)
            if centered:
                text_width = c.stringWidth(line, font_name, size)
                draw_x = (page_width - text_width) / 2
            else:
                draw_x = x
            if bold:
                # Simulate bold by drawing text twice with a slight offset
                c.drawString(draw_x, y, line)
                c.drawString(draw_x + 0.4, y, line)
            else:
                c.drawString(draw_x, y, line)
            y -= line_height

    c.save()
    return output.getvalue()


def _wrap_text_for_pdf(c, font_name: str, size: int, text: str, max_width: float) -> list[str]:
    """Wrap text to fit within max_width using actual font metrics."""
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and c.stringWidth(candidate, font_name, size) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


_export_jobs_lock = threading.Lock()
_export_jobs: dict[str, dict] = {}


def validate_export_format(value: str) -> str:
    export_format = str(value or "docx").strip().lower()
    if export_format not in {"docx", "pdf"}:
        raise ApiError(400, "导出格式必须是 docx 或 pdf")
    return export_format


def export_filename(export_format: str) -> str:
    return "cs2-tactics-book.pdf" if export_format == "pdf" else "cs2-tactics-book.docx"


def export_content_type(export_format: str) -> str:
    if export_format == "pdf":
        return "application/pdf"
    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def build_export_file(export_format: str) -> bytes:
    blocks = build_export_blocks()
    if export_format == "docx":
        return build_docx(blocks)
    if export_format == "pdf":
        return build_pdf(blocks)
    raise ApiError(400, "导出格式必须是 docx 或 pdf")


def cleanup_export_jobs() -> None:
    cutoff = time.time() - EXPORT_JOB_TTL
    stale_ids = [
        job_id
        for job_id, job in _export_jobs.items()
        if (job.get("finished_ts") or job.get("created_ts", 0)) < cutoff
    ]
    for job_id in stale_ids:
        _export_jobs.pop(job_id, None)

    if len(_export_jobs) <= EXPORT_JOB_LIMIT:
        return
    ordered = sorted(_export_jobs.items(), key=lambda item: item[1].get("created_ts", 0))
    for job_id, _job in ordered[: len(_export_jobs) - EXPORT_JOB_LIMIT]:
        _export_jobs.pop(job_id, None)


def export_job_public(job: dict) -> dict:
    return {
        "id": job["id"],
        "format": job["format"],
        "status": job["status"],
        "filename": job["filename"],
        "size": job.get("size", 0),
        "error": job.get("error", ""),
        "created_at": job["created_at"],
        "started_at": job.get("started_at", ""),
        "finished_at": job.get("finished_at", ""),
    }


def start_export_job(export_format: str) -> dict:
    job_id = secrets.token_urlsafe(12)
    stamp = now()
    job = {
        "id": job_id,
        "format": export_format,
        "status": "queued",
        "filename": export_filename(export_format),
        "content_type": export_content_type(export_format),
        "content": b"",
        "size": 0,
        "error": "",
        "created_at": stamp,
        "created_ts": time.time(),
        "started_at": "",
        "finished_at": "",
        "finished_ts": 0.0,
    }
    with _export_jobs_lock:
        cleanup_export_jobs()
        _export_jobs[job_id] = job
    threading.Thread(target=run_export_job, args=(job_id,), daemon=True).start()
    return export_job_public(job)


def run_export_job(job_id: str) -> None:
    with _export_jobs_lock:
        job = _export_jobs.get(job_id)
        if not job:
            return
        export_format = job["format"]
        job["status"] = "running"
        job["started_at"] = now()

    try:
        content = build_export_file(export_format)
    except Exception as exc:
        traceback.print_exc()
        with _export_jobs_lock:
            job = _export_jobs.get(job_id)
            if job:
                job["status"] = "error"
                job["error"] = str(exc) or "导出失败"
                job["finished_at"] = now()
                job["finished_ts"] = time.time()
        return

    with _export_jobs_lock:
        job = _export_jobs.get(job_id)
        if job:
            job["status"] = "done"
            job["content"] = content
            job["size"] = len(content)
            job["finished_at"] = now()
            job["finished_ts"] = time.time()


def get_export_job(job_id: str) -> dict:
    with _export_jobs_lock:
        cleanup_export_jobs()
        job = _export_jobs.get(job_id)
        if not job:
            raise ApiError(404, "导出任务不存在或已过期")
        return dict(job)


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


def parse_map_id(value) -> int:
    if isinstance(value, bool):
        raise ApiError(400, "地图 ID 必须是正整数")
    if isinstance(value, int):
        map_id = value
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdigit():
            raise ApiError(400, "地图 ID 必须是正整数")
        map_id = int(stripped)
    else:
        raise ApiError(400, "地图 ID 必须是正整数")
    if map_id <= 0:
        raise ApiError(400, "地图 ID 必须是正整数")
    return map_id


def parse_optional_map_id(value) -> int | None:
    try:
        return parse_map_id(value)
    except ApiError:
        return None


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
    protocol_version = "HTTP/1.1"
    server_version = "CS2Tactics/1.0"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(KEEP_ALIVE_TIMEOUT)

    def log_message(self, format: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), format % args))

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        elapsed = (time.monotonic() - self._request_start) * 1000 if hasattr(self, "_request_start") else 0
        self.log_message('"%s" %s %s %.0fms', self.requestline, str(code), str(size), elapsed)

    def do_GET(self) -> None:
        self.handle_request("GET")

    def do_POST(self) -> None:
        self.handle_request("POST")

    def do_PUT(self) -> None:
        self.handle_request("PUT")

    def do_DELETE(self) -> None:
        self.handle_request("DELETE")

    def handle_request(self, method: str) -> None:
        self._request_start = time.monotonic()
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path.startswith("/api/"):
                self.handle_api(method, path, parsed.query)
            elif method == "GET":
                self.serve_static(path, parsed.query)
            else:
                raise ApiError(405, "Method not allowed")
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except sqlite3.IntegrityError as exc:
            message = "数据已存在或违反约束"
            if "UNIQUE" in str(exc).upper():
                message = "名称已存在"
            self.send_json({"error": message}, 409)
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "服务器错误，请稍后再试"}, 500)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        if length > MAX_JSON_BODY_SIZE:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise ApiError(413, "请求体过大")
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
        secure = "; Secure" if is_secure_cookie_context() else ""
        self.send_header(
            "Set-Cookie",
            f"{AUTH_COOKIE}={token}; Max-Age={SESSION_MAX_AGE}; Path=/; HttpOnly; SameSite=Lax{secure}",
        )

    def clear_auth_cookie(self) -> None:
        secure = "; Secure" if is_secure_cookie_context() else ""
        self.send_header(
            "Set-Cookie",
            f"{AUTH_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax{secure}",
        )

    def accepts_gzip(self) -> bool:
        accepted = self.headers.get("Accept-Encoding", "")
        return any(item.strip().split(";", 1)[0].lower() == "gzip" for item in accepted.split(","))

    def is_compressible_content(self, content_type: str) -> bool:
        media_type = content_type.split(";", 1)[0].strip().lower()
        return media_type.startswith("text/") or media_type in {
            "application/json",
            "application/javascript",
            "application/xml",
            "image/svg+xml",
        }

    def send_connection_headers(self) -> None:
        if self.close_connection:
            self.send_header("Connection", "close")
            return
        self.send_header("Connection", "keep-alive")
        self.send_header("Keep-Alive", f"timeout={KEEP_ALIVE_TIMEOUT}")

    def send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")

    def send_body(
        self,
        content: bytes,
        content_type: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
        compress: bool = True,
    ) -> None:
        should_vary = compress and self.is_compressible_content(content_type)
        encoded = content
        if should_vary and len(content) >= GZIP_MIN_BODY_SIZE and self.accepts_gzip():
            encoded = gzip.compress(content, compresslevel=6)
            headers = {**(headers or {}), "Content-Encoding": "gzip"}

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_connection_headers()
        self.send_security_headers()
        if should_vary:
            self.send_header("Vary", "Accept-Encoding")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, value, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_body(body, "application/json; charset=utf-8", status)

    def send_download(self, content: bytes, content_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_connection_headers()
        self.send_security_headers()
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def static_asset_version(self, name: str) -> str:
        file_path = STATIC_DIR / name
        if not file_path.exists():
            return "missing"
        stat = file_path.stat()
        return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"

    def static_cache_headers(self, file_path: Path) -> tuple[str, str, float]:
        stat = file_path.stat()
        identities = [f"{stat.st_mtime_ns:x}-{stat.st_size:x}"]
        modified_at = stat.st_mtime
        if file_path.name == "index.html":
            for name in VERSIONED_ASSETS:
                asset_path = STATIC_DIR / name
                if asset_path.exists():
                    asset_stat = asset_path.stat()
                    identities.append(f"{name}:{asset_stat.st_mtime_ns:x}-{asset_stat.st_size:x}")
                    modified_at = max(modified_at, asset_stat.st_mtime)
        etag = f'W/"{"-".join(identities)}"'
        last_modified = formatdate(modified_at, usegmt=True)
        return etag, last_modified, modified_at

    def static_not_modified(self, etag: str, last_modified: str, modified_at: float) -> bool:
        if self.headers.get("If-None-Match") == etag:
            return True
        if_modified_since = self.headers.get("If-Modified-Since")
        if not if_modified_since:
            return False
        try:
            since = parsedate_to_datetime(if_modified_since)
        except (TypeError, ValueError):
            return False
        return since.timestamp() >= int(modified_at)

    def static_cache_control(self, file_path: Path, query: str) -> str:
        params = parse_qs(query)
        if file_path.suffix in {".css", ".js"} and params.get("v"):
            return "public, max-age=31536000, immutable"
        if file_path.suffix in {".html", ".css", ".js"}:
            return "no-cache"
        return "public, max-age=3600"

    def inject_asset_versions(self, file_path: Path, content: bytes) -> bytes:
        if file_path.name != "index.html":
            return content
        html = content.decode("utf-8")
        for name in VERSIONED_ASSETS:
            version = self.static_asset_version(name)
            pattern = rf"(/static/{re.escape(name)})(?:\?v=[^\"'<> ]*)?"
            html = re.sub(pattern, rf"\1?v={version}", html)
        return html.encode("utf-8")

    def serve_static(self, path: str, query: str = "") -> None:
        if path in ("", "/"):
            file_path = STATIC_DIR / "index.html"
        else:
            static_root = STATIC_DIR.resolve()
            relative_path = path.removeprefix("/static/").lstrip("/")
            requested = (static_root / relative_path).resolve()
            try:
                requested.relative_to(static_root)
            except ValueError:
                raise ApiError(403, "Forbidden")
            file_path = requested

        if not file_path.exists() or not file_path.is_file():
            file_path = STATIC_DIR / "index.html"

        etag, last_modified, modified_at = self.static_cache_headers(file_path)
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if file_path.suffix in {".html", ".css", ".js"}:
            content_type += "; charset=utf-8"
        cache_control = self.static_cache_control(file_path, query)
        if self.static_not_modified(etag, last_modified, modified_at):
            self.send_response(304)
            self.send_connection_headers()
            self.send_security_headers()
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.send_header("Cache-Control", cache_control)
            if self.is_compressible_content(content_type):
                self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            return

        cached = _static_cache_get(file_path, etag)
        if cached is not None:
            content = cached
        else:
            content = self.inject_asset_versions(file_path, file_path.read_bytes())
            _static_cache_set(file_path, etag, content)
        self.send_body(
            content,
            content_type,
            headers={
                "ETag": etag,
                "Last-Modified": last_modified,
                "Cache-Control": cache_control,
            },
        )

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

        if method == "GET" and parts == ["api", "bootstrap"]:
            authenticated = self.is_authenticated()
            payload = {
                "authenticated": authenticated,
                "configured": auth_configured(),
                "env": ACCESS_KEY_ENV,
            }
            if authenticated:
                params = parse_qs(query)
                preferred_id = parse_optional_map_id((params.get("map_id") or [""])[0])
                with connect() as conn:
                    maps = load_maps(conn)
                    selected_map = None
                    if preferred_id is not None:
                        selected_map = next((item for item in maps if item["id"] == preferred_id), None)
                    if selected_map is None and maps:
                        selected_map = maps[0]
                    payload["maps"] = maps
                    payload["selected_map_id"] = selected_map["id"] if selected_map else None
                    if selected_map:
                        payload["content"] = map_content_to_dict(
                            conn, selected_map["id"], selected_map.get("counts")
                        )
                    else:
                        payload["content"] = None
            self.send_json(payload)
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
            self.send_connection_headers()
            self.send_header("Content-Length", str(len(body)))
            self.send_auth_cookie(token)
            self.end_headers()
            self.wfile.write(body)
            return

        if method == "POST" and parts == ["api", "logout"]:
            body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_connection_headers()
            self.send_header("Content-Length", str(len(body)))
            self.clear_auth_cookie()
            self.end_headers()
            self.wfile.write(body)
            return

        if not self.is_authenticated():
            raise ApiError(401, "请先输入密钥登录")

        if method == "POST" and parts == ["api", "export-jobs"]:
            data = self.read_json()
            export_format = validate_export_format(data.get("format", "docx"))
            self.send_json({"job": start_export_job(export_format)}, 202)
            return

        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "export-jobs"]:
            job = get_export_job(parts[2])
            self.send_json({"job": export_job_public(job)})
            return

        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "export-jobs"] and parts[3] == "download":
            job = get_export_job(parts[2])
            if job["status"] == "error":
                raise ApiError(500, job.get("error") or "导出失败")
            if job["status"] != "done":
                raise ApiError(409, "导出任务尚未完成")
            self.send_download(job["content"], job["content_type"], job["filename"])
            return

        if method == "GET" and parts == ["api", "export"]:
            params = parse_qs(query)
            export_format = validate_export_format((params.get("format", ["docx"])[0] or "docx").lower())
            self.send_download(
                build_export_file(export_format),
                export_content_type(export_format),
                export_filename(export_format),
            )
            return

        if method == "GET" and parts == ["api", "maps"]:
            with connect() as conn:
                self.send_json({"maps": load_maps(conn)})
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

        map_match = _RE_MAP.match(path)
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

        if method == "PUT" and parts == ["api", "maps", "reorder"]:
            data = self.read_json()
            order = data.get("order")
            if not isinstance(order, list) or not order:
                raise ApiError(400, "请提供有序的地图 ID 列表")
            parsed_order: list[int] = []
            seen_ids: set[int] = set()
            for raw_id in order:
                map_id = parse_map_id(raw_id)
                if map_id in seen_ids:
                    raise ApiError(400, "地图 ID 不能重复")
                seen_ids.add(map_id)
                parsed_order.append(map_id)
            with connect() as conn:
                existing_ids = {
                    row["id"] for row in conn.execute("SELECT id FROM maps")
                }
                if set(parsed_order) != existing_ids:
                    raise ApiError(400, "请提供完整且有效的地图 ID 列表")
                stamp = now()
                for index, map_id in enumerate(parsed_order):
                    conn.execute("UPDATE maps SET sort_order = ?, updated_at = ? WHERE id = ?", (index + 1, stamp, map_id))
                self.send_json({"maps": load_maps(conn)})
            return

        content_match = _RE_MAP_CONTENT.match(path)
        if content_match and method == "GET":
            map_id = int(content_match.group(1))
            with connect() as conn:
                self.send_json(map_content_to_dict(conn, map_id))
            return

        if method == "POST" and parts == ["api", "tactics"]:
            data = self.read_json()
            map_id = parse_map_id(data.get("map_id"))
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

        tactic_match = _RE_TACTIC.match(path)
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
            map_id = parse_map_id(data.get("map_id"))
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

        note_match = _RE_NOTE.match(path)
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


_static_cache_lock = threading.Lock()
_static_cache: dict[str, tuple[str, bytes]] = {}


def _static_cache_get(file_path: Path, etag: str) -> bytes | None:
    key = str(file_path)
    with _static_cache_lock:
        entry = _static_cache.get(key)
        if entry and entry[0] == etag:
            return entry[1]
    return None


def _static_cache_set(file_path: Path, etag: str, content: bytes) -> None:
    key = str(file_path)
    with _static_cache_lock:
        _static_cache[key] = (etag, content)


class TacticsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


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
    server = TacticsHTTPServer((host, port), Handler)

    def graceful_shutdown(signum, frame):
        print("收到关闭信号，正在停止服务...", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    print(f"CS2 战术本已启动: http://{display_host}:{port}", flush=True)
    if not os.environ.get(ACCESS_KEY_ENV) and not env_port:
        print(f"本地测试密钥: {get_access_key()}", flush=True)
    print("按 Ctrl+C 停止服务", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run()
