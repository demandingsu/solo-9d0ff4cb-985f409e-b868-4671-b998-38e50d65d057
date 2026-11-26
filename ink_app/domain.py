"""专色墨称量/核定的纯计算领域逻辑。

规则要点
--------
1. 每桶有容量 ``capacity``（克，正整数）和各色目标质量比 ``ratios``（正整数）。
2. 只允许加料（各色补的克数 >= 0），因此比例共同倍数 q 只能取满足
   ``q * r_i >= w_i`` 的最小值；为保证整数克，q 取 1/g 的整数倍，
   其中 g = gcd(所有目标比)。
3. 合格条件：桶里有料、实际配比恰好等于目标比、总量未超容量。
"""

from __future__ import annotations

import json
from fractions import Fraction
from math import gcd
from typing import Dict, Mapping, Optional, Tuple


def ratio_gcd(ratios: Mapping[str, int]) -> int:
    """所有目标质量比的最大公约数。"""
    g = 0
    for value in ratios.values():
        g = gcd(g, int(value))
    return g


def compute_additions(
    weights: Mapping[str, int], ratios: Mapping[str, int]
) -> Dict[str, int]:
    """计算"只加料"前提下达到目标配比时，每色最少要补的整数克数。

    设目标比例为 r_i，当前实称为 w_i。达标状态为 q*r_i，
    加料要求 q*r_i >= w_i 对所有色成立 => q >= max_i(w_i / r_i)。
    又因 q*r_i 必须是整数，令 g = gcd(r_i)，则 q 必须是 1/g 的整数倍。
    取满足条件的最小 q 即得到总加料最少的方案。
    """
    if not ratios:
        return {}
    g = ratio_gcd(ratios)
    # k = q*g，必为整数；q >= w_i/r_i 等价于 k*r_i >= w_i*g
    k = 0
    for color, r in ratios.items():
        w = int(weights.get(color, 0))
        # ceil(w * g / r)
        k = max(k, (w * g + r - 1) // r)
    return {
        color: (k * r // g) - int(weights.get(color, 0))
        for color, r in ratios.items()
    }


def total(weights: Mapping[str, int]) -> int:
    return sum(int(v) for v in weights.values())


def is_compliant(
    weights: Mapping[str, int],
    ratios: Mapping[str, int],
    capacity: int,
) -> Tuple[bool, str]:
    """按最新实称核定是否合格。

    返回 (是否合格, 原因说明)。合格要求三者同时满足：
    桶里有料、配比与目标比完全一致、总量不超过容量。
    """
    current_total = total(weights)
    if current_total == 0:
        return False, "桶里无料"
    if current_total > int(capacity):
        return False, f"实称总量 {current_total}g 已超容量 {capacity}g"
    # 用分数精确比较 w_i / r_i 是否全部相等（且没有缺色/零料色失配）
    base: Optional[Fraction] = None
    for color, r in ratios.items():
        w = int(weights.get(color, 0))
        if w <= 0:
            return False, f"{color} 未入料，配比不完整"
        frac = Fraction(w, int(r))
        if base is None:
            base = frac
        elif frac != base:
            return False, "实际配比与目标比不一致"
    return True, "有料、配比正确且未超容量"


def build_suggestion(
    weights: Mapping[str, int],
    ratios: Mapping[str, int],
    capacity: int,
) -> Dict[str, object]:
    """生成加料建议及"补完是否超容量"的停配提示。"""
    adds = compute_additions(weights, ratios)
    add_total = sum(adds.values())
    current_total = total(weights)
    target_total = current_total + add_total
    compliant, reason = is_compliant(weights, ratios, capacity)
    over_after = target_total > int(capacity)
    return {
        "additions": adds,
        "add_total": add_total,
        "current_total": current_total,
        "target_total": target_total,
        "capacity": int(capacity),
        "already_compliant": compliant,
        "compliance_reason": reason,
        "stop": over_after,
        "message": (
            f"补完总量将达 {target_total}g，超过桶容量 {capacity}g，应停止补配"
            if over_after
            else (
                "当前已达标，无需加料"
                if compliant
                else f"最少共补 {add_total}g 即可达到目标配比"
            )
        ),
    }


def dump_weights(weights: Mapping[str, int]) -> str:
    return json.dumps(dict(weights), ensure_ascii=False, sort_keys=True)


def load_weights(payload: Optional[str]) -> Dict[str, int]:
    if not payload:
        return {}
    return {str(k): int(v) for k, v in json.loads(payload).items()}
