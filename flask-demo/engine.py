# -*- coding: utf-8 -*-
"""
e日金 规则引擎（纯函数，零依赖）

公式唯一依据：
  《工行杯 e日金 商业计划书 v5.0》附录 C 公式总表（公式冻结基准）
  《工行杯 e日金 5.0 档位算法定版 v1.0》（唯一数学权威，2026-09-12 定稿拍板）

Demo 参数口径：S=20 / d0=30（MVP 固定）/ W0=100 / N=29（周期=29 普通日+1 放纵日）
"""

D0, S, W0, N = 30, 20, 100, 29
CYCLE_DAYS = N + 1              # 30：29 普通日 + 1 放纵日
GATE = 500                      # 回滚池开闸水位线（资金流转阈值）
BUFFER = 10                     # 超额缓冲释放额
FULL_STATE = 500                # 水库状态解锁阈值（K_fixed≥500 → 完整约束状态）
TIERS = {"L1": 50, "L2": 100, "L3": 150, "L4": 200, "L5": 300}
TIER_ORDER = ["L1", "L2", "L3", "L4", "L5"]


def k_max(M):
    """附录C#1：最大安全锁定额——锁定后必须付得起普通日保障线＋放纵日基础"""
    return M - N * S - W0


def rigid_e(M, K):
    """附录C#5：刚需池＝专项余数；E<0 则该档位组合不成立（母版 3.6）"""
    return M - K - N * D0 - W0


def daily_ledger():
    """附录C#6：日常预算账本（普通日＋放纵日，排除刚需池 E）"""
    return N * D0 + W0


def constraint_state(K_fixed):
    """母版 3.3：500 状态门槛——水库的水（K_fixed）≥500 → 完整约束状态"""
    return "FULL" if K_fixed >= FULL_STATE else "BASIC"


def tier_options(M, K_fixed):
    """档位判定树（定版 §五）：
    可用档位 = {金额 ≤ K_max} ∩ {E ≥ 0} ∩ {500 状态允许的档位集}
    不截断规则：超 K_max 判不可用并提示，不自动改金额（定版 §三）"""
    state = constraint_state(K_fixed)
    out = []
    for t in TIER_ORDER:
        K = TIERS[t]
        if K > k_max(M):
            ok, reason = False, "超过 K_max ¥%d（保障线校验未过，母版 3.3）" % k_max(M)
        elif rigid_e(M, K) < 0:
            ok, reason = False, "账目组合不成立（刚需池 E<0，母版 3.6）"
        elif state == "BASIC" and t not in ("L1", "L2"):
            ok, reason = False, "基础约束状态仅开放 L1-L2（水库 K_fixed≥500 解锁全档，母版 3.3）"
        else:
            ok, reason = True, ""
        out.append({"tier": t, "K": K, "ok": ok, "reason": reason})
    return out


def issue_amount(b_daily_remain, d_remain):
    """附录C#8：d_i = min(d0, B_daily_remain − S×(D_remain−1))
    公式只管正常状态（B_daily_remain ≥ S×D_remain，故 d_i ≥ S 恒成立）；
    不满足 → protection=True，不进公式——母版 3.6：预算保护状态，不压低日发"""
    if b_daily_remain >= S * d_remain:
        return {"amount": min(D0, b_daily_remain - S * (d_remain - 1)), "protection": False}
    return {"amount": min(S, b_daily_remain), "protection": True}


def settle_rollback(pool, r):
    """附录C#10 / 定版 §六：X = R_t + r_i
    X ≤ 500：全部入回滚池
    X > 500：500 开闸入锁定池，Δ = X−500；Δ<10 全额归用户；Δ≥10 释放 10 元、Δ−10 留回滚池作下阶段起点"""
    if r <= 0:
        return {"pool": pool, "to_lock": 0, "to_user": 0, "opened": False}
    x = pool + r
    if x <= GATE:
        return {"pool": x, "to_lock": 0, "to_user": 0, "opened": False}
    delta = x - GATE
    if delta < BUFFER:
        return {"pool": 0, "to_lock": GATE, "to_user": delta, "opened": True}
    return {"pool": delta - BUFFER, "to_lock": GATE, "to_user": BUFFER, "opened": True}


def indulgence_w(r_prev, w_draw=0):
    """附录C#13：W = min(100 + 0.5×R₋₁ + W_draw, 300)，额度取整到元
    R₋₁ 是行为统计量不是资金余额——只提高支付权限，不从回滚池扣款（母版 3.8）"""
    return min(100 + r_prev // 2 + w_draw, 300)
