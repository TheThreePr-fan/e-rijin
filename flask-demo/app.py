# -*- coding: utf-8 -*-
"""
e日金 · Flask Demo（母版 v5.0 公式直译版）

规则依据：《工行杯 e日金 商业计划书 v5.0》附录 C 公式总表 + 《档位算法定版 v1.0》
实现范围：预算/档位（双校验 + 500 状态门槛）/ 每日发放（安全截断 + 预算保护状态）/
          余量回滚（荧光标记 + 500 开闸 + 超额缓冲）/ 放纵日（W 公式）/ 周期末结转 / 退出
暂不实现（留插槽）：自律评分、绿碳、白名单、紧急通道、影子锁

运行：python app.py → http://127.0.0.1:5000
"""
from datetime import datetime

from flask import Flask, jsonify, request, render_template_string

from engine import (D0, W0, N, CYCLE_DAYS, TIERS, TIER_ORDER,
                    k_max, rigid_e, daily_ledger, constraint_state,
                    tier_options, issue_amount, settle_rollback, indulgence_w)

app = Flask(__name__)


def new_seed():
    return {
        "status": "INIT",          # INIT | ACTIVE | INDULGENCE | EXITED | CLOSED
        "balance": 0,              # 账户从 0 开始（演示口径：一开始什么都没有）；设预算时＝把 M 存入
        "K_fixed": 0,              # 锁定池（水库）从 0 开始——新用户＝基础约束状态，仅开放 L1-L2（母版 3.3）
        "R_t": 0,                  # 回滚池（高位池塘）
        "R_accrued": 0,            # 本周期有效回滚累计（关账后成为下周期 R₋₁）
        "R_prev": 0,               # 上一周期有效回滚总额（行为统计量，放纵加码用）
        "indulgence_balance": 0,   # 放纵日余额（滚存）
        "M": 1500, "tier": None, "K_new": 0, "E": 0, "B": 0, "B_daily": 0,
        "day": 0, "quota_today": 0, "spent_today": 0,
        "spent_total": 0, "issued_total": 0,
        "breach_count": 0, "breach_days": [],
        "protection_note": "",
        "days": {},                # dayNo -> {issued, spent, settled}
        "history": [],
        "last_cycle": None,
    }


ST = new_seed()


def ev(t, p=None, stamp=None):
    ST["history"].append({
        "id": len(ST["history"]) + 1,
        "type": t,
        "time": stamp or datetime.now().strftime("%H:%M:%S"),
        "payload": p or {},
    })


def remaining_today():
    return ST["quota_today"] - ST["spent_today"]


def disposable():
    """可支配 = 钱包余额 − 各受限视图（资金不划转，池是账本视图）"""
    locked = ST["K_fixed"] + ST["R_t"]
    if ST["status"] == "ACTIVE":
        locked += ST["E"] + W0
    elif ST["status"] == "INDULGENCE":
        locked += ST["E"]
    return ST["balance"] - locked


def settle_day(day_no):
    """母版 3.7：结算某日余量 → 荧光标记入回滚池 → 满 500 开闸 + 超额缓冲"""
    d = ST["days"].get(day_no)
    if not d or d["settled"]:
        return
    r = max(0, d["issued"] - d["spent"])
    if r > 0:
        res = settle_rollback(ST["R_t"], r)
        ST["R_t"] = res["pool"]
        ST["K_fixed"] += res["to_lock"]
        ST["R_accrued"] += r
        ev("ROLLBACK_MARKED", {
            "dayNo": day_no, "amount": r, "poolAfter": ST["R_t"],
            "note": "第 %d 天 06:00 结算：余量 ¥%d 荧光标记入回滚池（池内 ¥%d/500，只进不出）" % (day_no, r, ST["R_t"])},
           stamp="D%d 06:00" % day_no)
        if res["opened"]:
            ev("ROLLBACK_GATE_OPENED", {
                "dayNo": day_no, "toLock": res["to_lock"],
                "note": "回滚池满 500，开闸 ¥%d 全额转入锁定池（水库）——高位池塘注定流向水库" % res["to_lock"]},
               stamp="D%d 06:00" % day_no)
        if res["to_user"] > 0:
            ev("BUFFER_RELEASED", {
                "dayNo": day_no, "toUser": res["to_user"], "poolRemains": ST["R_t"],
                "note": "超额缓冲：¥%d 释放归用户（自由支配），回滚池新起点 ¥%d" % (res["to_user"], ST["R_t"])},
               stamp="D%d 06:00" % day_no)
    d["settled"] = True


