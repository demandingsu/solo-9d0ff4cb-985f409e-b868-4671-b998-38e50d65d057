"""SQLite 数据访问层。

表结构
------
bucket        单桶档案：容量 + 各色目标质量比（整个应用只跟踪一桶专色墨）
tickets       称量单：单号唯一；同号只记一次，改量重交一律退回
verifications 核定记录：每次人工核定留痕；最新一条合格记录代表当前合格标志
"""

from __future__ import annotations

import sqlite3
from typing import Dict, Mapping, Optional

from . import domain

SCHEMA = """
CREATE TABLE IF NOT EXISTS bucket (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    capacity    INTEGER NOT NULL CHECK (capacity > 0),
    ratios_json TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS tickets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_no     TEXT    NOT NULL UNIQUE,
    weights_json  TEXT    NOT NULL,
    total_grams   INTEGER NOT NULL,
    entered_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS verifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    weights_json   TEXT    NOT NULL,
    total_grams    INTEGER NOT NULL,
    passed         INTEGER NOT NULL,
    reason         TEXT    NOT NULL,
    last_ticket_id INTEGER,
    revoked        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------- 桶档案 ----------

def get_bucket(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM bucket WHERE id = 1").fetchone()


def save_bucket(conn: sqlite3.Connection, capacity: int, ratios: Mapping[str, int]) -> None:
    conn.execute(
        "INSERT INTO bucket (id, capacity, ratios_json) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET capacity = excluded.capacity, "
        "ratios_json = excluded.ratios_json, created_at = datetime('now', 'localtime')",
        (capacity, domain.dump_weights(ratios)),
    )
    conn.commit()


def bucket_ratios(conn: sqlite3.Connection) -> Dict[str, int]:
    row = get_bucket(conn)
    if row is None:
        return {}
    return domain.load_weights(row["ratios_json"])


# ---------- 称量单 ----------

class DuplicateTicketError(Exception):
    """同号称量单已登记（无论克数是否变化，一律退回）。"""

    def __init__(self, ticket_no: str, existing: Mapping[str, int]):
        super().__init__(f"称量单号 {ticket_no} 已登记，同号单据只记一次")
        self.ticket_no = ticket_no
        self.existing = dict(existing)


def list_tickets(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM tickets ORDER BY id"
    ).fetchall()


def find_ticket(conn: sqlite3.Connection, ticket_no: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tickets WHERE ticket_no = ?", (ticket_no,)
    ).fetchone()


def add_ticket(
    conn: sqlite3.Connection, ticket_no: str, weights: Mapping[str, int]
) -> None:
    """登记一张称量单；同号（含改量重交）直接退回。"""
    existing = find_ticket(conn, ticket_no)
    if existing is not None:
        raise DuplicateTicketError(ticket_no, domain.load_weights(existing["weights_json"]))
    conn.execute(
        "INSERT INTO tickets (ticket_no, weights_json, total_grams) VALUES (?, ?, ?)",
        (
            ticket_no.strip(),
            domain.dump_weights(weights),
            domain.total(weights),
        ),
    )
    conn.commit()


def current_weights(conn: sqlite3.Connection) -> Dict[str, int]:
    """按入账顺序累加全部称量单，得到桶内各色最新实称克数。"""
    ratios = bucket_ratios(conn)
    weights = {color: 0 for color in ratios}
    for row in list_tickets(conn):
        for color, grams in domain.load_weights(row["weights_json"]).items():
            weights[color] = weights.get(color, 0) + grams
    return weights


def reset_all(conn: sqlite3.Connection) -> None:
    """清空一桶的全部数据（换桶/重新建档用）。"""
    conn.executescript(
        "DELETE FROM verifications; DELETE FROM tickets; DELETE FROM bucket;"
    )
    conn.commit()


# ---------- 核定 ----------

def latest_verification(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM verifications ORDER BY id DESC LIMIT 1"
    ).fetchone()


def max_ticket_id(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute("SELECT MAX(id) AS m FROM tickets").fetchone()
    return row["m"] if row and row["m"] is not None else None


def record_verification(
    conn: sqlite3.Connection,
    weights: Mapping[str, int],
    passed: bool,
    reason: str,
) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO verifications (weights_json, total_grams, passed, reason, last_ticket_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            domain.dump_weights(weights),
            domain.total(weights),
            1 if passed else 0,
            reason,
            max_ticket_id(conn),
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM verifications WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def list_verifications(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM verifications ORDER BY id DESC"
    ).fetchall()


def certified_state(conn: sqlite3.Connection, capacity: int, ratios: Mapping[str, int]):
    """计算当前合格标志及自动撤标状态。

    合格标志只由"最新一次人工核定通过"持有；核定后若有新称量单入账，
    按最新实称复核，不达标即自动撤标，补好后必须重新核定。
    """
    latest = latest_verification(conn)
    weights = current_weights(conn)
    if latest is None:
        return {
            "certified": False,
            "revoked": False,
            "latest": None,
            "reason": "尚未核定",
            "weights": weights,
        }
    # 核定之后若有新称量单入账（迟到补登），按最新实称重新复核
    new_after = (max_ticket_id(conn) or 0) > (latest["last_ticket_id"] or 0)
    # 容量/目标比被改小或修改后，同样按最新桶参数复核当前实称
    compliant, reason = domain.is_compliant(weights, ratios, capacity)
    if not latest["passed"]:
        status = {
            "certified": False,
            "revoked": False,
            "latest": latest,
            "reason": f"最近一次核定未通过：{latest['reason']}",
            "weights": weights,
        }
    elif latest["revoked"]:
        # 已被自动撤标：即使后来补料恢复达标，合格标志也不自动恢复，须人工重新核定
        status = {
            "certified": False,
            "revoked": True,
            "latest": latest,
            "reason": (
                "合格标志已自动撤销"
                + ("，当前实称已恢复达标，请重新核定" if compliant else f"（{reason}），补好后须重新核定")
            ),
            "weights": weights,
        }
    elif not compliant:
        # 任何原因导致最新实称不再达标（迟到补登或桶参数变更）：自动撤标并落库
        conn.execute("UPDATE verifications SET revoked = 1 WHERE id = ?", (latest["id"],))
        conn.commit()
        latest = latest_verification(conn)
        trigger = "核定后有补登单据且" if new_after else "桶参数变更后"
        status = {
            "certified": False,
            "revoked": True,
            "latest": latest,
            "reason": f"{trigger}已不达标（{reason}），合格标志自动撤销，补好后须重新核定",
            "weights": weights,
        }
    elif new_after:
        status = {
            "certified": True,
            "revoked": False,
            "latest": latest,
            "reason": "核定后有补登单据，按最新实称复核仍达标，合格标志保留",
            "weights": weights,
        }
    else:
        status = {
            "certified": bool(latest["passed"]),
            "revoked": False,
            "latest": latest,
            "reason": latest["reason"],
            "weights": weights,
        }
    return status
