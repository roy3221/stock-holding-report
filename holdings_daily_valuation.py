#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
holdings_daily_valuation.py  ——  持仓「估值 + 买入决策」每日早报

设计原则（重要）：
  IV（内在价值三档）是**慢变量**，依赖所有者盈余/增长假设等人工判断，
  只在做完整深研（position-thesis-monitor v2.0 的 Phase 4）或财报后更新，
  写进 holdings.json 缓存。本脚本**不每天重算 IV**，只做每天会变的部分：
  抓今日价格 → 对照缓存的 IV 三档 → 算安全边际 + 买入/卖出区间判定。

每天产出（对应深研报告的 §4 估值 + §5 买入决策与安全边际）：
  · 现价 vs IV 三档（保守/基准/乐观）、对基准 IV 的安全边际
  · 按护城河等级 + Integrity 的买入判定（强力/合理/观望/不值/极端高估）
  · 目标买入区间
  · 折现率漂移告警：若今日 10Y 国债较算 IV 时变动 >50bp → 提示该重算 IV

依赖：pip install yfinance
用法：
  python3 holdings_daily_valuation.py                 # 打印到终端
  python3 holdings_daily_valuation.py --email          # 计算并发邮件给自己
  python3 holdings_daily_valuation.py --html out.html  # 另存 HTML
邮件凭证从环境变量读取（脚本绝不内置密码）：
  GMAIL_USER=you@gmail.com  GMAIL_APP_PASSWORD=xxxx  MAIL_TO=you@gmail.com