def issue_next_day():
    """发放下一天普通日额度（母版 3.6 + 2026-09-18 演示口径）：
    06:00 结算 → 查余额判断能否维系 → 08:00 发放；
    预算不够 → 先退回锁定池金额（档位下调一档）并入预算重新规划；
    退无可退（L1）→ 暂停发放；余额恢复满足保障线后自动恢复发放。"""
    new_day = ST["day"] + 1
    b_remain = daily_ledger() - ST["spent_total"]     # 发放前的日常账本剩余
    d_rem = CYCLE_DAYS - ST["day"]                    # 新一日的剩余管理天数（首日=30，母版 3.6 口径）
    res = issue_amount(b_remain, d_rem)
    if res["protection"]:
        idx = TIER_ORDER.index(ST["tier"]) if ST["tier"] else -1
        if idx > 0:
            old = TIER_ORDER[idx]
            new_tier = TIER_ORDER[idx - 1]
            new_k = TIERS[new_tier]
            diff = ST["K_new"] - new_k
            ST["tier"] = new_tier
            ST["K_new"] = new_k
            ST["K_fixed"] -= diff              # 退回锁定池金额（释放为可支配）
            ST["B"] = ST["M"] - new_k
            ST["E"] = rigid_e(ST["M"], new_k)  # 与本周期预算合并重新规划
            note = ("预算不够（保护状态）：已退回锁定池 ¥%d、档位 %s→%s 并入预算重新规划；"
                    "本日暂停发放" % (diff, old, new_tier))
            ST["protection_note"] = note
            ev("PROTECTION_TRIGGERED", {"dayNo": new_day, "note": note}, stamp="D%d 06:00" % new_day)
        else:
            note = ("预算不够（保护状态）：锁定池已无可退金额，暂停发放"
                    "（可支配资金仍可消费，余额恢复后自动恢复发放）")
            ST["protection_note"] = note
            ev("PROTECTION_TRIGGERED", {"dayNo": new_day, "note": note}, stamp="D%d 06:00" % new_day)
        ST["day"] = new_day
        ST["quota_today"] = 0
        ST["spent_today"] = 0
        ST["days"][new_day] = {"issued": 0, "spent": 0, "settled": False}
        ev("DAY_ISSUED", {"dayNo": new_day, "issued": 0, "remaining": 0,
                          "note": "第 %d 天 08:00 暂停发放（预算不够）" % new_day},
           stamp="D%d 08:00" % new_day)
        return True
    ST["day"] = new_day
    ST["quota_today"] = res["amount"]
    ST["spent_today"] = 0
    ST["issued_total"] += res["amount"]
    ST["days"][new_day] = {"issued": res["amount"], "spent": 0, "settled": False}
    ST["protection_note"] = ""
    ev("DAY_ISSUED", {
        "dayNo": new_day, "issued": res["amount"], "remaining": res["amount"],
        "note": "第 %d 天 08:00 发放 ¥%d——额度闸门打开，钱原地不动" % (new_day, res["amount"])},
       stamp="D%d 08:00" % new_day)
    return True


def close_cycle():
    """母版 3.7/3.8 周期末结转：刚需池→水库；回滚池结转（不拆分不提取）；放纵日余额滚存"""
    left = max(0, ST["quota_today"] - ST["spent_today"])
    if left > 0:
        ST["indulgence_balance"] += left
    e_remain = ST["E"]
    ST["K_fixed"] += e_remain                    # 刚需池期末剩余全额入水库
    carried = ST["R_t"]                          # 回滚池结转下周期
    ST["last_cycle"] = {
        "tier": ST["tier"], "M": ST["M"], "K": ST["K_new"],
        "rigidToLock": e_remain, "rollbackCarried": carried,
        "indulgenceLeft": left, "status": "CLOSED"}
    ST["R_prev"] = ST["R_accrued"]               # 本周期有效回滚 → 下周期 R₋₁（行为统计量）
    ST["R_accrued"] = 0
    ST.update(status="CLOSED", day=0, quota_today=0, spent_today=0,
              tier=None, K_new=0, E=0, B=0, B_daily=0)
    ev("CYCLE_CLOSED", {
        "rigidToLock": e_remain, "rollbackCarried": carried,
        "indulgenceBalance": ST["indulgence_balance"],
        "note": "周期关账：刚需池 ¥%d 全额入水库；回滚池 ¥%d 结转下周期（不拆分、不提取）；"
                "放纵日余额滚存（下月可提 50%%）" % (e_remain, carried)},
       stamp="D30 23:59")


def payload():
    cyc = None
    if ST["status"] in ("ACTIVE", "INDULGENCE", "CLOSED", "EXITED"):
        cyc = {
            "status": ST["status"], "M": ST["M"], "tier": ST["tier"],
            "K": ST["K_new"], "E": ST["E"], "L": N * D0, "W0": W0,
            "dayCount": ST["day"],
            "issuedTotal": ST["issued_total"], "spentTotal": ST["spent_total"],
            "today": {"dayNo": ST["day"], "issued": ST["quota_today"],
                      "spent": ST["spent_today"], "remaining": remaining_today()}
            if ST["status"] in ("ACTIVE", "INDULGENCE") else None,
        }
    return {
        "status": ST["status"],
        "balance": ST["balance"], "disposable": disposable(),
        "K_fixed": ST["K_fixed"], "R_t": ST["R_t"], "R_prev": ST["R_prev"],
        "indulgence_balance": ST["indulgence_balance"],
        "constraint": constraint_state(ST["K_fixed"]),
        "M": ST["M"], "kMax": k_max(ST["M"]),
        "tier": ST["tier"],
        "tiers": tier_options(ST["M"], ST["K_fixed"]),
        "cycle": cyc,
        "breach_count": ST["breach_count"],
        "protection_note": ST["protection_note"],
        "last_cycle": ST["last_cycle"],
        "events": ST["history"],
    }


