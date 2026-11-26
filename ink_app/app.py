"""Flask 应用工厂与路由：专色墨称量登记、加料建议、核定。"""

from __future__ import annotations

import os
import sqlite3
from typing import Dict, List, Tuple

from flask import (
    Flask,
    flash,
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


def parse_bucket_form(form) -> Tuple[int, Dict[str, int]]:
    """解析建桶表单：颜色名列表 + 对应正整数目标比。"""
    capacity = _parse_positive_int(form.get("capacity"), "桶容量")
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
    return capacity, ratios


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


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
    app.config["DB_PATH"] = db_path or os.environ.get(
        "INK_DB_PATH", os.path.join(app.instance_path, "ink.db")
    )
    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)

    def get_conn() -> sqlite3.Connection:
        conn = db.connect(app.config["DB_PATH"])
        db.init_db(conn)
        return conn

    app.get_conn = get_conn  # type: ignore[attr-defined]

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
                g=domain.ratio_gcd(ratios),
            )
        finally:
            conn.close()

    # ---------- 建桶 / 改桶 ----------
    @app.route("/bucket/setup", methods=["POST"])
    def bucket_setup():
        conn = get_conn()
        try:
            try:
                capacity, ratios = parse_bucket_form(request.form)
            except ValueError as exc:
                flash(f"建桶失败：{exc}", "error")
                return redirect(url_for("index"))
            existing = db.get_bucket(conn)
            db.save_bucket(conn, capacity, ratios)
            if existing is None:
                flash(
                    f"已建档：容量 {capacity}g，目标比 "
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
    def bucket_reset():
        conn = get_conn()
        try:
            db.reset_all(conn)
            flash("已清空桶档案、全部称量单与核定记录", "ok")
            return redirect(url_for("index"))
        finally:
            conn.close()

    # ---------- 称量单登记 ----------
    @app.route("/tickets", methods=["POST"])
    def ticket_create():
        conn = get_conn()
        try:
            bucket = db.get_bucket(conn)
            if bucket is None:
                flash("请先建立桶档案再登记称量单", "error")
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

    return app
