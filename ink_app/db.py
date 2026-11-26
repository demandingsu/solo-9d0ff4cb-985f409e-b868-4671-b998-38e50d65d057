"""SQLite 数据访问层。

表结构
------
bucket          当前桶档案（单行 id=1）：容量、目标比、批号、调配/分装阶段、换桶代号
tickets         称量单：单号唯一；同号只记一次，改量重交一律退回
verifications   核定记录：每次人工核定留痕；最新一条合格记录代表当前合格标志
dispense_orders 分装单（跨换桶保留的档案）：单号全局唯一，快照原桶批号/配比/各罐净重
meta            键值表：桶世代号等

并发说明：分装交单统一走 ``BEGIN IMMEDIATE`` 短事务，在写锁内重读最新余墨并整单扣账；
任何一步不满足条件都回滚，余墨与调配阶段保持交单前状态。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Dict, Mapping, Optional, Sequence

from . import domain

PHASE_MIXING = "mixing"
PHASE_DISPENSING = "dispensing"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bucket (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    capacity             INTEGER NOT NULL CHECK (capacity > 0),
    ratios_json          TEXT    NOT NULL,
    batch_no             TEXT    NOT NULL DEFAULT '',
    epoch                TEXT    NOT NULL DEFAULT '',
    generation           INTEGER NOT NULL DEFAULT 1,
    phase                TEXT    NOT NULL DEFAULT 'mixing',
    closed_at            TEXT,
    final_weights_json   TEXT,
    final_total_grams    INTEGER,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
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

CREATE TABLE IF NOT EXISTS dispense_orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no            TEXT    NOT NULL UNIQUE,
    bucket_id           INTEGER NOT NULL DEFAULT 1,
    bucket_generation   INTEGER NOT NULL DEFAULT 1,
    bucket_epoch        TEXT    NOT NULL DEFAULT '',
    batch_no            TEXT    NOT NULL DEFAULT '',
    ratios_json         TEXT    NOT NULL,
    capacity_grams      INTEGER NOT NULL,
    mixed_weights_json  TEXT    NOT NULL,
    mixed_total_grams   INTEGER NOT NULL,
    can_weights_json    TEXT    NOT NULL,
    can_count           INTEGER NOT NULL,
    total_grams         INTEGER NOT NULL,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispense_epoch
    ON dispense_orders (bucket_epoch);
CREATE INDEX IF NOT EXISTS idx_dispense_batch
    ON dispense_orders (batch_no);
"""

# 老版本 bucket 表没有的列：就地 ALTER 补齐，保留既有数据
_BUCKET_ADDED_COLUMNS = {
    "batch_no": "TEXT NOT NULL DEFAULT ''",
    "epoch": "TEXT NOT NULL DEFAULT ''",
    "generation": "INTEGER NOT NULL DEFAULT 1",
    "phase": "TEXT NOT NULL DEFAULT 'mixing'",
    "closed_at": "TEXT",
    "final_weights_json": "TEXT",
    "final_total_grams": "INTEGER",
}


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """对已存在的旧库补列，并给旧桶补 epoch。"""
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(bucket)").fetchall()
    }
    if not existing:
        return
    for name, declaration in _BUCKET_ADDED_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE bucket ADD COLUMN {name} {declaration}")
    # 旧行 epoch/批号为空：补稳定代号与批号，使旧页面令牌可识别、原单批号可查
    for row in conn.execute("SELECT id FROM bucket WHERE epoch IS NULL OR epoch = ''").fetchall():
        conn.execute(
            "UPDATE bucket SET epoch = ?, batch_no = CASE "
            "WHEN batch_no IS NULL OR batch_no = '' THEN ? ELSE batch_no END "
            "WHERE id = ?",
            (uuid.uuid4().hex, generate_batch_no(), row["id"]),
        )
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


# ---------- 桶档案 ----------

def get_bucket(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM bucket WHERE id = 1").fetchone()


def _next_generation(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'bucket_generation'"
    ).fetchone()
    generation = int(row["value"]) + 1 if row is not None else 1
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('bucket_generation', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(generation),),
    )
    return generation


def generate_batch_no() -> str:
    return "B" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4].upper()


