"""Flask 端到端自测（临时 SQLite，不接触真实数据）。

覆盖交付要求中的三类必测：
  * 超称：入桶即超容量的称量单整单退回；达标补料会超容量时给停配提示
  * 重交：同号原单只记一次，同号改量也退回
  * 迟到入账：核定合格后补登单据自动撤标，补好重新核定
"""

import os
import tempfile
import unittest

from ink_app import create_app, domain
from ink_app import db


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.app = create_app(self.db_path)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def setup_bucket(self, capacity=1000, ratios=None):
        ratios = ratios or {"青": 3, "品红": 2}
        return self.client.post(
            "/bucket/setup",
            data={
                "capacity": str(capacity),
                "color_name": list(ratios.keys()),
                "color_ratio": [str(v) for v in ratios.values()],
            },
            follow_redirects=True,
        )

    def post_ticket(self, ticket_no, grams):
        return self.client.post(
            "/tickets",
            data={"ticket_no": ticket_no, **{f"grams_{k}": str(v) for k, v in grams.items()}},
            follow_redirects=True,
        )

    def body_text(self, resp):
        return resp.get_data(as_text=True)

    def assertFlash(self, resp, needle):
        self.assertIn(needle, self.body_text(resp))


class TestBucketAndFlow(AppTestBase):
    def test_setup_renders_bucket(self):
        resp = self.setup_bucket()
        self.assertEqual(resp.status_code, 200)
        self.assertFlash(resp, "已建档")
        self.assertIn("目标质量比", self.body_text(resp))

    def test_setup_validation(self):
        resp = self.client.post(
            "/bucket/setup",
            data={"capacity": "0", "color_name": ["青", "品红"], "color_ratio": ["3", "2"]},
            follow_redirects=True,
        )
        self.assertFlash(resp, "正整数")

        resp = self.client.post(
            "/bucket/setup",
            data={"capacity": "100", "color_name": ["青"], "color_ratio": ["3"]},
            follow_redirects=True,
        )
        self.assertFlash(resp, "至少需要两种颜色")

    def test_happy_path_suggestion_and_verify(self):
        self.setup_bucket(capacity=1000, ratios={"青": 3, "品红": 2})
        # 先入 青300、品红100 -> 建议补品红 100g
        resp = self.post_ticket("T001", {"青": 300, "品红": 100})
        self.assertFlash(resp, "已入账")
        html = self.body_text(resp)
        self.assertIn("最少共补 100g", html)

        # 按建议补一张单
        resp = self.post_ticket("T002", {"青": 0, "品红": 100})
        self.assertFlash(resp, "已入账")
        resp = self.client.post("/verify", follow_redirects=True)
        self.assertFlash(resp, "核定合格")
        self.assertIn("合格", self.body_text(resp))

    def test_ticket_requires_positive_entry(self):
        self.setup_bucket()
        resp = self.post_ticket("T0", {"青": 0, "品红": 0})
        self.assertFlash(resp, "至少要有一色入料")


class TestOverweight(AppTestBase):
    def test_entry_exceeding_capacity_is_rejected(self):
        self.setup_bucket(capacity=500)
        resp = self.post_ticket("T100", {"青": 300, "品红": 210})  # 合计 510 > 500
        self.assertEqual(resp.status_code, 200)
        self.assertFlash(resp, "超过容量")
        # 台账中不应出现该单
        with self.app.get_conn() as conn:
            self.assertEqual(db.list_tickets(conn), [])
            self.assertEqual(db.current_weights(conn), {"青": 0, "品红": 0})

    def test_cumulative_overweight_rejected(self):
        self.setup_bucket(capacity=500)
        self.post_ticket("T100", {"青": 300, "品红": 100})  # 400
        resp = self.post_ticket("T101", {"青": 0, "品红": 101})  # 501 > 500
        self.assertFlash(resp, "超过容量")
        with self.app.get_conn() as conn:
            weights = db.current_weights(conn)
        self.assertEqual(weights, {"青": 300, "品红": 100})

    def test_stop_notice_when_compliance_breaks_capacity(self):
        self.setup_bucket(capacity=450)
        resp = self.post_ticket("T200", {"青": 300, "品红": 100})
        html = self.body_text(resp)
        # 达标需 300/200=500 > 450
        self.assertIn("停配提示", html)
        self.assertIn("停止补配", html)

    def test_verify_fails_when_over_capacity(self):
        self.setup_bucket(capacity=400)
        self.post_ticket("T300", {"青": 300, "品红": 100})  # 400 正好合格
        # 再入一张仍在容量内但破坏配比的单（容量 400，已无空间——改用更大容量桶）
        with self.app.get_conn() as conn:
            db.save_bucket(conn, 500, {"青": 3, "品红": 2})
        self.post_ticket("T301", {"青": 50, "品红": 0})  # 350/100，总量 450 未超容量
        resp = self.client.post("/verify", follow_redirects=True)
        self.assertFlash(resp, "核定不合格")


