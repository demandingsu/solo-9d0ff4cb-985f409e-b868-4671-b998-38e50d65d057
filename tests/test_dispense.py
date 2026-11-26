"""分装单（分墨）端到端自测与并发核对。

覆盖任务要求：
  * 总览开分装单：单号 + 各罐正整数净重
  * 首单入账一并结束调配：配比达标 / 未超容量 / 够装 三项确认
  * 此后停收称量单与桶档修改，总览显示余墨
  * 并发交单按最新余墨整单扣账；登记失败余墨与调配状态照旧
  * 换桶后凭单号查原桶批号、配比、各罐净重
  * 原单照样重交只返回原单；同号改内容退回
  * 旧桶页面提交尚未入账的单退回
"""

import os
import re
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from ink_app import create_app, db, domain


class DispenseTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.app = create_app(self.db_path)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def setup_bucket(self, capacity=1000, ratios=None, batch_no="B1"):
        ratios = ratios or {"青": 3, "品红": 2}
        return self.client.post(
            "/bucket/setup",
            data={
                "capacity": str(capacity),
                "batch_no": batch_no,
                "color_name": list(ratios.keys()),
                "color_ratio": [str(v) for v in ratios.values()],
            },
            follow_redirects=True,
        )

    def post_ticket(self, no, grams):
        return self.client.post(
            "/tickets",
            data={"ticket_no": no,
                  **{f"grams_{k}": str(v) for k, v in grams.items()}},
            follow_redirects=True,
        )

    def load_compliant_500(self):
        """300/200 = 3:2，合计 500g，容量 1000。"""
        self.setup_bucket()
        self.post_ticket("T1", {"青": 300, "品红": 200})

    def dispense(self, no, cans, **extra):
        data = {"order_no": no, "can_weight": [str(c) for c in cans], **extra}
        return self.client.post("/dispense", data=data, follow_redirects=True)

    def text(self, resp):
        return resp.get_data(as_text=True)

    def assertInBody(self, resp, needle):
        self.assertIn(needle, self.text(resp))


class TestFirstOrderGateAndClose(DispenseTestBase):
    def test_first_order_happy_closes_mixing_and_shows_remaining(self):
        self.load_compliant_500()
        resp = self.dispense("FZ1", [250, 200])  # 450
        self.assertEqual(resp.status_code, 200)
        self.assertInBody(resp, "已整单入账")
        self.assertInBody(resp, "余墨 50g")
        with self.app.get_conn() as conn:
            bucket = db.get_bucket(conn)
            self.assertEqual(bucket["phase"], db.PHASE_DISPENSING)
            self.assertIsNotNone(bucket["closed_at"])
            self.assertEqual(db.current_remaining_grams(conn), 50)
            order = db.find_dispense_order(conn, "FZ1")
            self.assertEqual(domain.load_list(order["can_weights_json"]), [250, 200])
            self.assertEqual(order["batch_no"], "B1")
            self.assertEqual(order["ratios_json"],
                             domain.dump_weights({"青": 3, "品红": 2}))

    def test_first_order_ratio_not_compliant_rejected_keeps_mixing(self):
        self.setup_bucket()
        self.post_ticket("T1", {"青": 300, "品红": 100})  # 配比不对，400g
        resp = self.dispense("FZ-BAD", [100])
        self.assertInBody(resp, "配比尚未达标")
        with self.app.get_conn() as conn:
            self.assertEqual(db.get_bucket(conn)["phase"], db.PHASE_MIXING)
            self.assertIsNone(db.find_dispense_order(conn, "FZ-BAD"))
            self.assertEqual(db.current_remaining_grams(conn), 400)

    def test_first_order_over_capacity_rejected(self):
        self.setup_bucket(capacity=400)
        self.post_ticket("T1", {"青": 300, "品红": 200})  # 500 > 400
        resp = self.dispense("FZ-CAP", [10])
        self.assertInBody(resp, "超桶容量")
        with self.app.get_conn() as conn:
            self.assertEqual(db.get_bucket(conn)["phase"], db.PHASE_MIXING)
            self.assertIsNone(db.find_dispense_order(conn, "FZ-CAP"))

    def test_first_order_not_enough_to_fill_rejected(self):
        self.load_compliant_500()
        resp = self.dispense("FZ-BIG", [300, 201])  # 501 > 500
        self.assertInBody(resp, "不够装")
        with self.app.get_conn() as conn:
            self.assertEqual(db.get_bucket(conn)["phase"], db.PHASE_MIXING)
            self.assertIsNone(db.find_dispense_order(conn, "FZ-BIG"))
            self.assertEqual(db.current_remaining_grams(conn), 500)

    def test_invalid_can_weights(self):
        self.load_compliant_500()
        for bad in ([0], [-5], [10, 0]):
            resp = self.dispense("BAD-" + "_".join(map(str, bad)), bad)
            self.assertInBody(resp, "正整数")
        # 非数字
        resp = self.client.post(
            "/dispense",
            data={"order_no": "BAD-X", "can_weight": ["abc"]},
            follow_redirects=True,
        )
        self.assertInBody(resp, "正整数")
        with self.app.get_conn() as conn:
            self.assertEqual(len(db.list_dispense_orders(conn)), 0)