# ---------------- API ----------------

@app.get("/api/state")
def api_state():
    return jsonify(payload())


@app.post("/api/budget")
def api_budget():
    d = request.get_json(silent=True) or {}
    try:
        M = int(d.get("M", 1500))
    except (TypeError, ValueError):
        return jsonify(error="预算 M 必须是数字"), 400
    if M <= 0:
        return jsonify(error="预算 M 必须大于 0"), 400
    if ST["status"] in ("ACTIVE", "INDULGENCE"):
        return jsonify(error="当前周期正在运行，不能修改预算"), 400
    ST["M"] = M
    ST["balance"] = M              # 演示口径：开新周期＝把 M 存入账户并纳入管理（M 是用户自定金额，母版 3.3）
    if ST["tier"] and not any(x["tier"] == ST["tier"] and x["ok"]
                              for x in tier_options(M, ST["K_fixed"])):
        ST["tier"] = None
    ev("BUDGET_SET", {"M": M, "note": "设定本周期预算 M = ¥%d" % M})
    return jsonify(payload())


@app.post("/api/tier")
def api_tier():
    d = request.get_json(silent=True) or {}
    t = d.get("tier")
    if ST["status"] in ("ACTIVE", "INDULGENCE"):
        return jsonify(error="当前周期正在运行，不能修改档位"), 400
    opt = next((x for x in tier_options(ST["M"], ST["K_fixed"]) if x["tier"] == t), None)
    if not opt:
        return jsonify(error="无效档位"), 400
    if not opt["ok"]:
        return jsonify(error="档位不可用：%s" % opt["reason"]), 400
    ST["tier"] = t
    ev("TIER_SELECTED", {"tier": t, "K": opt["K"],
                         "note": "已选择 %s：锁定额度 ¥%d" % (t, opt["K"])})
    return jsonify(payload())


@app.post("/api/start")
def api_start():
    if ST["status"] in ("ACTIVE", "INDULGENCE"):
        return jsonify(error="周期已经开始"), 400
    if not ST["tier"]:
        return jsonify(error="请先选择锁定档位"), 400
    opt = next((x for x in tier_options(ST["M"], ST["K_fixed"]) if x["tier"] == ST["tier"]), None)
    if not opt or not opt["ok"]:
        return jsonify(error="档位不可用：%s" % (opt["reason"] if opt else "不存在")), 400
    K = opt["K"]
    ST["K_new"] = K
    ST["E"] = rigid_e(ST["M"], K)
    ST["B"] = ST["M"] - K
    ST["B_daily"] = daily_ledger()
    ST["K_fixed"] += K                          # 锁定即入锁定池（母版 3.3）
    ST.update(status="ACTIVE", day=1, quota_today=D0, spent_today=0,
              spent_total=0, issued_total=D0, breach_count=0, breach_days=[],
              protection_note="", R_accrued=0,
              days={1: {"issued": D0, "spent": 0, "settled": False}})
    ev("CYCLE_STARTED", {
        "M": ST["M"], "tier": ST["tier"], "K": K, "E": ST["E"], "L": N * D0, "W0": W0,
        "note": "拆分：刚需 ¥%d + 锁定 ¥%d + 普通日 %d×¥%d + 放纵日 ¥%d = ¥%d ✓ 闭合"
                % (ST["E"], K, N, D0, W0, ST["M"])})
    ev("DAY_ISSUED", {
        "dayNo": 1, "issued": D0, "remaining": D0,
        "note": "第 1 天 08:00 发放 ¥%d——额度闸门打开，钱原地不动" % D0},
       stamp="D1 08:00")
    return jsonify(payload())