class TestDuplicateTicket(AppTestBase):
    def test_same_number_same_amount_rejected(self):
        self.setup_bucket()
        self.post_ticket("D01", {"青": 100, "品红": 50})
        resp = self.post_ticket("D01", {"青": 100, "品红": 50})
        self.assertFlash(resp, "同号单据只记一次")
        with self.app.get_conn() as conn:
            self.assertEqual(len(db.list_tickets(conn)), 1)

    def test_same_number_changed_amount_rejected(self):
        self.setup_bucket()
        self.post_ticket("D02", {"青": 100, "品红": 50})
        # 同号改量重交 -> 退回，且页面展示原单内容
        resp = self.post_ticket("D02", {"青": 120, "品红": 50})
        self.assertFlash(resp, "已登记")
        self.assertFlash(resp, "同号单据只记一次")
        with self.app.get_conn() as conn:
            rows = db.list_tickets(conn)
            self.assertEqual(len(rows), 1)
            self.assertEqual(domain.load_weights(rows[0]["weights_json"]),
                             {"青": 100, "品红": 50})

    def test_blank_ticket_no_rejected(self):
        self.setup_bucket()
        resp = self.post_ticket(" ", {"青": 10, "品红": 5})
        self.assertFlash(resp, "称量单号不能为空")


class TestLateEntryAndRevocation(AppTestBase):
    def _certified_bucket(self):
        self.setup_bucket(capacity=1000, ratios={"青": 3, "品红": 2})
        self.post_ticket("L01", {"青": 300, "品红": 200})
        resp = self.client.post("/verify", follow_redirects=True)
        self.assertFlash(resp, "核定合格")

    def test_late_compliant_entry_keeps_certification(self):
        self._certified_bucket()
        # 核定后补登一张保持 3:2 的单（30/20），仍达标 -> 标志保留
        resp = self.post_ticket("L02", {"青": 30, "品红": 20})
        self.assertIn("复核仍达标", self.body_text(resp))
        with self.app.get_conn() as conn:
            state = db.certified_state(conn, 1000, {"青": 3, "品红": 2})
        self.assertTrue(state["certified"])
        self.assertFalse(state["revoked"])

    def test_late_bad_entry_auto_revokes(self):
        self._certified_bucket()
        # 核定后迟到一张破坏配比的单
        resp = self.post_ticket("L03", {"青": 30, "品红": 0})
        self.assertIn("已自动撤标", self.body_text(resp))
        with self.app.get_conn() as conn:
            state = db.certified_state(conn, 1000, {"青": 3, "品红": 2})
        self.assertFalse(state["certified"])
        self.assertTrue(state["revoked"])
        self.assertIn("自动撤销", state["reason"])

    def test_reverify_after_fixing(self):
        self._certified_bucket()
        self.post_ticket("L04", {"青": 30, "品红": 0})  # 撤标
        # 此时 330/200，建议补品红 20g（330/3=110 -> 品红 220）
        resp = self.post_ticket("L05", {"青": 0, "品红": 20})
        # 补好但必须人工重新核定，不会自动恢复合格
        with self.app.get_conn() as conn:
            state = db.certified_state(conn, 1000, {"青": 3, "品红": 2})
        self.assertFalse(state["certified"])
        resp = self.client.post("/verify", follow_redirects=True)
        self.assertFlash(resp, "核定合格")
        with self.app.get_conn() as conn:
            state = db.certified_state(conn, 1000, {"青": 3, "品红": 2})
        self.assertTrue(state["certified"])
        self.assertFalse(state["revoked"])

    def test_verify_failed_then_pass(self):
        self.setup_bucket()
        self.post_ticket("L10", {"青": 300, "品红": 100})
        resp = self.client.post("/verify", follow_redirects=True)
        self.assertFlash(resp, "核定不合格")
        self.post_ticket("L11", {"青": 0, "品红": 100})
        resp = self.client.post("/verify", follow_redirects=True)
        self.assertFlash(resp, "核定合格")

    def test_capacity_shrink_revokes_certification(self):
        self.setup_bucket(capacity=1000)
        self.post_ticket("L20", {"青": 300, "品红": 200})
        self.client.post("/verify", follow_redirects=True)
        # 容量改小到已装不下当前实称 -> 自动撤标，无需新单
        with self.app.get_conn() as conn:
            db.save_bucket(conn, 400, {"青": 3, "品红": 2})
            state = db.certified_state(conn, 400, {"青": 3, "品红": 2})
        self.assertFalse(state["certified"])
        self.assertTrue(state["revoked"])
        self.assertIn("桶参数变更", state["reason"])


class TestReset(AppTestBase):
    def test_reset_clears_everything(self):
        self.setup_bucket()
        self.post_ticket("R1", {"青": 10, "品红": 5})
        self.client.post("/verify", follow_redirects=True)
        resp = self.client.post("/bucket/reset", follow_redirects=True)
        self.assertFlash(resp, "已清空")
        self.assertIn("建立桶档案", self.body_text(resp))
        with self.app.get_conn() as conn:
            self.assertIsNone(db.get_bucket(conn))
            self.assertEqual(db.list_tickets(conn), [])
            self.assertEqual(db.list_verifications(conn), [])


if __name__ == "__main__":
    unittest.main()