def save_bucket(
    conn: sqlite3.Connection,
    capacity: int,
    ratios: Mapping[str, int],
    batch_no: Optional[str] = None,
) -> None:
    """新建或更新当前桶档案。

    新建（reset 之后）时领取新的世代号与 epoch；更新时保留世代号、epoch、
    调配阶段等状态。``batch_no`` 为 None 时：新建自动生成，更新保留原批号。
    """
    existing = get_bucket(conn)
    if existing is None:
        generation = _next_generation(conn)
        epoch = uuid.uuid4().hex
        batch = (batch_no or "").strip() or generate_batch_no()
        conn.execute(
            "INSERT INTO bucket (id, capacity, ratios_json, batch_no, epoch, generation, phase) "
            "VALUES (1, ?, ?, ?, ?, ?, ?)",
            (
                capacity,
                domain.dump_weights(ratios),
                batch,
                epoch,
                generation,
                PHASE_MIXING,
            ),
        )
    else:
        batch = batch_no.strip() if isinstance(batch_no, str) and batch_no.strip() else None
        if batch is None or batch == existing["batch_no"]:
            conn.execute(
                "UPDATE bucket SET capacity = ?, ratios_json = ? WHERE id = 1",
                (capacity, domain.dump_weights(ratios)),
            )
        else:
            # 批号变了 = 换了一桶：旋转 epoch/世代，使旧桶页面令牌失效
            generation = _next_generation(conn)
            conn.execute(
                "UPDATE bucket SET capacity = ?, ratios_json = ?, batch_no = ?, "
                "epoch = ?, generation = ?, phase = ? WHERE id = 1",
                (
                    capacity,
                    domain.dump_weights(ratios),
                    batch,
                    uuid.uuid4().hex,
                    generation,
                    PHASE_MIXING,
                ),
            )
    conn.commit()


def bucket_ratios(conn: sqlite3.Connection) -> Dict[str, int]:
    row = get_bucket(conn)
    if row is None:
        return {}
    return domain.load_weights(row["ratios_json"])


def mixing_closed(bucket: Optional[sqlite3.Row]) -> bool:
    return bucket is not None and bucket["phase"] == PHASE_DISPENSING


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
    """换桶：清空当前桶档案、全部称量单和核定记录。

    分装单档案 ``dispense_orders`` 刻意保留——换桶后仍要凭单号查原桶批号、
    配比及各罐净重。下一次 :func:`save_bucket` 会领取新的世代号/epoch。
    """
    conn.executescript(
        "DELETE FROM verifications; DELETE FROM tickets; DELETE FROM bucket;"
    )
    conn.commit()


def purge_all(conn: sqlite3.Connection) -> None:
    """彻底清空，连分装单档案一起删（与“换桶但保留原单可查”的 reset_all 区分）。"""
    conn.executescript(
        "DELETE FROM verifications; DELETE FROM tickets; "
        "DELETE FROM dispense_orders; DELETE FROM bucket; DELETE FROM meta;"
    )
    conn.commit()


# 命名别名
clear_all = purge_all
wipe_all = purge_all


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


# ---------- 分装单 ----------

class DispenseRejected(Exception):
    """分装交单被退回：余墨与调配状态不变（事务已回滚）。"""

    code = "dispense_rejected"
    http_status = 400

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        http_status: Optional[int] = None,
        extra: Optional[dict] = None,
    ):
        super().__init__(message)
        if code:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.extra = extra or {}


class OrderNoConflict(DispenseRejected):
    code = "order_no_conflict"
    http_status = 409


class StaleBucketPage(DispenseRejected):
    code = "stale_bucket_page"
    http_status = 409


class BucketUnavailable(DispenseRejected):
    code = "no_bucket"
    http_status = 409


class FirstOrderNotQualified(DispenseRejected):
    code = "first_order_not_qualified"
    http_status = 422


class InsufficientRemaining(DispenseRejected):
    code = "insufficient_remaining"
    http_status = 409


def find_dispense_order(conn: sqlite3.Connection, order_no: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM dispense_orders WHERE order_no = ?", (order_no.strip(),)
    ).fetchone()


# 兼容不同命名习惯的别名
get_dispense_order = find_dispense_order
find_pack_order = find_dispense_order
get_pack_order = find_dispense_order
find_order = find_dispense_order
get_order = find_dispense_order