"""

import os, sys, json, datetime, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

HERE = os.path.dirname(os.path.abspath(__file__))
CFG  = os.path.join(HERE, "holdings.json")

# ── 买入分层规则（与 position-thesis-monitor v2.0 Phase 5 完全一致）──────
# ratio = 现价 / IV_base（价格占基准 IV 的比例）。
# addon = 宏观高估叠加（小数，如 0.10 = +10pp 安全边际）：贵环境下要求更深折让，
#         即把所有 ratio 阈值整体下移 addon（skill: CAPE>35+Buffett Indicator>200% → +10pp）。
def classify(ratio_base, ratio_opt, moat, integrity, addon=0.0):
    """返回 (信号emoji, 文案)。moat∈{wide,narrow,none}；integrity∈{ok,warn,fail}"""
    if integrity == "fail":
        return "⛔", "Integrity ❌ 已排除（财报/未来现金流不可信，不参与估值结论）"
    block_strong = (integrity == "warn")  # ⚠️ 禁“强力买入”
    sfx = "（含宏观+%.0fpp）" % (addon * 100) if addon else ""

    if moat == "wide":
        if ratio_base <= 0.80 - addon:
            if block_strong:
                return "🟢🟢", f"合理买入（Integrity⚠️封顶，本可强力）{sfx}"
            tag = "（超深折让，先核查价值陷阱）" if ratio_base < 0.70 - addon else ""
            return "🟢🟢🟢", f"强力买入{tag}{sfx}"
        if ratio_base <= 0.85 - addon:
            return "🟢🟢", f"合理买入{sfx}"
        if ratio_base <= 0.95 - addon:
            return "🟡", f"观望（安全边际不足）{sfx}"
    elif moat == "narrow":
        if ratio_base <= 0.70 - addon:
            return "🟢🟢", f"合理买入（Narrow 无强力档）{sfx}"
        if ratio_base <= 0.85 - addon:
            return "🟡", f"观望（Narrow 要求 ≥30% 安全边际）{sfx}"
    else:  # none
        return "🔴", "无护城河，不纳入买入"

    # 高于观望区
    if ratio_opt is not None and ratio_opt > 1.0:
        return "🔴", "极端高估（>乐观IV）→ 触发“持有=买入”复核；但涨多≠卖出信号，看论文不看价"
    return "⚪", "持有不加（已达/接近基准 IV，无新钱安全边际）"


def fetch_price(tkr):
    import yfinance as yf
    t = yf.Ticker(tkr)
    # 优先 fast_info，失败回退到日线收盘
    try:
        p = t.fast_info.get("last_price") or t.fast_info.get("lastPrice")
        if p:
            return float(p)
    except Exception:
        pass
    h = t.history(period="5d")
    if len(h):
        return float(h["Close"].iloc[-1])
    raise RuntimeError(f"无法获取 {tkr} 价格")


def fetch_iv_estimates(tkr):
    """从 Yahoo Finance 抓取分析师目标价作为 IV 估算。
    返回 (conservative, base, optimistic, source) 或 None。"""
    import yfinance as yf
    t = yf.Ticker(tkr)
    try:
        targets = t.analyst_price_targets
        if targets is not None:
            if hasattr(targets, "to_dict"):
                targets = targets.to_dict()
            low = targets.get("low")
            median = targets.get("median") or targets.get("mean")
            high = targets.get("high")
            if low and median and high:
                return float(low), float(median), float(high), "Yahoo Analyst Targets"
    except Exception:
        pass
    try:
        info = t.info or {}
        target_low = info.get("targetLowPrice")
        target_median = info.get("targetMedianPrice") or info.get("targetMeanPrice")
        target_high = info.get("targetHighPrice")
        if target_low and target_median and target_high:
            return float(target_low), float(target_median), float(target_high), "Yahoo Analyst Targets"
    except Exception:
        pass
    return None


def fetch_10y():
    import yfinance as yf
    try:
        h = yf.Ticker("^TNX").history(period="5d")
        if len(h):
            raw = float(h["Close"].iloc[-1])
            # ^TNX 报价口径不一：可能是 4.45（百分数）或 44.5（×10）。归一到小数。
            if raw > 20:   return raw / 1000.0   # 44.5 -> 0.0445
            if raw > 1:    return raw / 100.0    # 4.45 -> 0.0445
            return raw                            # 已是小数
    except Exception:
        pass
    return None


def analyze(cfg):
    today = datetime.date.today().isoformat()
    r_now = fetch_10y()
    macro = cfg.get("macro", {}) or {}
    addon = (macro.get("mos_addon_pp", 0) or 0) / 100.0   # 宏观高估叠加(小数)
    pos_cap = macro.get("position_cap_pct")
    rows, alerts = [], []
    for h in cfg["holdings"]:
        tkr = h["ticker"]
        try:
            px = fetch_price(tkr)
        except Exception as e:
            rows.append({"ticker": tkr, "error": str(e)})
            continue
        # IV 来源：优先 holdings.json 手动值，否则自动抓取
        if h.get("iv_base"):
            ivc, ivb, ivo = h["iv_conservative"], h["iv_base"], h["iv_optimistic"]
            iv_source = "手动深研"
        else:
            auto = fetch_iv_estimates(tkr)
            if auto is None:
                rows.append({"ticker": tkr, "error": "无手动IV且无法获取分析师目标价"})
                continue
            ivc, ivb, ivo, iv_source = auto
        ratio_b = px / ivb
        ratio_o = px / ivo if ivo else None
        mos = (1 - px / ivb) * 100        # 对基准 IV 的安全边际（正=低估）
        sig, txt = classify(ratio_b, ratio_o, h.get("moat", "wide"),
                            h.get("integrity", "ok"), addon)
        # 目标买入区间（基准 IV × 护城河档，按宏观叠加同步收紧；Narrow 下界对齐 skill 0.60）
        moat = h.get("moat", "wide")
        if moat == "wide":
            buy_lo, buy_hi = ivb * (0.70 - addon), ivb * (0.85 - addon)
        elif moat == "narrow":
            buy_lo, buy_hi = ivb * (0.60 - addon), ivb * (0.70 - addon)
        else:
            buy_lo, buy_hi = None, None
        # 折现率漂移告警
        r_iv = h.get("r_at_iv_calc")
        r_flag = ""
        if r_now and r_iv and abs(r_now - r_iv) * 100 >= 50:  # >=50bp
            r_flag = f"⚠️10Y已从{r_iv:.2%}变到{r_now:.2%}（>50bp）→ 建议重算 IV"
            alerts.append(f"{tkr}: {r_flag}")
        rows.append({
            "ticker": tkr, "price": px, "ivc": ivc, "ivb": ivb, "ivo": ivo,
            "mos": mos, "sig": sig, "txt": txt,
            "buy_lo": buy_lo, "buy_hi": buy_hi, "r_flag": r_flag,
            "asof": h.get("iv_asof", "?"), "iv_source": iv_source,
        })
        # 可执行提醒：进入强力/合理买入(附三层全绿门提醒) 或 极端高估
        if sig in ("🟢🟢🟢", "🟢🟢"):
            cap = f"，仓位≤{pos_cap}%" if pos_cap else ""
            alerts.append(f"{tkr}: {txt} @ ${px:.2f}（基准IV ${ivb:.0f}，安全边际{mos:+.0f}%）"
                          f" → 下单前须过三层全绿(宏观🟢行业🟢个股🟢){cap}")
        elif sig == "🔴" and "极端高估" in txt:
            alerts.append(f"{tkr}: 极端高估 @ ${px:.2f}（>乐观IV ${ivo:.0f}）→ 复核论文")
    return today, r_now, rows, alerts, macro, addon


# ── 渲染 ────────────────────────────────────────────────────────────────
DISCLAIMER = ("判定 = position-thesis-monitor Phase 5(买入决策)+ 宏观叠加；"
              "IV来源：手动深研=DCF缓存，Yahoo Analyst Targets=分析师目标价(low/median/high)。"
              "仅供研究与纪律辅助，不构成投资建议。")

def render_text(today, r_now, rows, alerts, macro, addon):
    reg = macro.get("regime_note", "未设")
    macro_line = (f"宏观:{reg}" + (f" → 安全边际+{addon*100:.0f}pp"
                  f"、仓位≤{macro.get('position_cap_pct')}%" if addon else " → 无叠加"))
    L = [f"持仓估值日报 · {today} · 10Y={f'{r_now:.2%}' if r_now else 'n/a'} · {macro_line}",
         "=" * 64]
    if alerts:
        L.append("【今日可执行提醒】")
        L += [f"  • {a}" for a in alerts]
        L.append("")
    L.append(f"{'代码':<6}{'现价':>8}{'安全边际':>9}  {'判定'}")
    L.append("-" * 64)
    for r in rows:
        if r.get("error"):
            L.append(f"{r['ticker']:<6}{'取价失败: '+r['error']}")
            continue
        L.append(f"{r['ticker']:<6}{r['price']:>8.2f}{r['mos']:>8.0f}%  {r['sig']} {r['txt']}")
        L.append(f"       IV 保/基/乐: {r['ivc']:.0f}/{r['ivb']:.0f}/{r['ivo']:.0f}"
                 + (f" | 买入区 ${r['buy_lo']:.0f}-{r['buy_hi']:.0f}" if r['buy_lo'] else "")
                 + f" | 来源:{r['iv_source']}"
                 + (f" | IV算于{r['asof']}" if r['asof'] != '?' else ""))
        if r["r_flag"]:
            L.append(f"       {r['r_flag']}")
    L += ["", "纪律提醒：持有=买入（空仓持现金今天会以现价买吗？）；成本价/浮盈亏不进决策；",
          "          涨多了不是卖出理由（看论文不看价）；估值信号≠下单，买入须过三层全绿。",
          "", DISCLAIMER]
    return "\n".join(L)

def render_html(today, r_now, rows, alerts, macro, addon):
    def sig_color(s):
        return {"🟢🟢🟢":"#137333","🟢🟢":"#188038","🟡":"#b06000",
                "🔴":"#c5221f","⚪":"#5f6368","⛔":"#c5221f"}.get(s, "#202124")
    reg = macro.get("regime_note", "未设")
    macro_txt = (f"宏观：{reg}" + (f"　→　安全边际+{addon*100:.0f}pp、仓位≤{macro.get('position_cap_pct')}%"
                 if addon else "　→　无叠加"))
    h = [f"<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:760px'>"]
    h.append(f"<h2 style='margin:0'>持仓估值日报 · {today}</h2>")
    h.append(f"<div style='color:#5f6368;font-size:13px'>10Y 国债={f'{r_now:.2%}' if r_now else 'n/a'}　·　{macro_txt}　·　对应深研报告 §4 估值 + §5 买入决策</div>")
    if alerts:
        h.append("<div style='background:#fef7e0;border:1px solid #f9e3a0;border-radius:8px;padding:10px 12px;margin:12px 0'>"
                 "<b>今日可执行提醒</b><ul style='margin:6px 0 0 0;padding-left:18px'>"
                 + "".join(f"<li>{a}</li>" for a in alerts) + "</ul></div>")
    h.append("<table style='border-collapse:collapse;width:100%;font-size:14px'>"
             "<tr style='background:#f1f3f4'><th style='text-align:left;padding:6px'>代码</th>"
             "<th style='text-align:right;padding:6px'>现价</th>"
             "<th style='text-align:right;padding:6px'>安全边际</th>"
             "<th style='text-align:right;padding:6px'>IV 保/基/乐</th>"
             "<th style='text-align:left;padding:6px'>IV来源</th>"
             "<th style='text-align:left;padding:6px'>判定</th></tr>")
    for r in rows:
        if r.get("error"):
            h.append(f"<tr><td style='padding:6px'>{r['ticker']}</td><td colspan=5 style='padding:6px;color:#c5221f'>取价失败</td></tr>")
            continue
        c = sig_color(r["sig"])
        extra = f"<br><span style='color:#b06000;font-size:12px'>{r['r_flag']}</span>" if r["r_flag"] else ""
        buy = f"<br><span style='color:#5f6368;font-size:12px'>买入区 ${r['buy_lo']:.0f}–{r['buy_hi']:.0f}</span>" if r['buy_lo'] else ""
        src_color = "#137333" if r['iv_source'] == "手动深研" else "#1a73e8"
        h.append(f"<tr style='border-top:1px solid #e8eaed'>"
                 f"<td style='padding:6px;font-weight:600'>{r['ticker']}</td>"
                 f"<td style='padding:6px;text-align:right'>${r['price']:.2f}</td>"
                 f"<td style='padding:6px;text-align:right;color:{c}'>{r['mos']:+.0f}%</td>"
                 f"<td style='padding:6px;text-align:right'>{r['ivc']:.0f}/{r['ivb']:.0f}/{r['ivo']:.0f}</td>"
                 f"<td style='padding:6px;color:{src_color};font-size:12px'>{r['iv_source']}</td>"
                 f"<td style='padding:6px;color:{c}'>{r['sig']} {r['txt']}{buy}{extra}</td></tr>")
    h.append("</table>")
    h.append("<div style='color:#5f6368;font-size:12px;margin-top:14px'>"
             "纪律：持有=买入（空仓持现金今天会以现价买吗？）；成本价/浮盈亏不进决策；"
             "涨多了不是卖出理由；<b>估值信号≠下单，买入须过三层全绿(宏观🟢行业🟢个股🟢)</b>。<br>"
             + DISCLAIMER + "</div></div>")
    return "".join(h)


def send_email(subject, html_body, text_body):
    user = os.environ.get("GMAIL_USER"); pw = os.environ.get("GMAIL_APP_PASSWORD")
    to   = os.environ.get("MAIL_TO", user)
    if not (user and pw):
        print("⚠️ 未设置 GMAIL_USER / GMAIL_APP_PASSWORD 环境变量，跳过发信。", file=sys.stderr)
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject; msg["From"] = user; msg["To"] = to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, [to], msg.as_string())
    print(f"✅ 已发送至 {to}")
    return True


def main():
    if not os.path.exists(CFG):
        print(f"缺少配置 {CFG}", file=sys.stderr); sys.exit(1)
    cfg = json.load(open(CFG, encoding="utf-8"))
    today, r_now, rows, alerts, macro, addon = analyze(cfg)
    text = render_text(today, r_now, rows, alerts, macro, addon)
    html = render_html(today, r_now, rows, alerts, macro, addon)
    print(text)
    if "--html" in sys.argv:
        path = sys.argv[sys.argv.index("--html") + 1]
        open(path, "w", encoding="utf-8").write(html)
        print(f"\n[HTML 已写 {path}]")
    if "--email" in sys.argv:
        subj = f"持仓估值日报 {today}" + (f"（{len(alerts)}条提醒）" if alerts else "")
        send_email(subj, html, text)


if __name__ == "__main__":
    main()
