"""领域计算自测：加料最少化、合格判定、停配提示。"""

import unittest

from ink_app import domain


class TestComputeAdditions(unittest.TestCase):
    def test_simple_ratio_exact(self):
        # 目标 3:2，已有 300/200，已达标 -> 全 0
        adds = domain.compute_additions({"a": 300, "b": 200}, {"a": 3, "b": 2})
        self.assertEqual(adds, {"a": 0, "b": 0})

    def test_only_add_one_color(self):
        # 目标 3:2，已有 300/100 -> q=100，补 b 100g
        adds = domain.compute_additions({"a": 300, "b": 100}, {"a": 3, "b": 2})
        self.assertEqual(adds, {"a": 0, "b": 100})
        self.assertEqual(sum(adds.values()), 100)

    def test_fractional_q_gcd_step(self):
        # 目标 2:4（g=2，等价 1:2），已有 a=1 -> q=0.5 -> a=1, b=2，补 b 2g
        adds = domain.compute_additions({"a": 1, "b": 0}, {"a": 2, "b": 4})
        self.assertEqual(adds, {"a": 0, "b": 2})

    def test_minimal_total_no_other_solution_smaller(self):
        # 目标 3:2，已有 250/190：q>=max(250/3≈83.33, 190/2=95)=95
        adds = domain.compute_additions({"a": 250, "b": 190}, {"a": 3, "b": 2})
        self.assertEqual(adds, {"a": 35, "b": 0})
        # 验证任何更小的总加料都不可能同时满足只加料与整数克
        weights = {"a": 250, "b": 190}
        ratios = {"a": 3, "b": 2}
        best = sum(adds.values())
        for extra_a in range(best):
            # 穷举：总加料更少时不存在非负整数 (da,db) 使 (250+da)/3 == (190+db)/2
            for extra_b in range(best - extra_a):
                fa = (weights["a"] + extra_a) / ratios["a"]
                fb = (weights["b"] + extra_b) / ratios["b"]
                self.assertNotEqual(fa, fb)

    def test_three_colors_and_zeros(self):
        # 目标 1:1:1，只有 c 有 5g -> 各补到 5
        adds = domain.compute_additions(
            {"x": 0, "y": 0, "z": 5}, {"x": 1, "y": 1, "z": 1}
        )
        self.assertEqual(adds, {"x": 5, "y": 5, "z": 0})


class TestCompliance(unittest.TestCase):
    def test_empty_bucket_not_compliant(self):
        ok, reason = domain.is_compliant({"a": 0, "b": 0}, {"a": 3, "b": 2}, 1000)
        self.assertFalse(ok)
        self.assertIn("无料", reason)

    def test_ratio_mismatch(self):
        ok, reason = domain.is_compliant({"a": 301, "b": 200}, {"a": 3, "b": 2}, 1000)
        self.assertFalse(ok)
        self.assertIn("配比", reason)

    def test_missing_color(self):
        ok, _ = domain.is_compliant({"a": 300, "b": 0}, {"a": 3, "b": 2}, 1000)
        self.assertFalse(ok)

    def test_over_capacity(self):
        ok, reason = domain.is_compliant({"a": 300, "b": 200}, {"a": 3, "b": 2}, 400)
        self.assertFalse(ok)
        self.assertIn("超容量", reason)

    def test_compliant(self):
        ok, reason = domain.is_compliant({"a": 300, "b": 200}, {"a": 3, "b": 2}, 500)
        self.assertTrue(ok)


class TestSuggestion(unittest.TestCase):
    def test_stop_when_addition_exceeds_capacity(self):
        # 容量 450；现有 300/100，达标需 300/200=500 > 450 -> 停配
        s = domain.build_suggestion(
            {"a": 300, "b": 100}, {"a": 3, "b": 2}, 450
        )
        self.assertTrue(s["stop"])
        self.assertEqual(s["additions"], {"a": 0, "b": 100})
        self.assertEqual(s["target_total"], 500)
        self.assertIn("停止补配", s["message"])

    def test_no_stop_within_capacity(self):
        s = domain.build_suggestion(
            {"a": 300, "b": 100}, {"a": 3, "b": 2}, 600
        )
        self.assertFalse(s["stop"])
        self.assertFalse(s["already_compliant"])

    def test_already_compliant_message(self):
        s = domain.build_suggestion(
            {"a": 300, "b": 200}, {"a": 3, "b": 2}, 600
        )
        self.assertTrue(s["already_compliant"])
        self.assertEqual(s["add_total"], 0)


if __name__ == "__main__":
    unittest.main()