@app.post("/api/advance")
def api_advance():
    if ST["status"] not in ("ACTIVE", "INDULGENCE"):
        return jsonify(error="当前没有正在运行的预算周期"), 400
    d = request.get_json(silent=True) or {}
    try:
        hours = int(d.get("hours", 24))
    except (TypeError, ValueError):
        hours = 24
    if hours <= 0:
        return jsonify(error="快进时间必须大于 0"), 400
    for _ in range(max(1, hours // 24)):
        if ST["status"] == "ACTIVE":
            if ST["day"] >= 1:
                settle_day(ST["day"])          # 收口当日余量（母版 3.7：次日发放时结算前日）
            if ST["day"] >= N:                 # 29 个普通日发放完毕 → 放纵日（替代当天日发）
                W = indulgence_w(ST["R_prev"])
                ST.update(status="INDULGENCE", day=CYCLE_DAYS, quota_today=W, spent_today=0)
                ev("INDULGENCE_DAY", {
                    "dayNo": CYCLE_DAYS, "W": W,
                    "note": "第 30 天·放纵日（母版 3.8）：W = min(100 + 0.5×R₋₁ + W_draw, 300) = ¥%d，"
                            "替代当天普通日发放（非叠加）" % W},
                   stamp="D30 08:00")
            else:
                if not issue_next_day():
                    break                  # 保护状态已导致全额退出
        elif ST["status"] == "INDULGENCE":
            close_cycle()
            break
    return jsonify(payload())


@app.post("/api/consume")
def api_consume():
    if ST["status"] not in ("ACTIVE", "INDULGENCE"):
        return jsonify(error="当前没有正在运行的预算周期"), 400
    d = request.get_json(silent=True) or {}
    try:
        amount = int(d.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify(error="消费金额必须是数字"), 400
    if amount <= 0:
        return jsonify(error="消费金额必须大于 0"), 400
    if ST["status"] == "INDULGENCE":
        rem = ST["quota_today"] - ST["spent_today"]
        if amount > rem:
            return jsonify(error="超出放纵日额度（当前剩余 ¥%d）——不可透支（母版 3.8）" % rem), 400
    ST["spent_today"] += amount
    ST["spent_total"] += amount
    ST["balance"] -= amount                     # 只有消费产生实际支出（资金不划转）；余额可为负，红字警示
    if ST["status"] == "ACTIVE":
        ST["days"][ST["day"]]["spent"] = ST["spent_today"]
        # 40 元标尺：单日累计消费 > 40（非刚需/白名单/紧急/放纵）→ 记一次突破
        # （2026-09-17 定稿拍板：统一"单日累计"口径，单笔超 40 不单独触发）
        if ST["spent_today"] > 40 and ST["day"] not in ST["breach_days"]:
            ST["breach_days"].append(ST["day"])
            ST["breach_count"] += 1
            ev("BUDGET_BREACH", {
                "dayNo": ST["day"], "amount": ST["spent_today"],
                "note": "第 %d 天单日累计消费 ¥%d 超过 40 元标尺——不拒付、不冻结、不压后续日发到保障线以下；"
                        "仅记录（评分联动属后续模块）" % (ST["day"], ST["spent_today"])})
    ev("SPEND", {
        "dayNo": ST["day"], "amount": amount, "remaining": remaining_today(),
        "note": "第 %d 天消费 ¥%d，今日剩余额度 ¥%d（可为负，红字警示）" % (ST["day"], amount, remaining_today())},
       stamp="D%d %s" % (ST["day"], datetime.now().strftime("%H:%M")))
    return jsonify(payload())


@app.post("/api/exit")
def api_exit():
    if ST["status"] not in ("ACTIVE", "INDULGENCE"):
        return jsonify(error="当前没有正在运行的预算周期"), 400
    total_fixed = ST["K_fixed"]
    pool = ST["R_t"]
    ST.update(status="EXITED", K_fixed=0, R_t=0, quota_today=0, spent_today=0,
              tier=None, K_new=0, E=0, B=0, B_daily=0, day=0, R_accrued=0)
    ev("CYCLE_EXITED", {
        "K": total_fixed, "rollback": pool,
        "note": "退出申请即时生效：锁定资金 ¥%d（含历史固化）+ 回滚池 ¥%d 全额回到可用——一分不少"
                "（T+3 仅为到账设计目标）" % (total_fixed, pool)})
    return jsonify(payload())


@app.post("/api/dev")
def api_dev():
    d = request.get_json(silent=True) or {}
    action = d.get("action")
    if action == "setRollback497":
        ST["R_t"] = 497
        ev("DEV_SET_ROLLBACK", {
            "amount": 497,
            "note": "开发工具：回滚池调至 ¥497（下一次余量回滚即触发 500 开闸与超额缓冲）"})
        return jsonify(payload())
    if action == "reset":
        ST.clear()
        ST.update(new_seed())
        return jsonify(payload())
    return jsonify(error="未知操作"), 400


# ---------------- 页面 ----------------

HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>e日金 · 青年账户预算管理服务</title>
<style>
:root{--r:#c8102e;--rd:#a80722;--rs:#fff1f3;--ink:#171717;--muted:#737373;--line:#e8e8e8;--paper:#fff;--bg:#f7f7f5}
*{box-sizing:border-box}body{margin:0;color:var(--ink);font-family:Inter,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;background:radial-gradient(circle at 85% 4%,rgba(200,16,46,.055),transparent 25rem),var(--bg)}
button,input{font:inherit}button{cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}
.page{min-height:100vh;padding:24px}.shell{width:min(1180px,100%);margin:auto}.top{height:58px;display:flex;align-items:center;justify-content:space-between;gap:10px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:11px;font-weight:800}.mark{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;background:var(--r);color:#fff;font-weight:900;font-style:italic}
.topright{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.status{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);background:#fff;border-radius:999px;padding:7px 11px;font-size:12px;color:#525252}.dot{width:7px;height:7px;border-radius:50%;background:#18a957}
.badge{display:inline-flex;align-items:center;border-radius:999px;padding:7px 11px;font-size:11px;font-family:monospace}
.badge.basic{background:#fff;border:1px solid var(--line);color:#525252}
.badge.full{background:var(--r);color:#fff}
.hero{padding:58px 0 30px}.eyebrow{margin:0 0 12px;color:var(--r);font-size:11px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}
h1{margin:0;font-size:clamp(38px,6vw,72px);line-height:.98;letter-spacing:-.065em;font-weight:850}.sub{margin:18px 0 0;max-width:650px;color:var(--muted);line-height:1.7;font-size:14px}
.grid{display:grid;grid-template-columns:1.55fr .85fr;gap:14px}.card{background:var(--paper);border:1px solid var(--line);border-radius:18px;padding:24px}.head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:22px}
.label{color:#737373;font-size:11px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.amount{font-size:clamp(38px,5vw,62px);line-height:1;letter-spacing:-.055em;font-weight:800}.red{color:var(--r)}.mono{font-family:"SFMono-Regular",Consolas,monospace;font-variant-numeric:tabular-nums}
.day{margin-top:30px;font-size:14px;font-weight:750}.day span{color:#888;font-weight:500}.bar{height:5px;background:#ededed;border-radius:999px;overflow:hidden;margin-top:12px}.bar i{display:block;height:100%;background:var(--r);border-radius:inherit}
.dual{display:grid;grid-template-columns:1fr 1fr;border-top:1px solid var(--line);margin-top:24px}.stat{padding:18px 0 0}.stat+.stat{padding-left:20px;border-left:1px solid var(--line)}.sv{margin-top:5px;font-size:24px;font-weight:800}
.input{width:100%;border:1px solid #dcdcdc;border-radius:11px;padding:13px 14px;outline:0;background:#fff}.input:focus{border-color:var(--r);box-shadow:0 0 0 3px rgba(200,16,46,.08)}
.setup{display:grid;gap:18px}.tiers{display:grid;grid-template-columns:repeat(5,1fr);gap:7px}.tier{border:1px solid #ddd;background:#fff;border-radius:10px;padding:12px 6px;text-align:center}.tier.active{border-color:var(--r);background:var(--rs);color:var(--rd)}.tier b,.tier small{display:block}.tier small{margin-top:3px;color:#888;font-size:10px}
.tnote{margin-top:6px;font-size:11px;color:#999;line-height:1.6}
.btn{border:0;border-radius:11px;padding:13px 16px;font-weight:800}.primary{background:var(--r);color:#fff}.secondary{background:#f1f1f1;color:#222}.danger{background:#fff;color:var(--r);border:1px solid #efcbd1}
.split{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line);margin-top:22px}.pool{padding:17px 12px 3px 0}.pool+.pool{padding-left:14px;border-left:1px solid var(--line)}.pn{color:#777;font-size:11px}.pv{margin-top:6px;font-size:20px;font-weight:800}
.water{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:16px}.wcard{border:1px solid var(--line);border-radius:12px;padding:16px}.wnote{margin-top:8px;color:#888;font-size:11px;line-height:1.7}
.gauge{height:10px;background:#f0f0f0;border-radius:999px;overflow:hidden;margin-top:12px}.gauge i{display:block;height:100%;background:linear-gradient(90deg,var(--rd),#e63e57);border-radius:inherit}
.half{font-size:.45em;color:#999;font-weight:600}
.feed{max-height:390px;overflow:auto}.event{display:grid;grid-template-columns:62px 1fr auto;gap:12px;align-items:start;padding:13px 0;border-top:1px solid var(--line)}.event:first-child{border-top:0}.time{color:#999;font-size:11px;font-family:monospace}.em{font-size:13px;font-weight:650}.en{margin-top:4px;color:#888;font-size:11px;line-height:1.5}.ea{color:var(--r);font-family:monospace;font-size:12px;white-space:nowrap}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}.err{margin-top:12px;color:var(--rd);background:var(--rs);border:1px solid #f4ccd2;border-radius:10px;padding:10px 12px;font-size:12px;display:none}.note{color:#777;font-size:12px;line-height:1.7;margin-top:24px}.footer{padding:28px 0 10px;display:flex;justify-content:space-between;color:#888;font-size:11px}
.banner{margin-top:14px;background:var(--rs);border:1px solid #f4ccd2;color:var(--rd);border-radius:10px;padding:10px 12px;font-size:12px;line-height:1.6}
.chip2{display:inline-block;margin-top:14px;background:#f6f6f6;border:1px solid var(--line);border-radius:999px;padding:4px 12px;font-size:11px;color:#555}
.exitbox{margin-top:16px;padding:15px 16px;border-radius:12px;background:#f6f6f6;border:1px solid var(--line);font-size:13px;line-height:1.9}
@media(max-width:820px){.page{padding:14px}.hero{padding:38px 0 24px}.grid{grid-template-columns:1fr}.split{grid-template-columns:repeat(2,1fr)}.pool:nth-child(3){border-left:0;padding-left:0}.water{grid-template-columns:1fr}.footer{flex-direction:column;gap:10px}}
@media(max-width:520px){.card{padding:18px}h1{font-size:43px}.amount{font-size:48px}.event{grid-template-columns:48px 1fr}.ea{grid-column:2}}
</style></head><body><div class="page"><div class="shell">
<header class="top"><div class="brand"><span class="mark">e</span><span>e日金</span></div>
<div class="topright"><span id="badge" class="badge"></span><div class="status"><span class="dot"></span><span id="status">准备建立周期</span></div></div></header>
<section class="hero"><p class="eyebrow">Youth Account · Budget Gate</p><h1>把额度打开，<br>让钱留在原地。</h1><p class="sub">e日金不是把钱转走，而是在原有账户余额上建立可支付额度的规则闸门。每日额度按规则开放，消费只记录预算使用。</p></section>
<div id="app"></div>
<footer class="footer"><span><b>e日金</b> · 青年账户预算管理服务</span><span>规则引擎管钱 · 额度闸门打开 · 钱原地不动</span></footer>
</div></div>
<script>
const $=id=>document.getElementById(id), money=n=>{const v=Math.round(Number(n)||0);return (v<0?"-¥":"¥")+Math.abs(v).toLocaleString("zh-CN")};
let S=null,M=1500,tier="L5",busy=false;
async function load(){const r=await fetch("/api/state",{cache:"no-store"}),d=await r.json();if(!r.ok)throw Error(d.error||"读取状态失败");S=d;M=S.M||M;const cur=S.tiers.find(x=>x.tier===tier);if(!cur||!cur.ok){const ok=S.tiers.find(x=>x.ok);if(ok)tier=ok.tier}render()}
async function post(p,b){busy=true;render();let em=null;try{const r=await fetch(p,{method:"POST",headers:b?{"Content-Type":"application/json"}:undefined,body:b?JSON.stringify(b):undefined}),d=await r.json();if(!r.ok)throw Error(d.error||"操作失败");S=d}catch(e){em=e.message}finally{busy=false;render();if(em)err(em)}}
function err(m){const x=$("error");if(x){x.style.display="block";x.textContent=m}else alert(m)}
function info(e){const p=e.payload||{},names={BUDGET_SET:"设置预算",TIER_SELECTED:"选择约束档位",CYCLE_STARTED:"开始预算周期",DAY_ISSUED:"每日额度开放",SPEND:"发生消费",BUDGET_BREACH:"预算突破",ROLLBACK_MARKED:"余量回滚",ROLLBACK_GATE_OPENED:"回滚开闸",BUFFER_RELEASED:"超额缓冲",PROTECTION_TRIGGERED:"预算保护",INDULGENCE_DAY:"放纵日",CYCLE_CLOSED:"周期关账",CYCLE_EXITED:"退出预算约束",DEV_SET_ROLLBACK:"开发工具"};let a="";
if(e.type==="SPEND")a="-¥"+p.amount;else if(p.issued)a="+¥"+p.issued;else if(e.type==="ROLLBACK_MARKED")a="¥"+p.amount;else if(e.type==="ROLLBACK_GATE_OPENED")a="+¥"+p.toLock;else if(e.type==="BUFFER_RELEASED")a="¥"+p.toUser;
return{t:names[e.type]||e.type,a:a,n:p.note||""}}
function setup(){const ts=S.tiers.map(x=>`<button class="tier ${tier===x.tier?"active":""}" data-tier="${x.tier}" ${!x.ok||busy?"disabled":""} title="${x.reason||""}"><b>${x.tier}</b><small>${money(x.K)}</small></button>`).join("");const rs=[...new Set(S.tiers.filter(x=>!x.ok).map(x=>x.reason))].map(r=>`<div class="tnote">✕ ${r}</div>`).join("");
return `<section class="card"><div class="head"><div><div class="label">建立本周期预算</div><b>先设预算，再选择约束档位</b></div><span class="status">Kmax ${money(S.kMax)}</span></div>
<div class="setup"><label><div class="label">本周期预算 M · 账户总额 ${money(S.balance)}</div><input id="budget" class="input mono" value="${M}" inputmode="numeric"></label>
<div><div class="label">锁定档位（L1-L5）</div><div class="tiers">${ts}</div>${rs}</div>
${S.kMax<=0?`<div class="banner">当前预算连最低结构都撑不起——系统如实拒绝建立锁定（母版 3.3·用户 C 口径）</div>`:""}
<button id="start" class="btn primary" ${busy?"disabled":""}>锁定并开始 · 第 1 天立即开放 ¥30</button>
<div id="error" class="err"></div></div></section>`}
function active(a){const t=a.today||{dayNo:a.dayCount||1,issued:0,spent:0,remaining:0};const p=Math.min(100,t.dayNo/30*100),rp=S.R_t,kf=S.K_fixed,rpP=Math.min(100,rp/500*100),kfP=Math.min(100,kf/500*100),es=S.events.slice().reverse();
const breach=S.breach_count>0?`<div class="chip2">⚑ 预算突破 ${S.breach_count} 次（40 元标尺 · 单日累计 · 仅记录）</div>`:"";
const prot=S.protection_note?`<div class="banner">⚠ ${S.protection_note}</div>`:"";
return `<section class="grid">
<div class="card"><div class="head"><div class="label">今日可支付额度</div><span class="status">${a.tier} 约束 · ${money(a.K)} 锁定</span></div>
<div class="amount mono ${t.remaining<0?"red":""}">${money(t.remaining)}</div>
<div class="day">第 ${t.dayNo} / 30 天 <span>· 预算周期（29 普通日 + 1 放纵日）</span><div class="bar"><i style="width:${p}%"></i></div></div>
<div class="dual"><div class="stat"><div class="label">今日已开放</div><div class="sv mono">${money(t.issued)}</div></div><div class="stat"><div class="label">今日已消费</div><div class="sv mono">${money(t.spent)}</div></div></div>
${prot}${breach}
<div class="actions"><input id="ci" class="input mono" style="width:110px" value="23" inputmode="numeric"><button id="consume" class="btn primary">记录消费</button></div>
<div class="wnote">超出今日额度＝预算突破：不拒付、不冻结——仅记录（40 是标尺不是墙）；剩余额度可为负，红字警示</div></div>
<div class="card"><div class="head"><div><div class="label">本周期预算</div><b>${a.tier} · ${money(a.K)} 锁定</b></div><span class="status">${a.status}</span></div>
<div class="amount mono">${money(a.M)}</div>
<div class="split"><div class="pool"><div class="pn">刚需池 E</div><div class="pv mono">${money(a.E)}</div></div><div class="pool"><div class="pn">锁定池 K</div><div class="pv mono">${money(a.K)}</div></div><div class="pool"><div class="pn">普通日 29×¥30</div><div class="pv mono">${money(a.L)}</div><div class="wnote">已发 ${money(a.issuedTotal)} · 未发 ${money(Math.max(0,a.L-a.issuedTotal))}</div></div><div class="pool"><div class="pn">放纵日预留</div><div class="pv mono">${money(a.W0)}</div></div></div>
<div class="wnote">账目闭合：${a.E} + ${a.K} + ${a.L} + ${a.W0} = ${a.M} ✓<br>放纵日是替换当天 30 元，不是多发 100 元。</div></div></section>
<section class="card" style="margin-top:14px"><div class="head"><div><div class="label">水系 · 资金池</div><b>资金不划转——池＝余额上的受限视图</b></div><span class="mono" style="font-size:12px;color:#888">${S.constraint==="FULL"?"完整约束状态":"基础约束状态"}</span></div>
<div class="water">
<div class="wcard"><div class="label">钱包 · 可支配</div><div class="pv mono" style="font-size:26px;${S.disposable<0?"color:var(--r)":""}">${money(S.disposable)}</div><div class="wnote">可支配 = 账户总额 ¥${money(S.balance)} − 锁定池 ¥${money(kf)} − 回滚池 ¥${money(rp)} − 刚需池 ¥${money(a.E)} − 放纵日预留 ¥${money(a.W0)}<br>只有消费产生实际支出</div></div>
<div class="wcard"><div class="label">回滚池 · 高位池塘</div><div class="pv mono" style="font-size:26px">${money(rp)}<span class="half"> / 500</span></div><div class="gauge"><i style="width:${rpP}%"></i></div><div class="wnote">只进不出 · 满 500 开闸入水库<br><b class="red">500 · 开闸水位线</b>（资金流转阈值）</div></div>
<div class="wcard"><div class="label">锁定池 · 水库</div><div class="pv mono" style="font-size:26px">${money(kf)}</div><div class="gauge"><i style="width:${kfP}%"></i></div><div class="wnote">日常不可动 · 逐步扩张 · 退出全额取回<br><b class="red">500 · 状态门槛</b>（解锁全档，两个 500 不是同一个数）</div></div>
</div></section>
<section class="card" style="margin-top:14px"><div class="head"><div><div class="label">账本 / Event Ledger</div><b>规则引擎记录每一次额度变化</b></div><span class="mono" style="font-size:12px;color:#888">${es.length} events</span></div>
<div class="feed">${es.map(e=>{const x=info(e);return `<div class="event"><div class="time">${e.time||"--:--"}</div><div><div class="em">${x.t}</div>${x.n?`<div class="en">${x.n}</div>`:""}</div><div class="ea">${x.a}</div></div>`}).join("")}</div>
<div id="error" class="err"></div>
<div class="actions"><button id="advance" class="btn secondary">⏩ 快进 24h（次日发放）</button><button id="advance7" class="btn secondary">⏩ 快进 7 天</button><button id="dev497" class="btn secondary">🔧 回滚池 → ¥497（演示开闸）</button><button id="exit" class="btn danger">退出本周期</button><button id="reset" class="btn secondary">重置 Demo</button></div></section>`}
function indulgence(a){const t=a.today||{issued:0,spent:0};const W=t.issued,rem=Math.max(0,W-t.spent);
return `<section class="card"><div class="head"><div><div class="label">第 30 天 · 放纵日</div><b>放纵日额度替代当天普通日发放</b></div><span class="status">INDULGENCE</span></div>
<div class="amount red mono">${money(rem)}</div>
<div class="wnote">W = min(100 + 0.5×R₋₁ + W_draw, 300) = ${money(W)}（R₋₁=¥${S.R_prev}）；消费从账户余额扣，不动回滚池/水库资金。</div>
<div class="actions"><input id="ci" class="input mono" style="width:110px" value="100" inputmode="numeric"><button id="consume" class="btn primary">放纵日消费</button><button id="advance" class="btn secondary">⏩ 快进至关账</button><button id="exit" class="btn danger">退出（全额回可用）</button></div>
<div id="error" class="err"></div></section>`}
function closed(){const lc=S.last_cycle||{};
return `<section class="card"><div class="head"><div><div class="label">周期已关账</div><b>本周期结算完成</b></div><span class="status">CLOSED</span></div>
<div class="exitbox">刚需池 <b>${money(lc.rigidToLock||0)}</b> 全额转入水库（专项水道）<br>回滚池 <b>${money(lc.rollbackCarried||0)}</b> 结转下周期（不拆分、不提取）<br>放纵日余额 <b>${money(S.indulgence_balance)}</b> 已滚存（下月放纵日可提取 50%）<br>上一周期有效回滚 R₋₁ = <b>${money(S.R_prev)}</b>（下月放纵日加码 0.5×R₋₁）</div></section>`}
function exited(){const e=S.events.filter(x=>x.type==="CYCLE_EXITED").pop(),p=e?e.payload||{}:{};
return `<section class="card"><div class="head"><div><div class="label">本周期状态</div><b>预算约束已退出</b></div><span class="status">EXITED</span></div>
<div class="exitbox">退出申请即时生效：锁定资金 <b>${money(p.K||0)}</b>（含历史固化）+ 回滚池 <b>${money(p.rollback||0)}</b> 已全额回到可用——<b>一分不少</b>。<br>本 Demo 到此结束：用户可以退出预算约束，不需要系统批准。<br>钱始终在用户本人账户——产品不托管、不代持、不经手（T+3 仅为到账设计目标）。</div>
<div class="actions"><button id="reset" class="btn primary">重新开始 Demo</button></div></section>`}
function render(){if(!S)return;const stt={INIT:"准备建立周期",ACTIVE:"预算约束运行中",INDULGENCE:"放纵日 · 第 30 天",EXITED:"本周期已退出",CLOSED:"周期已关账"};
$("status").textContent=stt[S.status]||S.status;
$("badge").className="badge "+(S.constraint==="FULL"?"full":"basic");
$("badge").textContent=S.constraint==="FULL"?"完整约束状态 · 水库≥500":"基础约束状态 · 水库<500";
const app=$("app");
if(S.status==="ACTIVE")app.innerHTML=active(S.cycle);
else if(S.status==="INDULGENCE")app.innerHTML=indulgence(S.cycle);
else if(S.status==="EXITED")app.innerHTML=exited();
else if(S.status==="CLOSED")app.innerHTML=closed()+setup();
else app.innerHTML=setup();
bind()}
function bind(){let b=$("budget");if(b)b.onchange=()=>{M=Math.max(1,Number(b.value)||1500);post("/api/budget",{M})};
document.querySelectorAll("[data-tier]").forEach(x=>x.onclick=()=>{tier=x.dataset.tier;post("/api/tier",{tier})});
let st=$("start");if(st)st.onclick=async()=>{const r=await fetch("/api/budget",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({M:Number(M)})});const d=await r.json();if(!r.ok){err(d.error||"操作失败");return}S=d;render();if(!S.tier){await post("/api/tier",{tier})}if(!S.tier)return;await post("/api/start")};
document.querySelectorAll(".q").forEach(x=>x.onclick=()=>post("/api/consume",{amount:Number(x.dataset.a)}));
let c=$("consume");if(c)c.onclick=()=>{const n=Number($("ci").value);if(n>0)post("/api/consume",{amount:n})};
let a=$("advance");if(a)a.onclick=()=>post("/api/advance",{hours:24});
let a7=$("advance7");if(a7)a7.onclick=()=>post("/api/advance",{hours:168});
let dv=$("dev497");if(dv)dv.onclick=()=>post("/api/dev",{action:"setRollback497"});
let e=$("exit");if(e)e.onclick=()=>post("/api/exit");
let r=$("reset");if(r)r.onclick=()=>post("/api/dev",{action:"reset"})}
load().catch(e=>err(e.message));
</script></body></html>"""


@app.get("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
