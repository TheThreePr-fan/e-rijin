# -*- coding: utf-8 -*-
"""验收钉子：母版示例自检（python test_engine.py，零依赖）
每个案例来自：附录 C 公式总表 / 定版 §七 示例 / 母版 3.6-3.8 正文示例 / 附录 D"""
from engine import (k_max, rigid_e, daily_ledger, constraint_state, tier_options,
                    issue_amount, settle_rollback, indulgence_w, N, D0, W0)

fails = []
total = [0]


def check(name, got, want):
    total[0] += 1
    ok = got == want
    print(("  OK  " if ok else "FAIL  ") + name + ("" if ok else "   got=%r want=%r" % (got, want)))
    if not ok:
        fails.append(name)


print("== 周期层（附录C#1-#7）==")
check("K_max M=1500 → 820", k_max(1500), 820)
check("K_max M=800 → 120（定版例2）", k_max(800), 120)
check("K_max M=650 → -30（定版例3）", k_max(650), -30)
check("刚需池 E(1500,L5) → 230", rigid_e(1500, 300), 230)
check("账目闭合 300+870+100+230 = 1500", 300 + N * D0 + W0 + rigid_e(1500, 300), 1500)
check("日常账本 B_daily = 970", daily_ledger(), 970)
check("500 状态：499=BASIC", constraint_state(499), "BASIC")
check("500 状态：500=FULL", constraint_state(500), "FULL")

print("== 档位判定（定版 §五）==")
t = tier_options(1500, 500)
check("完整约束 + M=1500 → 全档可用", [x["ok"] for x in t], [True] * 5)
t = tier_options(1500, 0)
check("基础约束 → 仅 L1-L2", [x["tier"] for x in t if x["ok"]], ["L1", "L2"])
t = tier_options(1200, 500)
check("M=1200 → L5 因 E<0 不可用", [x["ok"] for x in t], [True, True, True, True, False])
t = tier_options(650, 500)
check("M=650 → 无档可用（不假装可以锁）", [x["ok"] for x in t], [False] * 5)

print("== 日常层（附录C#8，母版 3.6）==")
check("首日发放（B=970,D=30）→ 30", issue_amount(970, 30), {"amount": 30, "protection": False})
check("边界验证（B=60,D=3）→ 20", issue_amount(60, 3), {"amount": 20, "protection": False})
check("最后一天剩 20 发 20，绝不算 0", issue_amount(20, 1), {"amount": 20, "protection": False})
check("保护状态（B=50,D=3）→ protection", issue_amount(50, 3), {"amount": 20, "protection": True})

print("== 回滚层（附录C#10，母版 3.7 示例）==")
check("池200+8 → 全入池不开闸", settle_rollback(200, 8),
      {"pool": 208, "to_lock": 0, "to_user": 0, "opened": False})
check("497+8 → 开闸500、¥5 归用户、池归零", settle_rollback(497, 8),
      {"pool": 0, "to_lock": 500, "to_user": 5, "opened": True})
check("497+16 → 开闸500、释放¥10、池剩¥3", settle_rollback(497, 16),
      {"pool": 3, "to_lock": 500, "to_user": 10, "opened": True})
check("498+7 → 开闸500、释放¥5（定版示例）", settle_rollback(498, 7),
      {"pool": 0, "to_lock": 500, "to_user": 5, "opened": True})
check("498+22 → 开闸500、释放¥10、池剩¥10（定版示例，按规则文本口径）", settle_rollback(498, 22),
      {"pool": 10, "to_lock": 500, "to_user": 10, "opened": True})
check("X=500 满池不开闸（定版：X≤500 全入池）", settle_rollback(400, 100),
      {"pool": 500, "to_lock": 0, "to_user": 0, "opened": False})

print("== 放纵日（附录C#13，母版 3.8 / 附录D）==")
check("W R₋₁=0 → 100", indulgence_w(0), 100)
check("W R₋₁=196 → 198（附录D 甲）", indulgence_w(196), 198)
check("W R₋₁=60 → 130（附录D 乙）", indulgence_w(60), 130)
check("W 封顶 300", indulgence_w(500), 300)

print()
if fails:
    print("FAILED %d/%d: %s" % (len(fails), total[0], fails))
else:
    print("ALL %d PASS ✔" % total[0])