class TestLocksAfterFirstOrder(DispenseTestBase):
    def test_tickets_rejected_after_close(self):
        self.load_compliant_500()
        self.dispense("FZ1", [500])
        resp = self.post_ticket("T2", {"青": 1, "品红": 0})
        self.assertInBody(resp, "停收称量单")
        with self.app.get_conn() as conn:
            self.assertIsNone(db.find_ticket(conn, "T2"))

    def test_bucket_edit_rejected_after_close(self):
        self.load_compliant_500()
        self.dispense("FZ1", [100])
        resp = self.client.post(
            "/bucket/setup",
            data={"capacity": "999",
                  "color_name": ["青", "品红"],
                  "color_ratio": ["3", "2"]},
            follow_redirects=True,
        )
        self.assertInBody(resp, "桶档修改已退回")
        with self.app.get_conn() as conn:
            self.assertEqual(db.get_bucket(conn)["capacity"], 1000)

    def test_remaining_display_and_deduction(self):
        self.load_compliant_500()
        self.dispense("FZ1", [120, 80])  # 200
        resp = self.client.get("/")
        self.assertInBody(resp, "余墨 <strong>300</strong> g")
        # 整单 250 扣账 -> 50
        self.dispense("FZ2", [250])
        with self.app.get_conn() as conn:
            self.assertEqual(db.current_remaining_grams(conn), 50)
        # 60 > 50 整单退回，余墨照旧
        resp = self.dispense("FZ3", [60])
        self.assertInBody(resp, "最新余墨 50g")
        with self.app.get_conn() as conn:
            self.assertEqual(db.current_remaining_grams(conn), 50)
            self.assertIsNone(db.find_dispense_order(conn, "FZ3"))
        # 50 整单成功 -> 0
        self.dispense("FZ4", [20, 30])
        with self.app.get_conn() as conn:
            self.assertEqual(db.current_remaining_grams(conn), 0)