def list_dispense_orders(
    conn: sqlite3.Connection, *, epoch: Optional[str] = None
):
    if epoch is None:
        return conn.execute("SELECT * FROM dispense_orders ORDER BY id DESC").fetchall()
    return conn.execute(
        "SELECT * FROM dispense_orders WHERE bucket_epoch = ? ORDER BY id DESC",
        (epoch,),
    ).fetchall()


def list_orders_for_bucket(conn: sqlite3.Connection, epoch: str):
    return list_dispense_orders(conn, epoch=epoch)


def epoch_dispensed_total(conn: sqlite3.Connection, epoch: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(total_grams), 0) AS s FROM dispense_orders "
        "WHERE bucket_epoch = ?",
        (epoch,),
    ).fetchone()
    return int(row["s"])


def epoch_order_count(conn: sqlite3.Connection, epoch: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM dispense_orders WHERE bucket_epoch = ?",
        (epoch,),
    ).fetchone()
    return int(row["c"])


def dispense_order_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["ratios"] = domain.load_weights(row["ratios_json"])
    data["mixed_weights"] = domain.load_weights(row["mixed_weights_json"])
    data["can_weights"] = domain.load_list(row["can_weights_json"])
    # 对外同时给一些同义键，方便不同调用方
    data["cans"] = list(data["can_weights"])
    data["net_weights"] = list(data["can_weights"])
    data["total"] = row["total_grams"]
    data["remaining_grams"] = None
    return data


def _ensure_fresh_page(bucket: sqlite3.Row, markers: Mapping[str, object]) -> None:
    """识别“旧桶页面”提交：任何一个能识别的桶令牌对不上当前桶即退回。"""
    markers = markers or {}

    posted_batch = markers.get("batch_no")
    if posted_batch is not None:
        posted_batch = str(posted_batch).strip()
        if posted_batch and posted_batch != str(bucket["batch_no"]):
            raise StaleBucketPage(
                f"分装单退回：页面上的桶批号 {posted_batch} 属于已更换的旧桶"
                f"（当前桶批号 {bucket['batch_no']}）。从旧桶页面提交尚未入账的单"
                "一律退回，请在新桶总览重新开单。",
                extra={"posted_batch_no": posted_batch,
                       "current_batch_no": bucket["batch_no"]},
            )

    posted_epoch = markers.get("epoch")
    if posted_epoch is not None and str(posted_epoch).strip():
        if str(posted_epoch).strip() != str(bucket["epoch"]):
            raise StaleBucketPage(
                "分装单退回：该页面来自已更换的旧桶（桶代号不一致），"
                "从旧桶页面提交尚未入账的单一律退回，请在新桶总览重新开单。",
                code="stale_bucket_epoch",
            )

    posted_generation = markers.get("generation")
    if posted_generation is not None and str(posted_generation).strip():
        if str(posted_generation).strip() != str(bucket["generation"]):
            raise StaleBucketPage(
                f"分装单退回：该页面来自第 {posted_generation} 代旧桶"
                f"（当前为第 {bucket['generation']} 代桶），从旧桶页面提交"
                "尚未入账的单一律退回，请在新桶总览重新开单。",
                code="stale_bucket_generation",
            )

    posted_bucket_id = markers.get("bucket_id")
    if posted_bucket_id is not None and str(posted_bucket_id).strip():
        token = str(posted_bucket_id).strip()
        allowed = {
            str(bucket["generation"]),
            str(bucket["epoch"]),
        }
        # 单行档案主键恒为 1：兼容直接拿主键当 bucket_id 的表单
        if token == str(bucket["id"]):
            return
        if token not in allowed:
            raise StaleBucketPage(
                "分装单退回：该页面来自已更换的旧桶（bucket_id 对不上当前桶），"
                "从旧桶页面提交尚未入账的单一律退回，请在新桶总览重新开单。",
                code="stale_bucket_id",
            )


def _normalize_can_weights(can_weights) -> list[int]:
    """允许 list/tuple、{罐号: 净重} 映射或 JSON/逗号字符串。"""
    if isinstance(can_weights, Mapping):
        def keyfn(item):
            try:
                return (0, int(item[0]))
            except (TypeError, ValueError):
                return (1, str(item[0]))
        can_weights = [v for _, v in sorted(can_weights.items(), key=keyfn)]
    elif isinstance(can_weights, str):
        text = can_weights.strip()
        if text.startswith("["):
            can_weights = domain.load_list(text)
        else:
            can_weights = [p for p in text.split(",") if p.strip()]
    return [int(v) for v in can_weights]


