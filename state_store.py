"""SQLite state store. Replaces flat JSON files with a single state.db."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
DB_PATH = STATE_DIR / "state.db"

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS requirements (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    submitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS storypacks (
    id TEXT PRIMARY KEY,
    requirement_id TEXT NOT NULL,
    requirement_text TEXT NOT NULL,
    stories_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_review',
    created_at TEXT NOT NULL,
    FOREIGN KEY (requirement_id) REFERENCES requirements(id)
);

CREATE TABLE IF NOT EXISTS agent_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id TEXT,
    agent_type TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    storypack_id TEXT NOT NULL,
    test_type TEXT NOT NULL,
    passed INTEGER NOT NULL,
    output TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (storypack_id) REFERENCES storypacks(id)
);

CREATE TABLE IF NOT EXISTS fix_requests (
    id TEXT PRIMARY KEY,
    storypack_id TEXT NOT NULL,
    story_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    error_text TEXT NOT NULL,
    user_instructions TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_number INTEGER DEFAULT 1,
    result_message TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (storypack_id) REFERENCES storypacks(id)
);

CREATE TABLE IF NOT EXISTS agent_comms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    storypack_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL DEFAULT '',
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    event_type TEXT NOT NULL,
    story_id TEXT,
    cycle_number INTEGER DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failure_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    storypack_id TEXT NOT NULL,
    story_title TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    error_category TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    resolution TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enhancements (
    id TEXT PRIMARY KEY,
    agent_type TEXT NOT NULL,
    description TEXT NOT NULL,
    context TEXT DEFAULT '',
    story_json TEXT DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    result_message TEXT DEFAULT '',
    backup_path TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    storypack_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    story_id TEXT,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    ok INTEGER NOT NULL DEFAULT 1,
    result_excerpt TEXT NOT NULL DEFAULT '',
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    storypack_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL DEFAULT '',
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    event_type TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    story_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    reply_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    replied_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_pack ON tool_calls(storypack_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_story ON tool_calls(story_id);
CREATE INDEX IF NOT EXISTS idx_inbox_to ON agent_inbox(to_agent, status);
CREATE INDEX IF NOT EXISTS idx_inbox_correlation ON agent_inbox(correlation_id);

CREATE TABLE IF NOT EXISTS workspace_chat_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workspace_chat_msg_session ON workspace_chat_messages(session_id, id);

CREATE TABLE IF NOT EXISTS workspace_chat_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    path TEXT NOT NULL,
    summary TEXT,
    created_at TEXT NOT NULL
);
"""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = _get_conn()
    conn.executescript(_CREATE_TABLES)
    try:
        conn.execute("ALTER TABLE enhancements ADD COLUMN backup_path TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    # Migrate agent_comms: drop FK constraint if present (SQLite requires recreate)
    try:
        info = conn.execute("PRAGMA foreign_key_list(agent_comms)").fetchall()
        if info:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS agent_comms_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    storypack_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL DEFAULT '',
                    from_agent TEXT NOT NULL,
                    to_agent TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    story_id TEXT,
                    cycle_number INTEGER DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO agent_comms_new
                  (id, storypack_id, run_id, from_agent, to_agent, event_type, story_id,
                   cycle_number, payload_json, summary, created_at)
                  SELECT id, storypack_id, run_id, from_agent, to_agent, event_type, story_id,
                         cycle_number, payload_json, summary, created_at FROM agent_comms;
                DROP TABLE agent_comms;
                ALTER TABLE agent_comms_new RENAME TO agent_comms;
            """)
            conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass

    # Add correlation_id column if missing (no-FK case).
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_comms)").fetchall()]
        if "correlation_id" not in cols:
            conn.execute("ALTER TABLE agent_comms ADD COLUMN correlation_id TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass

    for alter in (
        "ALTER TABLE storypacks ADD COLUMN project_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE enhancements ADD COLUMN project_id TEXT NOT NULL DEFAULT 'default'",
    ):
        try:
            conn.execute(alter)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


init_db()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Requirements ---

def save_requirement(req_id: str, text: str, submitted_at: str | None = None) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO requirements (id, text, submitted_at) VALUES (?, ?, ?)",
        (req_id, text, submitted_at or _now_iso()),
    )
    conn.commit()
    conn.close()


def get_requirement(req_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM requirements WHERE id = ?", (req_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_requirements() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM requirements ORDER BY submitted_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- StoryPacks ---

def save_storypack(
    pack_id: str,
    requirement_id: str,
    requirement_text: str,
    stories: list[dict],
    status: str = "pending_review",
    created_at: str | None = None,
    project_id: str = "default",
) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO storypacks (id, requirement_id, requirement_text, stories_json, status, created_at, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            pack_id,
            requirement_id,
            requirement_text,
            json.dumps(stories, default=str),
            status,
            created_at or _now_iso(),
            project_id or "default",
        ),
    )
    conn.commit()
    conn.close()


def get_storypack(pack_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM storypacks WHERE id = ?", (pack_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["stories"] = json.loads(d.pop("stories_json"))
    return d


def get_storypack_by_requirement(req_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM storypacks WHERE requirement_id = ? ORDER BY created_at DESC LIMIT 1",
        (req_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["stories"] = json.loads(d.pop("stories_json"))
    return d


def list_storypacks() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM storypacks ORDER BY created_at DESC").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["stories"] = json.loads(d.pop("stories_json"))
        result.append(d)
    return result


def update_storypack_status(pack_id: str, status: str) -> None:
    conn = _get_conn()
    conn.execute("UPDATE storypacks SET status = ? WHERE id = ?", (status, pack_id))
    conn.commit()
    conn.close()


# --- Agent Logs ---

def add_agent_log(story_id: str | None, agent_type: str, message: str,
                  level: str = "info", detail: str | None = None) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO agent_logs (story_id, agent_type, level, message, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (story_id, agent_type, level, message, detail, _now_iso()),
    )
    conn.commit()
    conn.close()


def get_agent_logs(story_id: str | None = None, agent_type: str | None = None,
                   limit: int = 100) -> list[dict]:
    conn = _get_conn()
    query = "SELECT * FROM agent_logs WHERE 1=1"
    params: list[Any] = []
    if story_id:
        query += " AND story_id = ?"
        params.append(story_id)
    if agent_type:
        query += " AND agent_type = ?"
        params.append(agent_type)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Test Results ---

def save_test_result(storypack_id: str, test_type: str, passed: bool, output: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO test_results (storypack_id, test_type, passed, output, created_at) VALUES (?, ?, ?, ?, ?)",
        (storypack_id, test_type, int(passed), output, _now_iso()),
    )
    conn.commit()
    conn.close()


def get_test_results(storypack_id: str | None = None, limit: int = 50) -> list[dict]:
    conn = _get_conn()
    if storypack_id:
        rows = conn.execute(
            "SELECT * FROM test_results WHERE storypack_id = ? ORDER BY created_at DESC LIMIT ?",
            (storypack_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM test_results ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Fix Requests ---

def save_fix_request(fix_id: str, storypack_id: str, story_id: str,
                     agent_type: str, error_text: str,
                     user_instructions: str = "") -> None:
    conn = _get_conn()
    attempt = conn.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM fix_requests WHERE story_id = ? AND agent_type = ?",
        (story_id, agent_type),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO fix_requests (id, storypack_id, story_id, agent_type, error_text, user_instructions, status, attempt_number, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (fix_id, storypack_id, story_id, agent_type, error_text, user_instructions, attempt, _now_iso()),
    )
    conn.commit()
    conn.close()


def update_fix_request_status(fix_id: str, status: str, result_message: str = "") -> None:
    conn = _get_conn()
    completed_at = _now_iso() if status in ("success", "failed") else None
    conn.execute(
        "UPDATE fix_requests SET status = ?, result_message = ?, completed_at = ? WHERE id = ?",
        (status, result_message, completed_at, fix_id),
    )
    conn.commit()
    conn.close()


def get_fix_request(fix_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM fix_requests WHERE id = ?", (fix_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_fix_requests(storypack_id: str | None = None, limit: int = 50) -> list[dict]:
    conn = _get_conn()
    if storypack_id:
        rows = conn.execute(
            "SELECT * FROM fix_requests WHERE storypack_id = ? ORDER BY created_at DESC LIMIT ?",
            (storypack_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM fix_requests ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Agent Comms ---

def save_agent_comm(storypack_id: str, run_id: str, from_agent: str, to_agent: str,
                    event_type: str, story_id: str | None = None,
                    cycle_number: int = 0, payload_json: str = "{}",
                    summary: str = "", correlation_id: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO agent_comms (storypack_id, run_id, correlation_id, from_agent, to_agent, event_type, story_id, cycle_number, payload_json, summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (storypack_id, run_id, correlation_id, from_agent, to_agent, event_type, story_id, cycle_number, payload_json, summary, _now_iso()),
    )
    conn.commit()
    conn.close()


def get_agent_comms(storypack_id: str | None = None, run_id: str | None = None,
                    event_type: str | None = None, from_agent: str | None = None,
                    to_agent: str | None = None, limit: int = 200) -> list[dict]:
    conn = _get_conn()
    query = "SELECT * FROM agent_comms WHERE 1=1"
    params: list[Any] = []
    if storypack_id:
        query += " AND storypack_id = ?"
        params.append(storypack_id)
    if run_id:
        query += " AND run_id = ?"
        params.append(run_id)
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)
    if from_agent:
        query += " AND from_agent = ?"
        params.append(from_agent)
    if to_agent:
        query += " AND to_agent = ?"
        params.append(to_agent)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_agent_comms_since(since_id: int, limit: int = 500) -> list[dict]:
    """Return agent_comms rows with id > since_id, oldest first.

    Used by the SSE stream to replay missed events on reconnect and to tail
    the table during idle periods (no active supervisor).
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM agent_comms WHERE id > ? ORDER BY id ASC LIMIT ?",
        (int(since_id), int(limit)),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_comm_timeline(storypack_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM agent_comms WHERE storypack_id = ? ORDER BY created_at ASC",
        (storypack_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Failure Patterns ---

def save_failure_pattern(storypack_id: str, story_title: str, agent_type: str,
                         error_category: str, root_cause: str, resolution: str) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO failure_patterns (storypack_id, story_title, agent_type, error_category, root_cause, resolution, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (storypack_id, story_title, agent_type, error_category, root_cause, resolution, _now_iso()),
    )
    conn.commit()
    conn.close()


def get_failure_patterns(limit: int = 20) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM failure_patterns ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Enhancements ---

def save_enhancement(
    enhance_id: str,
    agent_type: str,
    description: str,
    context: str = "",
    story_json: str = "{}",
    project_id: str = "default",
) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO enhancements (id, agent_type, description, context, story_json, status, created_at, project_id) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
        (
            enhance_id,
            agent_type,
            description,
            context,
            story_json,
            _now_iso(),
            project_id or "default",
        ),
    )
    conn.commit()
    conn.close()


def update_enhancement_status(enhance_id: str, status: str, result_message: str = "",
                              story_json: str | None = None,
                              backup_path: str | None = None) -> None:
    conn = _get_conn()
    completed_at = _now_iso() if status in ("success", "failed") else None
    if story_json is not None and backup_path is not None:
        conn.execute(
            "UPDATE enhancements SET status = ?, result_message = ?, story_json = ?, backup_path = ?, completed_at = ? WHERE id = ?",
            (status, result_message, story_json, backup_path, completed_at, enhance_id),
        )
    elif story_json is not None:
        conn.execute(
            "UPDATE enhancements SET status = ?, result_message = ?, story_json = ?, completed_at = ? WHERE id = ?",
            (status, result_message, story_json, completed_at, enhance_id),
        )
    elif backup_path is not None:
        conn.execute(
            "UPDATE enhancements SET status = ?, result_message = ?, backup_path = ?, completed_at = ? WHERE id = ?",
            (status, result_message, backup_path, completed_at, enhance_id),
        )
    else:
        conn.execute(
            "UPDATE enhancements SET status = ?, result_message = ?, completed_at = ? WHERE id = ?",
            (status, result_message, completed_at, enhance_id),
        )
    conn.commit()
    conn.close()


def get_enhancement(enhance_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM enhancements WHERE id = ?", (enhance_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_enhancements(limit: int = 50) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM enhancements ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Tool Calls (audit trail of every tool invocation by an agent) ---

def save_tool_call(storypack_id: str, run_id: str, agent_id: str,
                   story_id: str | None, tool_name: str,
                   args_json: str, ok: bool, result_excerpt: str,
                   elapsed_ms: int) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO tool_calls (storypack_id, run_id, agent_id, story_id, tool_name, "
        "args_json, ok, result_excerpt, elapsed_ms, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (storypack_id, run_id, agent_id, story_id, tool_name,
         args_json, 1 if ok else 0, result_excerpt, elapsed_ms, _now_iso()),
    )
    conn.commit()
    conn.close()


def list_tool_calls(storypack_id: str | None = None, run_id: str | None = None,
                    agent_id: str | None = None, story_id: str | None = None,
                    limit: int = 200) -> list[dict]:
    conn = _get_conn()
    query = "SELECT * FROM tool_calls WHERE 1=1"
    params: list[Any] = []
    if storypack_id:
        query += " AND storypack_id = ?"
        params.append(storypack_id)
    if run_id:
        query += " AND run_id = ?"
        params.append(run_id)
    if agent_id:
        query += " AND agent_id = ?"
        params.append(agent_id)
    if story_id:
        query += " AND story_id = ?"
        params.append(story_id)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_tool_call_summary(storypack_id: str | None = None,
                          run_id: str | None = None) -> list[dict]:
    conn = _get_conn()
    query = (
        "SELECT agent_id, tool_name, COUNT(*) AS count, "
        "SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok_count, "
        "SUM(elapsed_ms) AS total_ms "
        "FROM tool_calls WHERE 1=1"
    )
    params: list[Any] = []
    if storypack_id:
        query += " AND storypack_id = ?"
        params.append(storypack_id)
    if run_id:
        query += " AND run_id = ?"
        params.append(run_id)
    query += " GROUP BY agent_id, tool_name ORDER BY count DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_agent_message_counts(storypack_id: str | None = None,
                             run_id: str | None = None) -> dict[str, dict[str, int]]:
    """Aggregate inter-agent message counts (sent + received) per agent_id.

    Used by the Live Console so agents that interact only via the bus (PM
    answering questions, observers, etc.) still appear in the per-agent panel
    even when they have zero tool_calls.

    Returns: {agent_id: {"sent": N, "received": N}}
    """
    conn = _get_conn()

    where = " WHERE 1=1"
    params: list[Any] = []
    if storypack_id:
        where += " AND storypack_id = ?"
        params.append(storypack_id)
    if run_id:
        where += " AND run_id = ?"
        params.append(run_id)

    sent_rows = conn.execute(
        "SELECT from_agent AS agent, COUNT(*) AS n FROM agent_comms"
        + where + " GROUP BY from_agent",
        params,
    ).fetchall()
    received_rows = conn.execute(
        "SELECT to_agent AS agent, COUNT(*) AS n FROM agent_comms"
        + where + " GROUP BY to_agent",
        params,
    ).fetchall()
    conn.close()

    result: dict[str, dict[str, int]] = {}
    for r in sent_rows:
        agent = r["agent"]
        if not agent or agent.startswith("topic:") or agent in ("<broadcast>", "<reply>"):
            continue
        result.setdefault(agent, {"sent": 0, "received": 0})["sent"] = int(r["n"])
    for r in received_rows:
        agent = r["agent"]
        if not agent or agent.startswith("topic:") or agent in ("<broadcast>", "<reply>"):
            continue
        result.setdefault(agent, {"sent": 0, "received": 0})["received"] = int(r["n"])
    return result


# --- Agent Inbox (durable message queue for the in-process bus) ---

def save_inbox_message(storypack_id: str, run_id: str, correlation_id: str,
                       from_agent: str, to_agent: str, event_type: str,
                       topic: str, story_id: str | None,
                       payload_json: str, summary: str,
                       status: str = "pending") -> int:
    conn = _get_conn()
    cursor = conn.execute(
        "INSERT INTO agent_inbox (storypack_id, run_id, correlation_id, from_agent, "
        "to_agent, event_type, topic, story_id, payload_json, summary, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (storypack_id, run_id, correlation_id, from_agent, to_agent,
         event_type, topic, story_id, payload_json, summary, status, _now_iso()),
    )
    msg_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return msg_id