class TestIdempotentReplayAndConflict(DispenseTestBase):
    def test_identical_replay_returns_original_no_double_charge(self):
        self.load_compliant_500()
        self.dispense("D1", [100, 50])  # 150
        # 不 follow：原单重交直接 200 返回原单页
        resp = self.client.post(
            "/dispense", data={"order_no": "D1", "can_weight": ["100", "50"]}
        )
        self.assertEqual(resp.status_code, 200)
        html = self.text(resp)
        self.assertIn("原单", html)
        self.assertIn("D1", html)
        with self.app.get_conn() as conn:
            self.assertEqual(len(db.list_dispense_orders(conn)), 1)
            self.assertEqual(db.current_remaining_grams(conn), 350)

    def test_changed_content_same_no_rejected(self):
        self.load_compliant_500()
        self.dispense("D2", [100, 50])
        resp = self.dispense("D2", [100, 51])
        self.assertInBody(resp, "退回")
        self.assertInBody(resp, "单号 D2 已登记")
        with self.app.get_conn() as conn:
            rows = db.list_dispense_orders(conn)
            self.assertEqual(len(rows), 1)
            self.assertEqual(domain.load_list(rows[0]["can_weights_json"]), [100, 50])
            self.assertEqual(db.current_remaining_grams(conn), 350)

    def test_json_replay_status_200_and_conflict_409(self):
        self.load_compliant_500()
        r = self.client.post("/api/dispense",
                             json={"order_no": "J1", "can_weights": [200]})
        self.assertEqual(r.status_code, 201)
        r = self.client.post("/api/dispense",
                             json={"order_no": "J1", "can_weights": [200]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["replayed"])
        r = self.client.post("/api/dispense",
                             json={"order_no": "J1", "can_weights": [201]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "order_no_conflict")


class TestBucketChangeAndLookup(DispenseTestBase):
    def _change_bucket(self, new_batch="B2"):
        self.client.post("/bucket/reset")
        self.setup_bucket(capacity=800, ratios={"黑": 1, "红": 1},
                          batch_no=new_batch)

    def test_lookup_old_order_after_change(self):
        self.load_compliant_500()
        self.dispense("Q1", [200, 100])
        self._change_bucket()
        for path in ("/dispense/Q1", "/orders/Q1", "/pack_orders/Q1",
                     "/dispense_orders/Q1", "/dispense/lookup?order_no=Q1",
                     "/lookup?order_no=Q1"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            html = self.text(resp)
            self.assertIn("B1", html, path)
            self.assertIn("Q1", html, path)
            self.assertIn("200", html, path)
            self.assertIn("100", html, path)
            self.assertIn("青", html, path)

    def test_json_lookup_snapshots(self):
        self.load_compliant_500()
        self.dispense("Q2", [123, 77])
        self._change_bucket()
        r = self.client.get("/api/dispense/Q2")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["batch_no"], "B1")
        self.assertEqual(data["ratios"], {"青": 3, "品红": 2})
        self.assertEqual(data["can_weights"], [123, 77])

    def test_stale_old_bucket_page_submission_rejected(self):
        self.load_compliant_500()
        # 从当前总览抓旧 epoch 令牌，模拟“旧页面”
        old_html = self.text(self.client.get("/"))
        old_epoch = re.search(r'name="bucket_epoch" value="([0-9a-f]+)"',
                              old_html).group(1)
        self._change_bucket()

        # 1) 旧 epoch 令牌
        resp = self.dispense("STALE1", [50], bucket_epoch=old_epoch)
        self.assertInBody(resp, "旧桶")
        # 2) 旧批号
        resp = self.dispense("STALE2", [50], batch_no="B1")
        self.assertInBody(resp, "旧桶")
        # 3) 旧世代
        resp = self.dispense("STALE3", [50], bucket_generation="1")
        self.assertInBody(resp, "旧桶")
        with self.app.get_conn() as conn:
            for no in ("STALE1", "STALE2", "STALE3"):
                self.assertIsNone(db.find_dispense_order(conn, no), no)

    def test_stale_marker_checked_after_duplicate_lookup(self):
        """已入账原单即使带旧桶标记，也按原单重交返回，而不是旧桶错误。"""
        self.load_compliant_500()
        self.dispense("K1", [100])
        self._change_bucket()
        resp = self.client.post(
            "/dispense",
            data={"order_no": "K1", "can_weight": ["100"],
                  "batch_no": "B1"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("原单", self.text(resp))

    def test_batch_change_without_reset_rotates_token(self):
        self.load_compliant_500()
        old_html = self.text(self.client.get("/"))
        old_epoch = re.search(r'name="bucket_epoch" value="([0-9a-f]+)"',
                              old_html).group(1)
        # 不 reset，直接用新批号重新保存桶档案（视同换桶）
        resp = self.setup_bucket(batch_no="B-NEW")
        self.assertInBody(resp, "桶档案已更新")
        resp = self.dispense("STALE4", [50], bucket_epoch=old_epoch)
        self.assertInBody(resp, "旧桶")
        with self.app.get_conn() as conn:
            self.assertIsNone(db.find_dispense_order(conn, "STALE4"))

    def test_new_bucket_accepts_new_orders_without_markers(self):
        self.load_compliant_500()
        self.dispense("OLD1", [500])
        self._change_bucket("B2")
        self.post_ticket("N1", {"黑": 100, "红": 100})
        resp = self.dispense("NEW1", [120, 80])
        self.assertInBody(resp, "已整单入账")
        with self.app.get_conn() as conn:
            self.assertEqual(db.current_remaining_grams(conn), 0)
            self.assertEqual(db.get_bucket(conn)["batch_no"], "B2")
            # 旧单档案仍在
            self.assertIsNotNone(db.find_dispense_order(conn, "OLD1"))

    def test_lookup_unknown_order_404_json_or_flash_html(self):
        self.setup_bucket()
        r = self.client.get("/api/dispense/NOPE")
        self.assertEqual(r.status_code, 404)
        resp = self.client.get("/dispense/NOPE", follow_redirects=True)
        self.assertInBody(resp, "查不到分装单号")


class TestFieldAndRouteAliases(DispenseTestBase):
    def test_repeated_can_weights_field(self):
        self.load_compliant_500()
        r = self.client.post(
            "/dispense_orders",
            data={"order_no": "A1", "can_weights": ["100", "200"]},
            follow_redirects=True,
        )
        self.assertEqual(r.status_code, 200)
        with self.app.get_conn() as conn:
            o = db.find_dispense_order(conn, "A1")
            self.assertIsNotNone(o)
            self.assertEqual(domain.load_list(o["can_weights_json"]), [100, 200])

    def test_indexed_can_fields(self):
        self.load_compliant_500()
        r = self.client.post(
            "/pack_orders",
            data={"order_no": "A2", "can_1": "11", "can_2": "22", "can_3": "33"},
            follow_redirects=True,
        )
        self.assertEqual(r.status_code, 200)
        with self.app.get_conn() as conn:
            o = db.find_dispense_order(conn, "A2")
            self.assertEqual(domain.load_list(o["can_weights_json"]), [11, 22, 33])

    def test_json_cans_as_object_list(self):
        self.load_compliant_500()
        r = self.client.post("/api/orders", json={
            "order_no": "A3",
            "cans": [{"net_weight": 40}, {"net_weight": 60}],
        })
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        with self.app.get_conn() as conn:
            o = db.find_dispense_order(conn, "A3")
            self.assertEqual(domain.load_list(o["can_weights_json"]), [40, 60])

    def test_get_collection_lookup(self):
        self.load_compliant_500()
        self.dispense("ALIAS1", [10])
        r = self.client.get("/dispense?order_no=ALIAS1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("ALIAS1", self.text(r))


class TestThreadedConcurrency(DispenseTestBase):
    def test_parallel_oversubscription_through_http_stack(self):
        self.load_compliant_500()
        n = 12
        barrier = threading.Barrier(n)
        outcomes = []
        lock = threading.Lock()

        def worker(i):
            client = self.app.test_client()
            barrier.wait()
            resp = client.post(
                "/api/dispense",
                json={"order_no": f"CC-{i}", "can_weights": [60]},
            )
            with lock:
                outcomes.append(resp.status_code)

        with ThreadPoolExecutor(max_workers=n) as pool:
            list(pool.map(worker, range(n)))

        self.assertEqual(outcomes.count(201), 8, outcomes)
        self.assertEqual(outcomes.count(409), 4, outcomes)
        with self.app.get_conn() as conn:
            self.assertEqual(db.current_remaining_grams(conn), 20)
            self.assertEqual(len(db.list_dispense_orders(conn)), 8)
            self.assertEqual(db.get_bucket(conn)["phase"], "dispensing")

    def test_parallel_identical_orders_single_row(self):
        self.load_compliant_500()
        n = 8
        barrier = threading.Barrier(n)
        codes = []
        lock = threading.Lock()

        def worker(_):
            client = self.app.test_client()
            barrier.wait()
            resp = client.post(
                "/api/dispense",
                json={"order_no": "DUP-C", "can_weights": [70]},
            )
            with lock:
                codes.append(resp.status_code)

        with ThreadPoolExecutor(max_workers=n) as pool:
            list(pool.map(worker, range(n)))

        self.assertEqual(codes.count(201), 1, codes)
        self.assertEqual(codes.count(200), 7, codes)
        with self.app.get_conn() as conn:
            self.assertEqual(len(db.list_dispense_orders(conn)), 1)
            self.assertEqual(db.current_remaining_grams(conn), 430)


if __name__ == "__main__":
    unittest.main()
