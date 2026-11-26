#!/usr/bin/env python3
"""真实 HTTP 并发分装 + 换桶后交单/查询试跑脚本（仅用标准库）。

用法：
    python scripts/http_trial.py [BASE_URL]

默认 BASE_URL=http://127.0.0.1:5000 。建议先用与 Docker Compose 相同的方式起服务：

    docker compose up -d --build
    # 或本地等效命令：
    INK_DB_PATH=/tmp/trial/ink.db \\
      gunicorn --bind 0.0.0.0:5000 --workers 2 ink_app.wsgi:app
    python scripts/http_trial.py
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000").rstrip("/")
# 每次试跑用唯一单号前缀，保证脚本可对同一服务重复执行
PFX = uuid.uuid4().hex[:6].upper()

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}  {detail}")


# 带 Cookie 的“浏览器”：让 flash 消息在 POST→302→GET 后仍能渲染
_browser = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(CookieJar())
)


def _request(url, data=None, headers=None, opener=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=None)
    op = opener or urllib.request.build_opener()
    try:
        resp = op.open(req, timeout=30)
        body = resp.read().decode("utf-8", "replace")
        return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body, dict(exc.headers)


def get_page(path):
    status, body, _ = _request(BASE + path, opener=_browser)
    return status, body


def post_form(path, fields):
    """fields: dict；值可以是 list/tuple 表示同名字段多行。"""
    pairs = []
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            for item in value:
                pairs.append((key, str(item)))
        else:
            pairs.append((key, str(value)))
    data = urllib.parse.urlencode(pairs).encode()
    status, body, _ = _request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        opener=_browser,
    )
    return status, body


def post_json(path, obj):
    data = json.dumps(obj).encode()
    status, body, headers = _request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = {}
    return status, parsed


def setup_bucket(batch, capacity, colors, ratios):
    fields = [("batch_no", batch), ("capacity", str(capacity))]
    for color in colors:
        fields.append(("color_name", color))
    for ratio in ratios:
        fields.append(("color_ratio", str(ratio)))
    # 直接拼 pairs（保留同名字段顺序）
    data = urllib.parse.urlencode(fields).encode()
    status, body, _ = _request(
        BASE + "/bucket/setup",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        opener=_browser,
    )
    assert status == 200, status
    return body


def post_ticket(no, grams):
    fields = {"ticket_no": no}
    for color, value in grams.items():
        fields[f"grams_{color}"] = value
    _, body = post_form("/tickets", fields)
    return body


def main():
    print(f"=== 目标服务 {BASE}（本次单号前缀 {PFX}）===")
    status, _ = get_page("/")
    check("GET / 健康检查 200", status == 200, str(status))

    # 自清理：换桶清空当前桶（历史分装档案保留，但本次单号带唯一前缀不冲突）
    post_form("/bucket/reset", {})

    print("\n-- 建桶 B-HTTP-1（1000g，青3∶品红2）并入料 300/200=500g --")
    setup_bucket("B-HTTP-1", 1000, ["青", "品红"], [3, 2])
    post_ticket("T1", {"青": 300, "品红": 200})
    _, page = get_page("/")
    check("总览出现批号 B-HTTP-1", "B-HTTP-1" in page)

    print("\n-- 并发超卖：12 人同时各交 60g（共请 720g，余墨 500g）--")
    barrier = threading.Barrier(12)

    def burst(i):
        barrier.wait()
        return post_json("/api/dispense",
                         {"order_no": f"{PFX}-C{i:02d}", "can_weights": [60]})

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(burst, range(12)))
    codes = [code for code, _ in results]
    created = codes.count(201)
    rejected = codes.count(409)
    dist = {code: codes.count(code) for code in set(codes)}
    print("   状态码分布:", dist)
    check("恰好 8 单成功（8×60=480≤500）", created == 8, f"created={created}")
    check("恰好 4 单因最新余墨不足整单退回", rejected == 4, f"rejected={rejected}")
    final_rem = min(
        (body.get("remaining_grams") for code, body in results if code == 201),
        default=None,
    )
    check("成功响应按提交时点回传余墨，最终余墨 20g", final_rem == 20,
          f"min remaining={final_rem}")
    _, page = get_page("/")
    check("总览显示余墨 20g", "余墨 <strong>20</strong> g" in page)
    check("总览显示调配已结束/停收称量单",
          "结束调配" in page and "停收称量单" in page)

    print("\n-- 并发同号原单：6 个相同 DUP（10g+10g=20g，恰够余墨）--")
    dup_no = f"{PFX}-DUP"
    barrier = threading.Barrier(6)

    def dup(_):
        barrier.wait()
        code, _ = post_json("/api/dispense",
                            {"order_no": dup_no, "can_weights": [10, 10]})
        return code

    with ThreadPoolExecutor(max_workers=6) as pool:
        dcodes = list(pool.map(dup, range(6)))
    print("   状态码:", dcodes)
    check("1 单 201 新建", dcodes.count(201) == 1, str(dcodes))
    check("5 单 200 原单重交(replayed)", dcodes.count(200) == 5, str(dcodes))
    code, data = post_json_get(f"/api/dispense/{dup_no}")
    check("DUP 各罐净重为 [10,10] 且批号快照 B-HTTP-1",
          code == 200 and data.get("can_weights") == [10, 10]
          and data.get("batch_no") == "B-HTTP-1")
    _, page = get_page("/")
    check("扣完后总览余墨 0g", "余墨 <strong>0</strong> g" in page)

    print("\n-- 锁：首单后称量单/桶档修改均退回 --")
    body = post_ticket("T2", {"青": 1, "品红": 0})
    check("称量单停收提示", "停收称量单" in body)
    body = setup_bucket("不应改成", 999, ["青", "品红"], [3, 2])
    check("桶档修改锁定提示", "桶档修改已退回" in body)

    print("\n-- 换桶：reset 后建 B-HTTP-2（1∶1，实称 200g）--")
    post_form("/bucket/reset", {})
    setup_bucket("B-HTTP-2", 500, ["黑", "红"], [1, 1])
    post_ticket("N1", {"黑": 100, "红": 100})

    print("\n-- 旧桶页面提交尚未入账的 STALE -> 退回且不落库 --")
    stale_no = f"{PFX}-STALE"
    _, body = post_form("/dispense", {
        "order_no": stale_no,
        "can_weight": ["50"],
        "batch_no": "B-HTTP-1",
        "bucket_generation": "1",
        "bucket_epoch": "old-page-token",
        "bucket_id": "1",
    })
    check("页面提示旧桶退回", "旧桶" in body and "退回" in body)
    code, _ = post_json_get(f"/api/dispense/{stale_no}")
    check("STALE 查不到（未登记）", code == 404, str(code))

    print("\n-- 换桶后凭单号查原桶 C00 / DUP --")
    first_conc = f"{PFX}-C00"
    status, body = get_page(f"/dispense/{first_conc}")
    check("HTML 原单 200 且含旧批号 B-HTTP-1",
          status == 200 and "B-HTTP-1" in body)
    check("原单显示配比 青3∶品红2", "青 3" in body and "品红 2" in body)
    check("原单显示该罐净重 60g", "60" in body)
    status, body = get_page(f"/orders/{dup_no}")
    check("别名路径 /orders/<单号> 可查",
          status == 200 and "B-HTTP-1" in body)

    print("\n-- 新桶交单 NEW [120,80]=200 成功；原单重交/改量同号 --")
    new_no = f"{PFX}-NEW"
    _, body = post_form("/dispense",
                        {"order_no": new_no, "can_weight": ["120", "80"]})
    check("新桶首单整单入账", "已整单入账" in body and "余墨 0g" in body)
    # 原样重交：应直接 200 返回原单页
    status, body = post_form_no_redirect(
        "/dispense", {"order_no": new_no, "can_weight": ["120", "80"]}
    )
    check("照样重交直接返回原单(200)",
          status == 200 and "原单" in body and "120" in body and "80" in body,
          f"status={status}")
    _, body = post_form("/dispense",
                        {"order_no": new_no, "can_weight": ["121", "80"]})
    check("改内容沿用单号退回",
          "改了内容" in body or "内容不同" in body)

    print(f"\n=== 结果：{_passed} 通过，{_failed} 失败 ===")
    return 1 if _failed else 0


def post_json_get(path):
    return _json_request("GET", path)


def _json_request(method, path, obj=None):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        body = resp.read().decode("utf-8", "replace")
        code = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        code = exc.code
    try:
        return code, json.loads(body)
    except json.JSONDecodeError:
        return code, {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


def post_form_no_redirect(path, fields):
    pairs = []
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            for item in value:
                pairs.append((key, str(item)))
        else:
            pairs.append((key, str(value)))
    data = urllib.parse.urlencode(pairs).encode()
    try:
        resp = _no_redirect_opener.open(urllib.request.Request(
            BASE + path, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}),
            timeout=30)
        return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 302 会以 HTTPError 形式抛出（NoRedirect 让 handler 返回 None）
        if exc.code in (301, 302, 303, 307, 308):
            location = exc.headers.get("Location", "/")
            if location.startswith("http"):
                target = location
            else:
                target = BASE + (location if location.startswith("/") else "/" + location)
            status, body, _ = _request(target, opener=_browser)
            return status, body
        return exc.code, exc.read().decode("utf-8", "replace")


if __name__ == "__main__":
    sys.exit(main())