def update_inbox_status(message_id: int, status: str,
                        reply_payload_json: str | None = None) -> None:
    conn = _get_conn()
    now = _now_iso()
    if status == "delivered":
        conn.execute(
            "UPDATE agent_inbox SET status = ?, delivered_at = ? WHERE id = ?",
            (status, now, message_id),
        )
    elif status == "replied":
        conn.execute(
            "UPDATE agent_inbox SET status = ?, replied_at = ?, reply_payload_json = ? WHERE id = ?",
            (status, now, reply_payload_json or "{}", message_id),
        )
    else:
        conn.execute(
            "UPDATE agent_inbox SET status = ? WHERE id = ?",
            (status, message_id),
        )
    conn.commit()
    conn.close()


def list_inbox_messages(to_agent: str | None = None, run_id: str | None = None,
                        status: str | None = None, limit: int = 100) -> list[dict]:
    conn = _get_conn()
    query = "SELECT * FROM agent_inbox WHERE 1=1"
    params: list[Any] = []
    if to_agent:
        query += " AND to_agent = ?"
        params.append(to_agent)
    if run_id:
        query += " AND run_id = ?"
        params.append(run_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Workspace chat (dashboard copilot) ---

def save_workspace_chat_session(session_id: str, project_id: str) -> None:
    conn = _get_conn()
    now = _now_iso()
    row = conn.execute("SELECT id FROM workspace_chat_sessions WHERE id = ?", (session_id,)).fetchone()
    if row:
        conn.execute(
            "UPDATE workspace_chat_sessions SET project_id = ?, updated_at = ? WHERE id = ?",
            (project_id, now, session_id),
        )
    else:
        conn.execute(
            "INSERT INTO workspace_chat_sessions (id, project_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, project_id, now, now),
        )
    conn.commit()
    conn.close()


def touch_workspace_chat_session(session_id: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE workspace_chat_sessions SET updated_at = ? WHERE id = ?",
        (_now_iso(), session_id),
    )
    conn.commit()
    conn.close()


def add_workspace_chat_message(session_id: str, role: str, content: str) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO workspace_chat_messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (session_id, role, content, _now_iso()),
    )
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    return mid