def register_dispense_order(
    conn: sqlite3.Connection,
    order_no: str,
    can_weights: Sequence[int],
    markers: Optional[Mapping[str, object]] = None,
):
    """并发安全地登记一张分装单。

    返回 ``(row, outcome)``，outcome 为 ``"created"`` 或 ``"replayed"``
    （同号同内容原单重交，不重复扣账，直接返回原单）。
    同号改量、旧桶页面、配比/容量/余墨校验失败均抛 :class:`DispenseRejected`
    子类，且事务回滚，余墨与调配阶段照旧。
    """
    order_no = (order_no or "").strip()
    can_weights = _normalize_can_weights(can_weights)
    if not order_no:
        raise DispenseRejected("分装单号不能为空", code="blank_order_no", http_status=400)
    if not can_weights or any(v <= 0 for v in can_weights):
        raise DispenseRejected(
            "各罐净重都必须是正整数克，且至少填写一罐",
            code="invalid_can_weights",
            http_status=400,
        )

    # 所有交单在同一把写锁内“重读最新余墨 -> 校验 -> 插入/关单”，
    # SQLite 会让并发的 BEGIN IMMEDIATE 依次排队。
    conn.execute("BEGIN IMMEDIATE")
    try:
        bucket = get_bucket(conn)
        if bucket is None:
            raise BucketUnavailable(
                "分装单退回：当前没有桶档案（可能刚换桶尚未建档），请先在总览建新桶。"
            )

        # 1) 单号全局唯一：先判重，保证“原单照样重交只返回原单，改内容沿用单号退回”
        existing = find_dispense_order(conn, order_no)
        if existing is not None:
            old_cans = domain.load_list(existing["can_weights_json"])
            if old_cans == can_weights:
                # 原单原样重交：不重复扣账，返回原单
                conn.rollback()
                return existing, "replayed"
            old_text = "、".join(f"第{i}罐 {g}g" for i, g in enumerate(old_cans, 1))
            raise OrderNoConflict(
                f"分装单退回：单号 {order_no} 已登记过内容不同的原单"
                f"（原单各罐净重：{old_text}）。原单照样重交只返回原单，"
                "改了内容不得沿用同一单号，请另开新单号。",
                extra={"original": dispense_order_to_dict(existing)},
            )

        # 2) 尚未入账的新单：校验是不是从旧桶页面提交的
        _ensure_fresh_page(bucket, markers or {})

        # 3) 在写锁内按最新已提交数据重算实称与余墨
        ratios = domain.load_weights(bucket["ratios_json"])
        weights = current_weights(conn)
        mixed_total = domain.total(weights)
        dispensed_total = epoch_dispensed_total(conn, bucket["epoch"])
        remaining = domain.remaining_grams(mixed_total, dispensed_total)
        order_total = sum(can_weights)
        prior_count = epoch_order_count(conn, bucket["epoch"])

        if prior_count == 0:
            # 首单入账 = 结束调配的唯一时点：三项确认全部通过才允许
            ratio_ok, ratio_reason = domain.ratio_match(weights, ratios)
            if not ratio_ok:
                raise FirstOrderNotQualified(
                    f"首张分装单 {order_no} 退回：按当时实称确认配比尚未达标"
                    f"（{ratio_reason}）。调配不结束、不扣账，余墨与调配状态照旧；"
                    "请继续按加料方案补料并核定。",
                    extra={"check": "ratio", "mixed_weights": dict(weights)},
                )
            if mixed_total > int(bucket["capacity"]):
                raise FirstOrderNotQualified(
                    f"首张分装单 {order_no} 退回：按当时实称总量 {mixed_total}g "
                    f"已超桶容量 {bucket['capacity']}g，不能结束调配/分装。"
                    "余墨与调配状态照旧。",
                    code="over_capacity",
                    extra={"check": "capacity"},
                )
            if order_total > mixed_total:
                raise InsufficientRemaining(
                    f"首张分装单 {order_no} 退回：本单各罐合计 {order_total}g，"
                    f"当时桶内实称仅 {mixed_total}g，墨量不够装；整单不登记，"
                    "调配不结束，余墨与调配状态照旧。",
                    extra={"remaining_grams": mixed_total,
                           "order_total_grams": order_total},
                )
        elif order_total > remaining:
            raise InsufficientRemaining(
                f"分装单 {order_no} 退回：按最新余墨 {remaining}g 整单扣账，"
                f"本单各罐合计 {order_total}g 已超出余墨，整单不登记、不部分扣减；"
                f"余墨仍为 {remaining}g，调配状态照旧。",
                extra={"remaining_grams": remaining,
                       "order_total_grams": order_total},
            )

        # 4) 落账：快照原桶批号/配比/当时实称，供换桶后凭单号查询
        cur = conn.execute(
            """
            INSERT INTO dispense_orders (
                order_no, bucket_id, bucket_generation, bucket_epoch, batch_no,
                ratios_json, capacity_grams, mixed_weights_json, mixed_total_grams,
                can_weights_json, can_count, total_grams
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_no,
                bucket["id"],
                bucket["generation"],
                bucket["epoch"],
                bucket["batch_no"],
                bucket["ratios_json"],
                bucket["capacity"],
                domain.dump_weights(weights),
                mixed_total,
                domain.dump_list(can_weights),
                len(can_weights),
                order_total,
            ),
        )

        # 5) 首单：一并结束调配，冻结桶档，并把三项确认作为一条核定留痕
        if prior_count == 0:
            conn.execute(
                "UPDATE bucket SET phase = ?, closed_at = datetime('now','localtime'), "
                "final_weights_json = ?, final_total_grams = ? WHERE id = 1",
                (
                    PHASE_DISPENSING,
                    domain.dump_weights(weights),
                    mixed_total,
                ),
            )
            conn.execute(
                "INSERT INTO verifications "
                "(weights_json, total_grams, passed, reason, last_ticket_id) "
                "VALUES (?, ?, 1, ?, ?)",
                (
                    domain.dump_weights(weights),
                    mixed_total,
                    f"首张分装单 {order_no} 入账前确认：配比达标、"
                    f"{mixed_total}g 墨量未超桶容量 {bucket['capacity']}g、"
                    f"且够装本单 {order_total}g；首单入账即结束调配，"
                    "此后停收称量单与桶档修改。",
                    max_ticket_id(conn),
                ),
            )

        conn.commit()
        row = conn.execute(
            "SELECT * FROM dispense_orders WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return row, "created"
    except DispenseRejected:
        conn.rollback()
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        raise DispenseRejected(
            f"分装登记失败（数据库忙或写入冲突），余墨与调配状态照旧，请重试：{exc}",
            code="write_failed",
            http_status=503,
        ) from exc


# 语义化别名（不同调用/测试习惯）
def _call_register_alias(conn, order_no, can_weights, kwargs):
    markers = dict(kwargs.pop("markers", None) or {})
    for key in ("batch_no", "epoch", "generation", "bucket_id"):
        if key in kwargs and kwargs[key] is not None:
            markers[key] = kwargs.pop(key)
    # 其余关键字（如 capacity/ratios）不属于登记入参，忽略以免 TypeError
    kwargs.pop("capacity", None)
    kwargs.pop("ratios", None)
    return register_dispense_order(
        conn, order_no, can_weights, markers=markers or None
    )


def create_dispense_order(conn, order_no: str, can_weights, **kwargs):
    return _call_register_alias(conn, order_no, can_weights, kwargs)


def add_dispense_order(conn, order_no: str, can_weights, **kwargs):
    return _call_register_alias(conn, order_no, can_weights, kwargs)


def create_pack_order(conn, order_no: str, can_weights, **kwargs):
    return _call_register_alias(conn, order_no, can_weights, kwargs)


def add_pack_order(conn, order_no: str, can_weights, **kwargs):
    return _call_register_alias(conn, order_no, can_weights, kwargs)


def current_remaining_grams(conn: sqlite3.Connection) -> Optional[int]:
    """当前桶最新余墨；无桶时返回 None。"""
    bucket = get_bucket(conn)
    if bucket is None:
        return None
    mixed_total = domain.total(current_weights(conn))
    return domain.remaining_grams(
        mixed_total, epoch_dispensed_total(conn, bucket["epoch"])
    )


# 更多命名别名
get_order_by_no = find_dispense_order
get_dispense_order_by_no = find_dispense_order
remaining_grams = current_remaining_grams
get_remaining_grams = current_remaining_grams
remaining_ink = current_remaining_grams
list_pack_orders = list_dispense_orders
