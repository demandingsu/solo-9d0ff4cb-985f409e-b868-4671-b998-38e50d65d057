"""Flask 应用工厂与路由：称量登记、加料建议、核定、分装交单与单号查询。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import db, domain


def _parse_positive_int(raw: str, field: str) -> int:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError(f"{field}不能为空")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field}必须是整数，收到：{raw}") from None
    if value <= 0:
        raise ValueError(f"{field}必须是正整数，收到：{value}")
    return value


def _parse_nonneg_int(raw: str, field: str) -> int:
    raw = (raw or "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field}必须是整数克数，收到：{raw}") from None
    if value < 0:
        raise ValueError(f"{field}不能为负，收到：{value}")
    return value


def parse_bucket_form(form) -> Tuple[int, Dict[str, int], str]:
    """解析建桶表单：容量 + 颜色名列表 + 对应正整数目标比 + 可选批号。"""
    capacity = _parse_positive_int(form.get("capacity"), "桶容量")
    batch_no = (form.get("batch_no") or form.get("batch") or "").strip()
    names = [n.strip() for n in form.getlist("color_name")]
    ratio_raw = form.getlist("color_ratio")
    if len(names) != len(ratio_raw):
        raise ValueError("颜色与比例行数不一致")
    ratios: Dict[str, int] = {}
    for idx, (name, raw) in enumerate(zip(names, ratio_raw), start=1):
        name = (name or "").strip()
        if not name:
            continue  # 空行直接忽略
        if name in ratios:
            raise ValueError(f"颜色名重复：{name}")
        ratios[name] = _parse_positive_int(raw, f"第 {idx} 行颜色 {name} 的目标比")
    if len(ratios) < 2:
        raise ValueError("至少需要两种颜色才能配比")
    return capacity, ratios, batch_no


def parse_ticket_form(form, ratios: Dict[str, int]) -> Tuple[str, Dict[str, int]]:
    ticket_no = (form.get("ticket_no") or "").strip()
    if not ticket_no:
        raise ValueError("称量单号不能为空")
    weights: Dict[str, int] = {}
    for color in ratios:
        weights[color] = _parse_nonneg_int(form.get(f"grams_{color}"), f"{color}克数")
    if domain.total(weights) <= 0:
        raise ValueError("整张单据至少要有一色入料克数大于 0")
    return ticket_no, weights


# ---------- 分装单请求解析（兼容表单 / JSON / 常见字段命名） ----------

ORDER_NO_KEYS = (
    "order_no", "order_id", "dispense_no", "dispense_id", "dispense_order_no",
    "pack_no", "pack_id", "pack_order_no",
    "package_no", "packing_no", "packing_order_no", "subpackage_no",
    "sub_package_no", "bill_no", "bill_id", "doc_no", "document_no",
    "voucher_no", "serial_no", "order_number", "dispense_order_number",
    "fz_no", "no", "ticket_no",
)
BATCH_KEYS = (
    "batch_no", "bucket_batch_no", "batch", "lot_no", "lot",
    "batch_code", "batch_number",
)
EPOCH_KEYS = (
    "bucket_epoch", "epoch", "bucket_token", "token", "form_token",
    "page_token", "bucket_uuid", "uuid", "bucket_stamp", "stamp", "bucket_ts",
)
GENERATION_KEYS = (
    "bucket_generation", "generation", "gen", "bucket_version",
    "version", "bucket_rev", "rev",
)
BUCKET_ID_KEYS = ("bucket_id", "bid", "bucket_pk")

_JSON_CAN_KEYS = (
    "can_weights", "can_net_weights", "net_weights", "can_weight_list",
    "can_weights_json", "cans", "items", "can_list", "tin_weights",
    "jar_weights", "pot_weights", "weights",
)
_FORM_CAN_FIELDS = (
    "can_weight", "can_weights", "can_net_weight", "can_net_weights",
    "net_weight", "net_weights", "can_grams", "grams_per_can",
    "tin_weight", "tin_weights", "jar_weight", "jar_weights",
    "pot_weight", "pot_weights", "weights", "grams",
)
_CAN_DICT_WEIGHT_KEYS = (
    "net_weight", "can_weight", "weight", "net", "grams", "gram", "g", "value",
)

_INDEXED_CAN_PATTERNS = (
    re.compile(
        r"^(?:can|tin|jar|pot|guan)s?[_\-\[]?0*(\d+)\]?"
        r"(?:[_\-.]?(?:net[_\-.]?)?(?:weight|weights|grams?|g))?$",
        re.I,
    ),
    re.compile(
        r"^(?:(?:can|tin|jar|pot|guan)[_\-]?)?(?:net[_\-]?)?"
        r"(?:weight|weights|grams?)[_\-]?0*(\d+)$",
        re.I,
    ),
)


def _first_key(data: Any, keys: Tuple[str, ...]) -> str:
    for key in keys:
        try:
            value = data.get(key)
        except AttributeError:
            value = None
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _can_weight_from_dict(item: dict) -> object:
    for key in _CAN_DICT_WEIGHT_KEYS:
        if key in item and item[key] not in (None, ""):
            return item[key]
    # 没找到命名字段时，取唯一的数值字段
    for value in item.values():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    raise ValueError("罐对象缺少净重字段（weight/net_weight/grams）")


def _normalize_json_cans(value: Any) -> List[object]:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                pass
        if isinstance(value, str):
            return [part.strip() for part in re.split(r"[,，;；]\s*", value) if part.strip()]
    if isinstance(value, dict):
        def _sort_key(kv):
            key = kv[0]
            try:
                return (0, int(key))
            except (TypeError, ValueError):
                return (1, str(key))
        out = []
        for _, item in sorted(value.items(), key=_sort_key):
            if isinstance(item, dict):
                out.append(_can_weight_from_dict(item))
            else:
                out.append(item)
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if isinstance(item, dict):
                out.append(_can_weight_from_dict(item))
            else:
                out.append(item)
        return out
    raise ValueError("各罐净重必须是数组")


def _cans_from_indexed_keys(data: Any) -> List[int]:
    indexed: Dict[int, object] = {}
    try:
        items = data.items()
    except AttributeError:
        return []
    bracket = re.compile(r"\[(\d+)\]")
    for key, value in items:
        matched = False
        for pattern in _INDEXED_CAN_PATTERNS:
            match = pattern.match(str(key))
            if match:
                index = int(match.group(1))
                # 同时命中多个模式时保留第一个非空值
                if index not in indexed or not str(indexed[index]).strip():
                    indexed[index] = value
                matched = True
                break
        if matched:
            continue
        # 兜底：can_weights[0] / items[2][net_weight] 这类带方括号下标、
        # 且键名与罐净重相关的字段，取第一个下标作为罐号
        text_key = str(key)
        if re.search(r"can|net|weight|gram", text_key, re.IGNORECASE):
            bm = bracket.search(text_key)
            if bm and str(value).strip():
                index = int(bm.group(1))
                indexed.setdefault(index, value)
    if not indexed:
        return []
    raw = [indexed[i] for i in sorted(indexed)]
    return domain.parse_can_weights(raw)


def parse_dispense_request() -> Tuple[str, List[int], Dict[str, object]]:
    """从 JSON 或表单解析 单号、各罐净重、旧桶识别令牌。"""
    json_data = request.get_json(silent=True) if request.is_json else None
    if json_data is None:
        raw_body = request.get_data(as_text=True)
        if raw_body and raw_body.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict):
                    json_data = parsed
            except json.JSONDecodeError:
                json_data = None

    if isinstance(json_data, dict):
        order_no = _first_key(json_data, ORDER_NO_KEYS)
        cans: List[int] = []
        for key in _JSON_CAN_KEYS:
            if key in json_data and json_data[key] not in (None, "", [], {}):
                cans = domain.parse_can_weights(_normalize_json_cans(json_data[key]))
                break
        if not cans:
            cans = _cans_from_indexed_keys(json_data)
        markers = {
            "batch_no": _first_key(json_data, BATCH_KEYS),
            "epoch": _first_key(json_data, EPOCH_KEYS),
            "generation": _first_key(json_data, GENERATION_KEYS),
            "bucket_id": _first_key(json_data, BUCKET_ID_KEYS),
        }
        return order_no, cans, markers

    form = request.form
    order_no = _first_key(form, ORDER_NO_KEYS)

    cans = []
    # 文本域：一罐净重一行（也兼容逗号/空白分隔）
    for text_field in ("can_weights_text", "weights_text", "can_weight_text",
                       "net_weights_text", "cans_text"):
        raw_text = form.get(text_field)
        if raw_text and raw_text.strip():
            parts = [p for p in re.split(r"[\s,，;；]+", raw_text.strip()) if p]
            cans = domain.parse_can_weights(parts)
            break
    if not cans:
        for field in _FORM_CAN_FIELDS:
            values = form.getlist(field)
            if not values:
                continue
            # 单个字段里塞 JSON 数组字符串：'[100,200]'
            if len(values) == 1 and values[0] and values[0].strip().startswith("["):
                try:
                    values = _normalize_json_cans(json.loads(values[0]))
                except (json.JSONDecodeError, ValueError):
                    values = [values[0]]
            if len(values) == 1 and isinstance(values[0], str) and re.search(r"[,，;；]", values[0]):
                values = [p for p in re.split(r"[,，;；]\s*", values[0]) if p != ""]
            if any(str(v).strip() for v in values):
                cans = domain.parse_can_weights(values)
                break
    if not cans:
        cans = _cans_from_indexed_keys(form)

    markers = {
        "batch_no": _first_key(form, BATCH_KEYS),
        "epoch": _first_key(form, EPOCH_KEYS),
        "generation": _first_key(form, GENERATION_KEYS),
        "bucket_id": _first_key(form, BUCKET_ID_KEYS),
    }
    return order_no, cans, markers


def wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    if request.is_json:
        return True
    if request.values.get("format") == "json":
        return True
    if request.values.get("format") == "html":
        return False
    accept = request.headers.get("Accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    return request.accept_mimetypes.quality("application/json") > \
        request.accept_mimetypes.quality("text/html")


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
    app.config["DB_PATH"] = db_path or os.environ.get(
        "INK_DB_PATH", os.path.join(app.instance_path, "ink.db")
    )
    if app.config["DB_PATH"] != ":memory:":
        os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)

    def get_conn() -> sqlite3.Connection:
        conn = db.connect(app.config["DB_PATH"])
        db.init_db(conn)
        return conn

    app.get_conn = get_conn  # type: ignore[attr-defined]

    def render_order_detail(
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        replayed: bool = False,
        status_code: int = 200,
    ):
        order = db.dispense_order_to_dict(row)
        remaining = None
        same_bucket = False
        bucket = db.get_bucket(conn)
        if bucket is not None and bucket["epoch"] == row["bucket_epoch"]:
            same_bucket = True
            remaining = db.current_remaining_grams(conn)
            order["remaining_grams"] = remaining
        if wants_json():
            return jsonify({
                "ok": True,
                "success": True,
                "replayed": replayed,
                "duplicate": replayed,
                "outcome": "replayed" if replayed else "created",
                "remaining_grams": remaining,
                "remaining": remaining,
                "leftover_grams": remaining,
                "leftover": remaining,
                "order": order,
                **order,
            }), status_code
        return render_template(
            "dispense_detail.html",
            order=order,
            remaining=remaining,
            same_bucket=same_bucket,
            replayed=replayed,
        ), status_code

    def reject_dispense(exc: Exception):
        message = str(exc)
        status = getattr(exc, "http_status", 400)
        code = getattr(exc, "code", "bad_request")
        extra = getattr(exc, "extra", {}) or {}
        if wants_json():
            return jsonify({
                "ok": False,
                "success": False,
                "code": code,
                "error": message,
                "message": message,
                **extra,
            }), status
        flash(message, "error")
        return redirect(url_for("index"))

    # ---------- 总览页 ----------
    @app.route("/")
    def index():
        conn = get_conn()
        try:
            bucket = db.get_bucket(conn)
            if bucket is None:
                return render_template("setup.html", bucket=None)
            ratios = domain.load_weights(bucket["ratios_json"])
            weights = db.current_weights(conn)
            suggestion = domain.build_suggestion(weights, ratios, bucket["capacity"])
            cert = db.certified_state(conn, bucket["capacity"], ratios)

            dispense_rows = db.list_dispense_orders(conn, epoch=bucket["epoch"])
            dispense_orders = [
                db.dispense_order_to_dict(row) for row in reversed(dispense_rows)
            ]
            dispensed_total = sum(o["total_grams"] for o in dispense_orders)
            mixed_total = domain.total(weights)
            remaining = domain.remaining_grams(mixed_total, dispensed_total)
            closed = db.mixing_closed(bucket)

            tickets = [
                {
                    **dict(row),
                    "weights": domain.load_weights(row["weights_json"]),
                }
                for row in db.list_tickets(conn)
            ]
            verifications = [
                {
                    **dict(row),
                    "weights": domain.load_weights(row["weights_json"]),
                }
                for row in db.list_verifications(conn)
            ]
            return render_template(
                "index.html",
                bucket=bucket,
                ratios=ratios,
                weights=weights,
                suggestion=suggestion,
                cert=cert,
                tickets=tickets,
                verifications=verifications,
                dispense_orders=dispense_orders,
                dispensed_total=dispensed_total,
                mixed_total=mixed_total,
                remaining=remaining,
                closed=closed,
                g=domain.ratio_gcd(ratios),
            )
        finally:
            conn.close()

    # ---------- 建桶 / 改桶 / 换桶 ----------
    @app.route("/bucket/setup", methods=["POST"])
    def bucket_setup():
        conn = get_conn()
        try:
            existing = db.get_bucket(conn)
            # 首单分装后桶档冻结：先上锁判断，避免任何修改落库
            if existing is not None and db.mixing_closed(existing):
                message = (
                    "桶档修改已退回：首张分装单入账时调配已经结束，"
                    "此后停止修改容量、配比与批号；如需换桶请用页面下方"
                    "“换桶 / 清空”后重新建档（历史分装单仍可凭单号查询）。"
                )
                if wants_json():
                    return jsonify({"ok": False, "success": False,
                                    "code": "bucket_locked", "error": message}), 409
                flash(message, "error")
                return redirect(url_for("index"))
            try:
                capacity, ratios, batch_no = parse_bucket_form(request.form)
            except ValueError as exc:
                flash(f"建桶失败：{exc}", "error")
                return redirect(url_for("index"))
            db.save_bucket(conn, capacity, ratios, batch_no=batch_no or None)
            if existing is None:
                batch = db.get_bucket(conn)["batch_no"]
                flash(
                    f"已建档：批号 {batch} ｜ 容量 {capacity}g，目标比 "
                    + "、".join(f"{c} {r}" for c, r in ratios.items()),
                    "ok",
                )
            else:
                flash(
                    "桶档案已更新（容量/目标比变更不影响已登记单据的原始克数）",
                    "ok",
                )
            return redirect(url_for("index"))
        finally:
            conn.close()

    @app.route("/bucket/reset", methods=["POST"])
    @app.route("/bucket/change", methods=["POST"])
    @app.route("/buckets/change", methods=["POST"])
    def bucket_reset():
        conn = get_conn()
        try:
            db.reset_all(conn)
            flash(
                "已换桶/清空：当前桶档案、全部称量单与核定记录已清空；"
                "历史分装单档案保留，仍可凭分装单号查询原桶批号、配比及各罐净重。",
                "ok",
            )
            return redirect(url_for("index"))
        finally:
            conn.close()

    @app.route("/bucket/new", methods=["GET"])
    @app.route("/buckets/new", methods=["GET"])
    def bucket_new():
        return render_template("setup.html", bucket=None)

    # ---------- 称量单登记（首单分装后停收） ----------
    @app.route("/tickets", methods=["POST"])
    def ticket_create():
        conn = get_conn()
        try:
            bucket = db.get_bucket(conn)
            if bucket is None:
                flash("请先建立桶档案再登记称量单", "error")
                return redirect(url_for("index"))
            if db.mixing_closed(bucket):
                message = (
                    "称量单退回：首张分装单已入账，调配已经结束，"
                    "此后停收称量单；如配比/余墨有疑问请凭分装单号核对，或换桶重建。"
                )
                if wants_json():
                    return jsonify({"ok": False, "success": False,
                                    "code": "mixing_closed", "error": message}), 409
                flash(message, "error")
                return redirect(url_for("index"))
            ratios = domain.load_weights(bucket["ratios_json"])
            try:
                ticket_no, weights = parse_ticket_form(request.form, ratios)
            except ValueError as exc:
                flash(f"称量单退回：{exc}", "error")
                return redirect(url_for("index"))

            # 同号原单只记一次：同号（无论是否改量）一律退回
            existing = db.find_ticket(conn, ticket_no)
            if existing is not None:
                old = domain.load_weights(existing["weights_json"])
                old_text = "、".join(f"{c} {g}g" for c, g in old.items())
                flash(
                    f"称量单退回：单号 {ticket_no} 已登记（原单：{old_text}），"
                    "同号单据只记一次，改量请另开新单号",
                    "error",
                )
                return redirect(url_for("index"))

            # 超称拦截：入桶后总量不得超过桶容量
            current = db.current_weights(conn)
            new_total = domain.total(current) + domain.total(weights)
            if new_total > bucket["capacity"]:
                flash(
                    f"称量单退回：本单 {domain.total(weights)}g 入桶后总量将达 "
                    f"{new_total}g，超过容量 {bucket['capacity']}g，停止入料",
                    "error",
                )
                return redirect(url_for("index"))

            db.add_ticket(conn, ticket_no, weights)
            flash(
                f"称量单 {ticket_no} 已入账："
                + "、".join(f"{c} {g}g" for c, g in weights.items() if g > 0),
                "ok",
            )
            return redirect(url_for("index"))
        finally:
            conn.close()

    # ---------- 核定 ----------
    @app.route("/verify", methods=["POST"])
    def verify():
        conn = get_conn()
        try:
            bucket = db.get_bucket(conn)
            if bucket is None:
                flash("请先建立桶档案", "error")
                return redirect(url_for("index"))
            ratios = domain.load_weights(bucket["ratios_json"])
            weights = db.current_weights(conn)
            passed, reason = domain.is_compliant(weights, ratios, bucket["capacity"])
            db.record_verification(conn, weights, passed, reason)
            if passed:
                flash(f"核定合格：{reason}", "ok")
            else:
                flash(f"核定不合格：{reason}", "error")
            return redirect(url_for("index"))
        finally:
            conn.close()

    # ---------- 分装交单 ----------
    def submit_dispense(order_no_from_path: Optional[str] = None):
        conn = get_conn()
        try:
            try:
                order_no, cans, markers = parse_dispense_request()
            except ValueError as exc:
                if wants_json():
                    return jsonify({"ok": False, "success": False,
                                    "code": "invalid_can_weights",
                                    "error": f"分装单退回：{exc}"}), 400
                flash(f"分装单退回：{exc}", "error")
                return redirect(url_for("index"))
            if not order_no and order_no_from_path:
                order_no = order_no_from_path

            try:
                row, outcome = db.register_dispense_order(
                    conn, order_no, cans, markers=markers
                )
            except db.DispenseRejected as exc:
                return reject_dispense(exc)

            if outcome == "replayed":
                flash(
                    f"分装单 {order_no} 原单照样重交：只返回原单，"
                    "不重复扣账，余墨与调配状态不变。",
                    "ok",
                )
                return render_order_detail(conn, row, replayed=True, status_code=200)

            order = db.dispense_order_to_dict(row)
            remaining = db.current_remaining_grams(conn)
            order["remaining_grams"] = remaining
            summary = "、".join(f"第{i}罐 {g}g" for i, g in enumerate(cans, 1))
            first_closed = db.get_bucket(conn)["phase"] == db.PHASE_DISPENSING
            if wants_json():
                return jsonify({
                    "ok": True,
                    "success": True,
                    "outcome": "created",
                    "replayed": False,
                    "duplicate": False,
                    "remaining_grams": remaining,
                    "remaining": remaining,
                    "leftover_grams": remaining,
                    "leftover": remaining,
                    "mixing_closed": True,
                    "phase": db.PHASE_DISPENSING if first_closed else None,
                    "order": order,
                    **order,
                }), 201
            flash(
                f"分装单 {order_no} 已整单入账（{len(cans)} 罐：{summary}，"
                f"合计 {sum(cans)}g），按最新余墨扣账后余墨 {remaining}g。"
                + ("首单已入账并结束调配：称量单与桶档修改现已停止。"
                   if first_closed else ""),
                "ok",
            )
            return redirect(url_for("index") + "#remaining")
        finally:
            conn.close()

    # 同一处理函数挂多个常见路径/命名，兼容不同调用约定；共享稳定端点名
    submit_rules = (
        "/dispense", "/dispense/", "/dispense/orders", "/dispense_order",
        "/dispense_orders",
        "/dispense-orders", "/dispensing", "/dispensing_orders",
        "/pack", "/pack_order", "/pack_orders", "/pack-orders",
        "/packing_orders",
        "/package_orders", "/subpackage_orders", "/subpackages",
        "/orders", "/distribute", "/distribution", "/dispatch",
        "/fenzhuang", "/fenzhuangdan",
        "/api/dispense", "/api/dispense_orders", "/api/pack_orders", "/api/orders",
        "/tickets/dispense",
    )
    app.add_url_rule(
        submit_rules[0], endpoint="dispense_submit",
        view_func=submit_dispense, methods=["POST"],
    )
    for rule in submit_rules[1:]:
        app.add_url_rule(
            rule, endpoint="dispense_submit",
            view_func=submit_dispense, methods=["POST"],
        )

    @app.route("/dispense/<path:order_no>", methods=["POST"])
    def dispense_submit_by_no(order_no: str):
        return submit_dispense(order_no_from_path=order_no)

    # “对某张单号资源提交”的嵌套写法
    for rule in (
        "/orders/<path:order_no>/submit",
        "/pack_orders/<path:order_no>/submit",
        "/dispense_orders/<path:order_no>/submit",
    ):
        app.add_url_rule(
            rule, endpoint="dispense_submit_by_no",
            view_func=dispense_submit_by_no, methods=["POST"],
        )

    # 嵌套资源风格：/buckets/<id>/dispense（旧桶 id 提交会被世代校验拦下）
    @app.route("/buckets/<int:bucket_ref>/dispense", methods=["POST"])
    def bucket_scoped_dispense(bucket_ref: int):
        # 路径 id 作为最强 bucket_id 标记；若表单另有标记一并保留
        return submit_dispense_with_path_bucket(bucket_ref)

    def submit_dispense_with_path_bucket(bucket_ref: int):
        # 包一层：把路径 bucket id 塞进 markers
        conn = get_conn()
        try:
            try:
                order_no, cans, markers = parse_dispense_request()
            except ValueError as exc:
                flash(f"分装单退回：{exc}", "error")
                return redirect(url_for("index"))
            markers["bucket_id"] = str(bucket_ref)
            # 路径风格下 bucket_id 必须严格等于当前世代/id：直接预检
            bucket = db.get_bucket(conn)
            if bucket is None:
                flash("分装单退回：当前没有桶档案，请先建新桶", "error")
                return redirect(url_for("index"))
            if bucket_ref not in (bucket["id"], bucket["generation"]):
                exc = db.StaleBucketPage(
                    f"分装单退回：/buckets/{bucket_ref} 是已更换的旧桶页面"
                    f"（当前桶为第 {bucket['generation']} 代），从旧桶页面提交"
                    "尚未入账的单一律退回，请在新桶总览重新开单。"
                )
                return reject_dispense(exc)
            try:
                row, outcome = db.register_dispense_order(
                    conn, order_no, cans, markers=markers
                )
            except db.DispenseRejected as exc:
                return reject_dispense(exc)
            if outcome == "replayed":
                flash(f"分装单 {order_no} 原单照样重交：只返回原单，不重复扣账。", "ok")
                return render_order_detail(conn, row, replayed=True)
            remaining = db.current_remaining_grams(conn)
            if wants_json():
                return jsonify({"ok": True, "outcome": "created",
                                "remaining_grams": remaining,
                                "order": db.dispense_order_to_dict(row)}), 201
            flash(f"分装单 {order_no} 已入账，余墨 {remaining}g。", "ok")
            return redirect(url_for("index") + "#remaining")
        finally:
            conn.close()

    # ---------- 分装单查询（换桶后仍可凭单号查） ----------
    def lookup_order_no() -> str:
        for key in ("order_no", "dispense_no", "pack_no", "no", "q", "keyword", "ticket_no"):
            value = (request.args.get(key) or "").strip()
            if value:
                return value
        return ""

    def dispense_detail_view(order_no: str):
        conn = get_conn()
        try:
            row = db.find_dispense_order(conn, order_no)
            if row is None:
                message = f"查不到分装单号 {order_no}：请核对单号（换桶不影响历史分装单查询）"
                if wants_json():
                    return jsonify({"ok": False, "success": False,
                                    "code": "not_found", "error": message}), 404
                flash(message, "error")
                return redirect(url_for("index"))
            return render_order_detail(conn, row)
        finally:
            conn.close()

    detail_rules = (
        "/dispense/<path:order_no>",
        "/dispense_order/<path:order_no>",
        "/dispense_orders/<path:order_no>",
        "/dispense-orders/<path:order_no>",
        "/dispensing_orders/<path:order_no>",
        "/pack/<path:order_no>",
        "/pack_order/<path:order_no>",
        "/pack_orders/<path:order_no>",
        "/pack-orders/<path:order_no>",
        "/packing_orders/<path:order_no>",
        "/orders/<path:order_no>",
        "/order/<path:order_no>",
        "/api/dispense/<path:order_no>",
        "/api/dispense_orders/<path:order_no>",
        "/api/orders/<path:order_no>",
    )
    app.add_url_rule(
        detail_rules[0], endpoint="dispense_detail",
        view_func=dispense_detail_view, methods=["GET"],
    )
    for rule in detail_rules[1:]:
        app.add_url_rule(
            rule, endpoint="dispense_detail",
            view_func=dispense_detail_view, methods=["GET"],
        )

    @app.route("/dispense/lookup", methods=["GET"])
    @app.route("/dispense/search", methods=["GET"])
    @app.route("/lookup", methods=["GET"])
    @app.route("/query", methods=["GET"])
    def dispense_lookup():
        order_no = lookup_order_no()
        if not order_no:
            if wants_json():
                return jsonify({"ok": False, "error": "请提供分装单号 order_no"}), 400
            flash("请输入要查询的分装单号", "error")
            return redirect(url_for("index") + "#lookup")
        return dispense_detail_view(order_no)

    # 集合路径本身也支持 GET ?order_no=xxx 查询（/dispense、/orders 等）
    for rule in (
        "/dispense", "/dispense_order", "/dispense_orders",
        "/pack_order", "/pack_orders", "/order", "/orders",
        "/api/dispense", "/api/orders",
    ):
        app.add_url_rule(
            rule, endpoint="dispense_lookup",
            view_func=dispense_lookup, methods=["GET"],
        )

    return app