def list_workspace_chat_messages(session_id: str, limit: int = 40) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM workspace_chat_messages WHERE session_id = ? ORDER BY id ASC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_workspace_chat_edit(session_id: str, project_id: str, path: str, summary: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO workspace_chat_edits (session_id, project_id, path, summary, created_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, project_id, path, summary[:2000], _now_iso()),
    )
    conn.commit()
    conn.close()


def list_workspace_chat_edits(session_id: str | None = None, limit: int = 50) -> list[dict]:
    conn = _get_conn()
    if session_id:
        rows = conn.execute(
            "SELECT * FROM workspace_chat_edits WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM workspace_chat_edits ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Backward compatibility with JSON-based callers ---

def requirement_path(req_id: str) -> Path:
    return STATE_DIR / f"requirement_{req_id}.json"


def storypack_path(pack_id: str) -> Path:
    return STATE_DIR / f"storypack_{pack_id}.json"


def save(path: Path, data: dict) -> None:
    """Legacy JSON save -- also writes to SQLite for new callers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    if "requirement_" in path.stem and "text" in data:
        save_requirement(data.get("id", path.stem), data["text"], data.get("submitted_at"))
    elif "storypack_" in path.stem and "stories" in data:
        stories_raw = data["stories"]
        if stories_raw and hasattr(stories_raw[0], "model_dump"):
            stories_raw = [s.model_dump() for s in stories_raw]
        save_storypack(
            data.get("id", path.stem),
            data.get("requirement_id", ""),
            data.get("requirement_text", ""),
            stories_raw,
            data.get("status", "pending_review"),
            data.get("created_at"),
            project_id=data.get("project_id", "default"),
        )


def load(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path, "r") as f:
        return json.load(f)
