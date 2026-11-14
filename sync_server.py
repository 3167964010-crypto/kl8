# -*- coding: utf-8 -*-
"""
快乐8 本地同步服务
- 提供网页静态文件
- /api/kl8      代理中国福彩官网开奖接口，绕开浏览器跨域限制
- /api/predict  AI 预测：每期自动结算历史预测、生成下一期预测、统计滚动命中率
"""

import os
import re
import ssl
import sys
import json
import time
import webbrowser
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

# 官方开奖查询接口
API = "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice"
BASE = os.path.dirname(os.path.abspath(__file__))   # 页面所在目录
PORT = 8767                                          # 默认端口
PRED_FILE = os.path.join(BASE, "predictions.json")   # 预测记录持久化文件
HIST_FILE = os.path.join(BASE, "history_backtest.json")  # 100 期预测-开奖对照缓存
UPICK_FILE = os.path.join(BASE, "user_picks.json")   # 用户自选号码定稿记录
LLM_CONF_FILE = os.path.join(BASE, "llm_config.json")  # 大模型 API 配置文件（页面可编辑）


def load_llm_conf():
    """读取大模型配置：优先本地配置文件，其次环境变量。返回 dict 或 None"""
    if os.path.exists(LLM_CONF_FILE):
        try:
            with open(LLM_CONF_FILE, "r", encoding="utf-8") as f:
                conf = json.load(f)
            if conf.get("url") and conf.get("key"):
                return conf
        except Exception:
            pass
    url = os.environ.get("KL8_LLM_URL")
    key = os.environ.get("KL8_LLM_KEY")
    if url and key:
        return {"url": url, "key": key,
                "model": os.environ.get("KL8_LLM_MODEL", "deepseek-chat")}
    return None


# ================= 官方数据 =================

def fetch_draws(count=50):
    """请求官方开奖接口，返回解析后的 dict（失败抛异常）"""
    url = "%s?name=kl8&issueCount=%d&pageNo=1&pageSize=%d&systemType=PC" % (API, count, count)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Referer": "https://www.cwl.gov.cn/ygkj/wqkjgg/kl8/",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    ctx = ssl._create_unverified_context()  # 忽略证书链校验，避免本地环境报错
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_rows(data):
    """把官方返回按期号升序整理为 [{code, date, nums:set}]"""
    rows = []
    for it in data.get("result", []):
        rows.append({
            "code": it["code"],
            "date": it.get("date", "").split("(")[0].strip(),
            "nums": set(int(x) for x in it["red"].split(",")),
        })
    rows.sort(key=lambda r: r["code"])
    return rows


# 新浪走势图页面（更新比福彩官网快，作为兜底数据源）
SINA_URL = ("https://lotto.sina.cn/trend/qxc_qlc_proxy.d.html"
            "?lottoType=kl8&actionType=chzs&type=%d")


def fetch_draws_sina(count=100):
    """解析新浪走势图页面，返回 [{code, date, nums:set}]；失败返回空列表"""
    req = urllib.request.Request(SINA_URL % count, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Referer": "https://lotto.sina.cn/",
    })
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        html = resp.read().decode("utf-8", "ignore")
    rows = []
    # 每期数据形如 <div class="issue"><span class="issue-no">2026291</span>
    #   <span class="time">2026-11-01 21:30:00</span><span class="red-ball">03</span>…
    for blk in re.split(r'<div class="issue">', html)[1:]:
        m = re.search(r'<span class="issue-no">(\d{7})</span>', blk)
        if not m:
            continue
        t = re.search(r'<span class="time">([^<]*)</span>', blk)
        nums = [int(x) for x in re.findall(r'<span class="red-ball">(\d{2})</span>', blk)]
        if len(nums) != 20:
            continue
        rows.append({
            "code": m.group(1),
            "date": (t.group(1)[:10] if t else ""),
            "nums": set(nums),
        })
    rows.sort(key=lambda r: r["code"])
    return rows


def fetch_rows(count=100):
    """
    合并数据源：以福彩官网为准，官网尚未发布的新期用新浪走势图补齐。
    返回按期号升序的最近 count 期。

    多取 12 期再截断：两个数据源更新节奏不同（官网常比新浪晚一期），
    各自只取 count 期时合并后可能凑不满 count 期，故先多要一些再取尾部。
    """
    need = count + 12
    official = []
    try:
        official = parse_rows(fetch_draws(need))
    except Exception:
        try:
            official = parse_rows(fetch_draws(count))   # 多取失败时退回原期数
        except Exception:
            pass
    sina = []
    try:
        # 新浪走势图只支持 30/50/100 等固定档位，传其它值会返回空，故固定取 100 期
        sina = fetch_draws_sina(100)
    except Exception:
        pass
    merged = {r["code"]: r for r in official}
    for r in sina:
        if r["code"] not in merged:      # 只补官网缺失的期，同期以官网数据为准
            merged[r["code"]] = r
    out = sorted(merged.values(), key=lambda r: r["code"])
    return out[-count:]


# ================= 预测模型 =================

def llm_pick10(rows):
    """
    大模型推测入口（可选）：在页面"AI 预测"卡片中配置兼容 OpenAI 的接口后启用，
    配置保存在 llm_config.json（也可用环境变量 KL8_LLM_URL / KL8_LLM_KEY）。
    返回按推荐度排序的号码列表（最多 10 个，供选一至选十取前 n 个），
    未配置或调用失败时返回 None，自动回退到本地统计模型。
    """
    conf = load_llm_conf()
    if not conf:
        return None
    url = conf["url"]
    key = conf["key"]
    model = conf.get("model") or "deepseek-chat"
    # 把最近 100 期开奖号码作为上下文交给模型（期数按实际数据动态标注，可验证）
    ctx_rows = rows[-100:]
    hist = "\n".join("%s: %s" % (r["code"], " ".join("%02d" % v for v in sorted(r["nums"])))
                     for r in ctx_rows)
    prompt = ("你是彩票数据分析助手。以下是快乐8最近%d期开奖号码（每期20个，范围01-80）：\n%s\n"
              "请基于冷热、遗漏、奇偶、大小、分区等统计特征，给出下一期你认为最值得关注的10个号码，"
              "按推荐度从高到低排序，用逗号分隔（如 09,73,31），只回答数字不要解释。"
              % (len(ctx_rows), hist))
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
    })
    try:
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=40, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        nums = []
        # \d{1,2} 才能吃到「07」这类带前导零的号码，避免 01-09 被漏掉
        for x in re.findall(r"\d{1,2}", text):
            v = int(x)
            if 1 <= v <= 80 and v not in nums:
                nums.append(v)
        if nums:
            return nums[:10]
    except Exception:
        pass
    return None


def score_all(rows):
    """
    本地统计模型：冷热 / 遗漏 / 近10期活跃 / 连出 / 分区轮动 五维打分，
    对 80 个号码打分并按分数降序返回完整列表。
    """
    n = len(rows)
    last = rows[-1]["nums"]
    # 统计 80 个号码的各项指标
    items = []
    for k in range(1, 81):
        count = conn = miss = recent = 0
        prev = False
        for i, r in enumerate(rows):
            hit = k in r["nums"]
            if hit:
                count += 1
                if prev:
                    conn += 1
            prev = hit
        for r in reversed(rows):
            if k in r["nums"]:
                break
            miss += 1
        for r in rows[-10:]:
            if k in r["nums"]:
                recent += 1
        items.append({"num": k, "count": count, "rate": count / n,
                      "conn": conn, "miss": miss, "recent": recent})
    # 归一化所需的最值
    r_min = min(x["rate"] for x in items)
    r_max = max(x["rate"] for x in items)
    m_max = max(x["miss"] for x in items)
    rec_max = max(x["recent"] for x in items)
    c_max = max(x["conn"] for x in items)
    # 分区轮动：最近一期各区出号数与期望（每区5个）比较，偏多区略降权
    zone_cnt = [0, 0, 0, 0]
    for v in last:
        zone_cnt[(v - 1) // 20] += 1
    for x in items:
        zone = (x["num"] - 1) // 20
        zone_adj = (5 - zone_cnt[zone]) / 5.0                    # [-1,1]
        hot = (x["rate"] - r_min) / (r_max - r_min) if r_max > r_min else 0.5
        x["score"] = round(
            hot * 45 +                                           # 冷热 45%
            (x["miss"] / m_max if m_max else 0) * 20 +           # 遗漏 20%
            (x["recent"] / rec_max if rec_max else 0) * 20 +     # 近期活跃 20%
            (x["conn"] / c_max if c_max else 0) * 10 +           # 连出 10%
            (zone_adj + 1) / 2 * 5                               # 分区轮动 5%
        )
    items.sort(key=lambda x: x["score"], reverse=True)
    return items


def ranked_numbers(rows):
    """返回 80 个号码按综合评分降序的号码列表"""
    return [x["num"] for x in score_all(rows)]


def model_pick(rows):
    """本地统计模型：选出综合分最高的号码。返回 (号码, 评分, 逻辑说明)"""
    items = score_all(rows)
    top = items[0]
    n = len(rows)
    logic = ("综合评分 %d/100：近 %d 期出现 %d 次（%.1f%%）、近 10 期出现 %d 次、"
             "当前遗漏 %d 期、历史连出 %d 次" % (
                 top["score"], n, top["count"], top["rate"] * 100,
                 top["recent"], top["miss"], top["conn"]))
    return top["num"], top["score"], logic


# ================= 十种玩法：中奖判定与概率 =================

def prize_hit(n, k):
    """快乐8官方规则：选 n 个号码命中 k 个是否有返奖（含选八/九/十全不中返 2 元）"""
    if n == 1:
        return k >= 1
    if n == 2:
        return k >= 2
    if n in (3, 4):
        return k >= 2
    if n in (5, 6):
        return k >= 3
    if n in (7, 8, 9):
        return k >= 4 or k == 0      # 选七/八/九：中 4 个及以上有奖，全不中也返 2 元
    return k >= 5 or k == 0            # 选十


def theo_prize_prob(n):
    """选 n 玩法的理论中奖概率：C(20,k)*C(60,n-k)/C(80,n) 对所有有奖 k 求和"""
    from math import comb
    total = comb(80, n)
    hit = sum(comb(20, k) * comb(60, n - k)
              for k in range(0, n + 1) if prize_hit(n, k))
    return hit / total


# 快乐8官方奖金表（每注 2 元）：选 n 玩法命中 k 个对应的奖金（元）
PRIZE_TABLE = {
    1:  {1: 4.6},
    2:  {2: 19},
    3:  {3: 53, 2: 3},
    4:  {4: 100, 3: 5, 2: 3},
    5:  {5: 1000, 4: 21, 3: 3},
    6:  {6: 3000, 5: 30, 4: 10, 3: 3},
    7:  {7: 10000, 6: 288, 5: 28, 4: 4, 0: 2},
    8:  {8: 50000, 7: 800, 6: 88, 5: 10, 4: 3, 0: 2},
    9:  {9: 300000, 8: 2000, 7: 200, 6: 20, 5: 5, 4: 3, 0: 2},
    10: {10: 5000000, 9: 8000, 8: 800, 7: 80, 6: 5, 5: 3, 0: 2},
}


def prize_amount(n, k):
    """选 n 玩法命中 k 个的单注奖金（元），无奖返回 0"""
    return PRIZE_TABLE.get(n, {}).get(k, 0)


def play_bets(ranked, n, count=None):
    """
    为某玩法生成 count 注号码：严格按推荐度排序取号，第 i 注 = 第 i~i+n-1 名。
    也就是每一注都落在推荐最靠前的号段里，相邻注顺延一位（不是另外拼一组低排名号码）。
    """
    if not count:
        count = BETS_PER_PLAY
    pool = list(ranked)
    if len(pool) < n + count - 1:          # 排序号码不足时用全号池补齐
        pool += [x for x in range(1, 81) if x not in pool]
    return [pool[i:i + n] for i in range(count)]


def algo_order(rows, aid, stat, picks10):
    """
    把某套算法的前 10 推荐扩展成完整排序，供"每玩法 10 注"取号使用。
    前 10 保持该算法当期推荐，其余号码按同一套算法评分补齐。
    """
    if aid == "ai":
        base = ranked_numbers(rows)
    elif aid in (CONSENSUS_ID, OVERLAP_ID):
        # 这两个算法本身就是汇总结果，扩展排序时用自身推荐 + 本地评分
        base = list(picks10) + ranked_numbers(rows)
    else:
        base = algo_rank(rows, aid, stat)
    order = list(picks10)
    for x in base:
        if x not in order:
            order.append(x)
    return order


def backtest10(rows, window=30):
    """
    十种玩法滚动回测：对最近 window 期逐期模拟（只用该期之前的数据打分取排序），
    统计每种玩法的平均命中数、单注回测中奖率，以及"10 注玩法"的注级命中率。
    """
    start = max(20, len(rows) - window)          # 至少保留 20 期历史用于打分
    agg = {n: {"hits": 0, "prize": 0, "cnt": 0, "bet_win": 0, "bet_cnt": 0, "tiers": {}}
           for n in range(1, 11)}
    for i in range(start, len(rows)):
        ranked = ranked_numbers(rows[:i])        # 该期之前的数据生成的排序
        actual = rows[i]["nums"]
        for n in range(1, 11):
            k = len(set(ranked[:n]) & actual)
            a = agg[n]
            a["hits"] += k
            a["cnt"] += 1
            if prize_hit(n, k):
                a["prize"] += 1
            # 10 注玩法：逐注判断是否中奖，统计注级命中率与各中奖档位
            for bet in play_bets(ranked, n):
                a["bet_cnt"] += 1
                bk = len(set(bet) & actual)
                if prize_hit(n, bk):
                    a["bet_win"] += 1
                    a["tiers"][str(bk)] = a["tiers"].get(str(bk), 0) + 1
    out = []
    for n in range(1, 11):
        a = agg[n]
        out.append({
            "n": n,
            "avg_hit": round(a["hits"] / a["cnt"], 2) if a["cnt"] else 0,
            "prize_rate": round(a["prize"] / a["cnt"] * 100, 1) if a["cnt"] else 0,
            "bet_rate": round(a["bet_win"] / a["bet_cnt"] * 100, 1) if a["bet_cnt"] else 0,
            "bet_win": a["bet_win"],
            "bet_cnt": a["bet_cnt"],
            "tiers": a["tiers"],                 # 各中奖档位注数（含"全不中返 2 元"的 0 档）
            "window": len(rows) - start,         # 实际回测期数
            "theo_rate": round(theo_prize_prob(n) * 100, 2),
        })
    return out


# ================= 预测记录：结算与生成 =================

def load_predictions():
    """读取预测记录文件，不存在则返回空结构"""
    if os.path.exists(PRED_FILE):
        try:
            with open(PRED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"history": []}


def save_predictions(state):
    """写入预测记录文件"""
    with open(PRED_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def build_history(rows, count=100):
    """
    生成最近 count 期的「预测号码 + 开奖结果」对照，最新一期在前。
    每期只用该期之前的数据打分，绝不使用未来信息；结果带文件缓存，避免每次请求重算。
    若该期存在真实运行记录（如 AI 大模型），优先展示真实记录。
    """
    real = {str(r.get("target_issue")): r for r in load_predictions()["history"]}
    cache = {}
    if os.path.exists(HIST_FILE):
        try:
            with open(HIST_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    out = []
    # 下标从 1 开始：第一期没有更早的数据可用于打分，必须留出一期历史基线
    start = max(1, len(rows) - count)
    for i in range(start, len(rows)):
        code = rows[i]["code"]
        actual = rows[i]["nums"]
        if code in real and real[code].get("picks10"):
            picks, method = real[code]["picks10"], real[code].get("method", "真实记录")
        elif code in cache:
            picks, method = cache[code]["picks10"], cache[code]["method"]
        else:
            picks, method = ranked_numbers(rows[:i])[:10], "本地模型回测"
            cache[code] = {"picks10": picks, "method": method}
        out.append({
            "target_issue": code,
            "date": rows[i]["date"],
            "picks10": picks,
            "actual": sorted(actual),
            "hits10": {str(n): len(set(picks[:n]) & actual) for n in range(1, 11)},
            "hit": picks[0] in actual,
            "settled": True,
            "method": method,
        })
    try:
        with open(HIST_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass
    out.reverse()                     # 最新一期在最上面
    return out


def settle_and_predict(rows=None):
    """
    核心流程（每次调用自动执行）：
    1. 拉取最新开奖
    2. 结算所有到期但未结算的历史预测（滚动命中率依据）
    3. 若还没有针对"下一期"的预测，则生成一条新预测并保存
    返回给前端的完整状态。
    rows 可外部传入（定时任务里只抓一次，避免重复请求数据源）
    """
    if not rows:
        rows = fetch_rows(101)                        # 多取 1 期作为首期回测的基线
    if not rows:
        # 两个数据源都没拿到数据时给出可读的错误，避免下游 rows[-1] 抛 IndexError
        raise RuntimeError("拉取开奖数据失败：福彩官网与新浪走势图均未返回数据")
    model_rows = rows[-100:]                          # 模型统一只用最近 100 期，与页面标注一致
    drawn = {r["code"]: r["nums"] for r in rows}      # 期号 -> 号码集合
    last = rows[-1]
    next_issue = str(int(last["code"]) + 1)           # 快乐8期号连续递增

    state = load_predictions()
    changed = False
    pos = {r["code"]: i for i, r in enumerate(rows)}   # 期号 -> 在 rows 里的下标

    # 第 1 步：结算已到期的预测（含十种玩法各自命中数）
    for rec in state["history"]:
        if not rec.get("settled") and rec["target_issue"] in drawn:
            actual = drawn[rec["target_issue"]]
            rec["settled"] = True
            rec["hit"] = rec["pick"] in actual
            rec["actual"] = sorted(actual)            # 记录开奖号码，供"当期 10 注"统计
            if rec.get("picks10"):
                rec["hits10"] = {str(n): len(set(rec["picks10"][:n]) & actual)
                                 for n in range(1, 11)}
                # 用该期之前的数据复现当期 10 注，开奖后即可逐注结算
                idx = pos.get(rec["target_issue"])
                if idx is not None:
                    order = list(rec["picks10"]) + [x for x in ranked_numbers(rows[:idx])
                                                    if x not in rec["picks10"]]
                    rec["bets"] = {str(n): play_bets(order, n) for n in range(1, 11)}
            changed = True

    # 第 2 步：生成下一期预测（每期只生成一次）
    current = None
    for rec in state["history"]:
        if rec["target_issue"] == next_issue:
            current = rec
            break
    if current is None:
        nums10 = llm_pick10(rows)                     # 优先大模型（若已配置）
        if nums10:
            # 大模型前 10 推荐，不足 10 个用本地评分排序补齐
            local = ranked_numbers(model_rows)
            picks10 = (nums10 + [x for x in local if x not in nums10])[:10]
            num, method = picks10[0], "AI 大模型"
            logic = "由已配置的大模型接口生成（前 10 推荐，不足 10 个按本地评分补齐）"
        else:
            num, score, logic = model_pick(model_rows)   # 回退本地统计模型
            method = "本地统计模型（未配置大模型 API）"
            picks10 = ranked_numbers(model_rows)[:10]
        current = {
            "target_issue": next_issue,               # 预测的目标期号
            "ctx": min(100, len(rows)),               # 喂给模型的开奖数据期数（可验证）
            "pick": num,                              # 选一预测号码
            "picks10": picks10,                       # 评分前 10，选 n 玩法取前 n 个
            "logic": logic,                           # 预测逻辑说明
            "method": method,                         # 使用的模型
            "based_on": last["code"],                 # 基于哪期数据生成
            "settled": False,
            "hit": None,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        state["history"].append(current)
        changed = True
    elif (not current.get("settled")
          and str(current.get("method", "")).startswith("本地统计模型")
          and load_llm_conf() is not None):
        # 已配置大模型：把未结算的本地模型预测自动升级为大模型预测
        nums10 = llm_pick10(rows)
        if nums10:
            local = ranked_numbers(model_rows)
            picks10 = (nums10 + [x for x in local if x not in nums10])[:10]
            current["pick"] = picks10[0]
            current["picks10"] = picks10
            current["method"] = "AI 大模型"
            current["logic"] = "由已配置的大模型接口生成（已升级替换本地模型预测）"
            current["ctx"] = min(100, len(rows))      # 同步记录上下文期数
            current["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
            changed = True

    # 旧记录迁移：补充十玩法预测列表
    if current is not None and "picks10" not in current:
        current["picks10"] = ranked_numbers(model_rows)[:10]
        changed = True

    # 旧记录迁移：补充上下文期数字段（预测台数据始终为最近 100 期）
    for rec in state["history"]:
        if "ctx" not in rec:
            rec["ctx"] = min(100, len(rows))
            changed = True

    # 十种玩法各生成 10 注：模型前 10 推荐优先，其余按本地评分补足名次
    if current is not None:
        order = list(current.get("picks10") or [])
        order += [x for x in ranked_numbers(model_rows) if x not in order]
        if order:
            bets = {str(n): play_bets(order, n) for n in range(1, 11)}
            if current.get("bets") != bets:
                current["bets"] = bets
                changed = True

    # 迁移：已结算但缺"当期 10 注 / 开奖号码"的旧记录，用该期之前的数据补算
    for rec in state["history"]:
        if rec.get("settled") and rec.get("picks10") and (not rec.get("bets") or not rec.get("actual")):
            iss = rec["target_issue"]
            idx = pos.get(iss)
            if idx is not None and iss in drawn:
                rec["actual"] = sorted(drawn[iss])
                order = list(rec["picks10"]) + [x for x in ranked_numbers(rows[:idx])
                                                if x not in rec["picks10"]]
                rec["bets"] = {str(n): play_bets(order, n) for n in range(1, 11)}
                changed = True

    # 迁移：每玩法注数改为 5 后，旧的 10 注记录按该期之前的数据重建排序再取号
    for rec in state["history"]:
        if not rec.get("settled") or not rec.get("picks10"):
            continue
        if not bets_need_rebuild(rec.get("bets")):
            continue
        idx = pos.get(rec["target_issue"])
        if idx is None:
            continue
        before = rows[:idx]
        order = list(rec["picks10"]) + [x for x in ranked_numbers(before) if x not in rec["picks10"]]
        rec["bets"] = {str(n): play_bets(order, n) for n in range(1, 11)}
        changed = True

    # 最近一期已开奖的"当期 10 注"逐注结果（当期口径，卡片只展示这一期）
    last_play = None
    for rec in sorted((r for r in state["history"]
                       if r.get("settled") and r.get("bets") and r.get("actual")),
                      key=lambda r: r["target_issue"], reverse=True):
        act = set(rec["actual"])
        plays = {}
        for n in range(1, 11):
            bets = (rec.get("bets") or {}).get(str(n)) or []
            win, tiers = 0, {}
            for bet in bets:
                k = len(set(bet) & act)
                if prize_amount(n, k) > 0:
                    win += 1
                    tiers[str(k)] = tiers.get(str(k), 0) + 1
            plays[str(n)] = {"win": win, "cnt": len(bets), "tiers": tiers}
        last_play = {"issue": rec["target_issue"], "plays": plays}
        break

    if changed:
        save_predictions(state)

    # 第 3 步：滚动命中率 = 命中数 / 已结算数
    settled = [r for r in state["history"] if r.get("settled")]
    hits = sum(1 for r in settled if r["hit"])
    return {
        "state": 0,
        "current": current,                           # 当前（下一期）预测
        "last_play": last_play,                       # 最近一期的当期 10 注结果
        "history": list(reversed(state["history"])),  # 全部记录，最新在前
        "total": len(settled),                        # 已结算次数
        "hits": hits,                                 # 命中次数
        "rate": round(hits / len(settled) * 100, 2) if settled else None,
        "backtest10": backtest10(model_rows),         # 十种玩法回测与理论中奖概率
        "records": build_history(rows, 100),          # 最近 100 期：预测号码 + 实际开奖 + 命中
        "next_draw_time": (rows[-1]["date"] + " 21:30:00") if rows else "",  # 最近一期开奖时间
    }


# ================= 用户自选号码定稿追踪 =================

def load_upicks():
    """读取用户定稿记录"""
    if os.path.exists(UPICK_FILE):
        try:
            with open(UPICK_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"history": []}


def save_upicks(state):
    """写入用户定稿记录"""
    with open(UPICK_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def settle_upicks(rows):
    """结算用户定稿：开奖后自动计算命中数与滚动命中率（累计命中 / 累计选号）"""
    drawn = {r["code"]: r["nums"] for r in rows}
    next_issue = str(int(rows[-1]["code"]) + 1)
    st = load_upicks()
    changed = False
    for rec in st["history"]:
        if not rec.get("settled") and rec["target"] in drawn:
            actual = drawn[rec["target"]]
            rec["settled"] = True
            rec["hit_count"] = len(set(rec["nums"]) & actual)
            rec["hit_nums"] = sorted(set(rec["nums"]) & actual)
            changed = True
    if changed:
        save_upicks(st)
    current = None
    for rec in st["history"]:
        if rec["target"] == next_issue and not rec.get("settled"):
            current = rec
            break
    settled = [r for r in st["history"] if r.get("settled")]
    tn = sum(len(r["nums"]) for r in settled)
    th = sum(r.get("hit_count", 0) for r in settled)
    return {
        "state": 0,
        "next_issue": next_issue,
        "current": current,                                     # 已定稿、等待开奖的号码
        "history": list(reversed(st["history"])),
        "total": len(settled),                                  # 已结算期数
        "total_nums": tn, "total_hits": th,
        "rate": round(th / tn * 100, 2) if tn else None,        # 滚动命中率
    }


# ================= 多算法对比（7 套并行预测） =================

ALGO_FILE = os.path.join(BASE, "algo_predictions.json")   # 多算法预测记录

# 算法清单：id、名称、说明（"重复号码"依赖其余算法结果，必须排在最后）
ALGOS = [
    ("ai", "AI 大模型", "MiMo 大模型生成；未配置 API 时回退本地五维评分"),
    ("hot", "热号优先", "近 100 期出现次数最多的号码排前面（追热）"),
    ("miss", "遗漏回补", "当前遗漏最深的号码排前面（赌冷号回补）"),
    ("momentum", "近期动量", "近 10 期活跃度优先，追踪短周期趋势"),
    ("balanced", "均衡结构", "五维评分基础上做分区配额与奇偶均衡"),
    ("prev", "上期规律", "以上一期号码为基准：重号 4 个 + 邻号 3 个 + 同尾号 3 个，跟随最近一期结构"),
    ("random", "随机对照", "以期号为种子的固定随机序列，作为基准线"),
    ("consensus", "重复号码", "统计以上各套算法同一期共同推荐的号码，按被推荐次数（票数）排序"),
    ("overlap", "高频重合", "统计前八套算法十种玩法全部注（每玩法 2 注）中号码的出现次数，按出现次数从高到低排序"),
]

CONSENSUS_ID = "consensus"      # 重复号码算法的 id，需要特殊处理（汇总其他算法）
OVERLAP_ID = "overlap"          # 高频重合算法的 id，依赖前八套算法的全部投注号码
BETS_PER_PLAY = 2               # 每种玩法预测的注数（选一~选十各 2 注）
CFG_VERSION = "v4-2bets"        # 算法配置版本：升级时清空受影响的缓存结果并按新规则重算


def bets_need_rebuild(bets):
    """每玩法注数配置变化后，旧记录（曾为 10 注）需要按新注数重建"""
    if not bets:
        return True
    return any(len(bets.get(str(n)) or []) != BETS_PER_PLAY for n in range(1, 11))


def load_json(path, default):
    """通用 JSON 读取，失败返回默认值"""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    """通用 JSON 写入"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def algo_stats(rows):
    """汇总 80 个号码的基础指标，供各算法排序与 UI 标注使用"""
    nums80 = list(range(1, 81))
    stat = {v: {"count": 0, "recent10": 0, "recent5": 0, "miss": 0, "conn": 0}
            for v in nums80}
    for r in rows:
        for v in r["nums"]:
            stat[v]["count"] += 1
    for r in rows[-10:]:
        for v in r["nums"]:
            stat[v]["recent10"] += 1
    for r in rows[-5:]:
        for v in r["nums"]:
            stat[v]["recent5"] += 1
    for v in nums80:
        m = c = 0
        for r in reversed(rows):
            if v in r["nums"]:
                break
            m += 1
        stat[v]["miss"] = m          # 当前遗漏期数
        for r in reversed(rows):
            if v in r["nums"]:
                c += 1               # 当前连续开出期数
            else:
                break
        stat[v]["conn"] = c
    return stat


def prev_pattern(rows, stat=None):
    """
    上期规律分析：以上一期 20 个开奖号码为基准，按固定配额挑出 10 个候选。
    1. 重号 4 个：上期开出过的号码，按历史出现次数取最热的
    2. 邻号 3 个：上期号码 ±1 的相邻号，按近 10 期活跃度取前 3
    3. 同尾 3 个：尾数与上期相同的号码，按历史出现次数取前 3
    返回 (有序候选列表, 标签字典)，标签用于前端展示每个号码的入选理由。
    """
    if stat is None:
        stat = algo_stats(rows)
    last = rows[-1]["nums"]
    tails = {v % 10 for v in last}
    picks, tag = [], {}

    def add(v, t):
        """加入候选并打标签（去重，保持加入顺序即推荐顺序）"""
        if v not in tag:
            picks.append(v)
            tag[v] = t

    def count_of(t):
        """已入选的某类号码数量，用于控制配额"""
        return sum(1 for x in tag.values() if x == t)

    # 1) 重号：延续上期开出的号码，按历史热度排序
    for v in sorted(last, key=lambda x: -stat[x]["count"])[:4]:
        add(v, "重号")

    # 2) 邻号：上期号码的左右相邻号，按近 10 期活跃度排序
    nb = sorted({v for u in last for v in (u - 1, u + 1)
                 if 1 <= v <= 80 and v not in last}, key=lambda x: -stat[x]["recent10"])
    for v in nb:
        if count_of("邻号") >= 3:
            break
        add(v, "邻号")

    # 3) 同尾：尾数跟随上期，按历史出现次数排序
    tm = sorted((v for v in range(1, 81) if v % 10 in tails and v not in last),
                key=lambda x: -stat[x]["count"])
    for v in tm:
        if count_of("同尾") >= 3:
            break
        add(v, "同尾")

    return picks, tag


def algo_rank(rows, kind, stat=None):
    """各算法的 80 个号码推荐排序（降序推荐度），每套逻辑完全独立"""
    if stat is None:
        stat = algo_stats(rows)
    nums80 = list(range(1, 81))
    if kind == "hot":        # 热号优先：100 期总次数降序，同次数看近 10 期
        return sorted(nums80, key=lambda v: (-stat[v]["count"], -stat[v]["recent10"], v))
    if kind == "miss":       # 遗漏回补：当前遗漏深度降序
        return sorted(nums80, key=lambda v: (-stat[v]["miss"], v))
    if kind == "momentum":   # 近期动量：近5期×2 + 近10期 + 当前连出×2
        return sorted(nums80, key=lambda v: (-(stat[v]["recent5"] * 2
                                               + stat[v]["recent10"]
                                               + stat[v]["conn"] * 2), v))
    if kind == "balanced":   # 均衡结构：评分排序 + 单区最多 3 个 + 奇偶各不超过 6 个
        base = ranked_numbers(rows)
        picks, zone_cnt, odd_cnt = [], [0, 0, 0, 0], 0
        for v in base:
            if len(picks) >= 10:
                break
            z = (v - 1) // 20
            odd = v % 2 == 1
            if zone_cnt[z] >= 3 or (odd and odd_cnt >= 6) \
                    or (not odd and len(picks) - odd_cnt >= 6):
                continue
            picks.append(v)
            zone_cnt[z] += 1
            odd_cnt += 1 if odd else 0
        return picks + [v for v in base if v not in picks]
    if kind == "prev":       # 上期规律：重号 4 + 邻号 3 + 同尾 3
        cand, _ = prev_pattern(rows, stat)
        # 候选按配额顺序排前，其余号码按历史热度补齐，保证始终有 80 个
        rest = [v for v in sorted(nums80, key=lambda x: -stat[x]["count"]) if v not in cand]
        return cand + rest
    if kind == "random":     # 随机对照：以期号为种子，每期结果固定可复现
        import random
        lst = list(nums80)
        random.Random(int(rows[-1]["code"])).shuffle(lst)
        return lst
    return ranked_numbers(rows)


def algo_notes(rows, kind, picks10, stat=None):
    """各算法预测号码的独立标注（UI 展示用），与 picks10 一一对应"""
    if stat is None:
        stat = algo_stats(rows)
    if kind == "hot":
        return ["%d次" % stat[v]["count"] for v in picks10]
    if kind == "miss":
        return ["漏%d期" % stat[v]["miss"] for v in picks10]
    if kind == "momentum":
        return ["近10期%d次" % stat[v]["recent10"] for v in picks10]
    if kind == "balanced":
        smap = {x["num"]: x["score"] for x in score_all(rows)}
        zn = "一二三四"
        return ["%d分·%s区" % (smap.get(v, 0), zn[(v - 1) // 20]) for v in picks10]
    if kind == "prev":
        # 标注每个号码与上一期的关系：重号 / 邻号 / 同尾 / 补位
        _, tag = prev_pattern(rows, stat)
        return [tag.get(v, "补位") for v in picks10]
    if kind == "random":
        return ["随机"] * len(picks10)
    return ["#%d" % (i + 1) for i in range(len(picks10))]   # AI：大模型推荐顺位


def consensus_votes(st, issue):
    """
    统计某一期各套算法共同推荐的票数（不含"重复号码"自身）。
    返回 {号码: 被几套算法推荐}，用于票数排序与前端"重复数量"标注。
    """
    votes = {}
    for aid, _name, _desc in ALGOS:
        if aid == CONSENSUS_ID:
            continue
        for rec in (st["algos"].get(aid) or {}).get("history", []):
            if rec.get("target") == issue:
                for v in rec.get("picks10", []):
                    votes[v] = votes.get(v, 0) + 1
                break
    return votes


def consensus_history(st, box):
    """
    回填"重复号码"算法的往期记录：用其余算法同一期的推荐重新计票，
    这样中奖率一开始就有与其它算法相同的样本量（新记录交给结算流程统一结算）。
    """
    have = {r["target"] for r in box["history"]}
    issues = set()
    for aid, _name, _desc in ALGOS:
        if aid == CONSENSUS_ID:
            continue
        for rec in (st["algos"].get(aid) or {}).get("history", []):
            issues.add(rec["target"])
    added = False
    for iss in sorted(issues):
        if iss in have:
            continue
        votes = consensus_votes(st, iss)
        if not votes:
            continue
        order = sorted(votes, key=lambda v: (-votes[v], v))[:10]   # 票多优先，同票按号码升序
        box["history"].append({
            "target": iss, "picks10": order, "method": "重复号码",
            "notes": ["%d票" % votes[v] for v in order],
            "settled": False, "hit": None, "created": "回填",
        })
        added = True
    if added:
        # 统一按往期顺序排列，保证前端"新→旧"的展示顺序正确
        box["history"].sort(key=lambda r: r["target"])
    return added


def overlap_votes(st, issue):
    """
    统计某一期"前八套算法十种玩法全部注（每玩法 2 注）"里每个号码的出现次数：
    某号码在八套算法共 160 注里出现了几次，就记几票（出现次数越高越靠前）。
    只取算法清单前八套，跳过"高频重合"自身，避免自我放大。
    """
    votes = {}
    for aid, _name, _desc in ALGOS[:8]:
        if aid == OVERLAP_ID:
            continue
        for rec in (st["algos"].get(aid) or {}).get("history", []):
            if rec.get("target") != issue:
                continue
            bets = rec.get("bets") or {}
            # 选一~选十每个玩法各 5 注，逐注逐号累加出现次数
            for n in range(1, 11):
                for bet in (bets.get(str(n)) or []):
                    for v in bet:
                        votes[v] = votes.get(v, 0) + 1
            break
    return votes


def overlap_history(st, box):
    """
    回填"高频重合"算法往期记录：用前八套算法同一期的全部投注号码重新计票，
    使它的中奖率一开始就有与其它算法相同的样本量。
    """
    have = {r["target"] for r in box["history"]}
    issues = set()
    for aid, _name, _desc in ALGOS[:8]:
        if aid == OVERLAP_ID:
            continue
        for rec in (st["algos"].get(aid) or {}).get("history", []):
            # 要求该期已有"每玩法 2 注"的记录，否则无法统计号码出现次数
            if rec.get("picks10") and rec.get("bets"):
                issues.add(rec["target"])
    added = False
    for iss in sorted(issues):
        if iss in have:
            continue
        votes = overlap_votes(st, iss)
        if not votes:
            continue
        order = sorted(votes, key=lambda v: (-votes[v], v))[:10]   # 出现次数多优先，同次数按号码升序
        box["history"].append({
            "target": iss, "picks10": order, "method": "高频重合",
            "notes": ["%d次" % votes[v] for v in order],
            "settled": False, "hit": None, "created": "回填",
        })
        added = True
    if added:
        box["history"].sort(key=lambda r: r["target"])
    return added


def settle_algos(rows):
    """
    9 套算法并行：结算各套到期的历史预测（含选一命中与十种玩法命中），
    并为还没有下一期预测的算法各生成一条。返回每套的当前预测与滚动命中率。
    其中"重复号码"是汇总其余算法的投票结果，故排在最后处理。
    """
    if not rows:
        # 数据源全挂时直接报错，避免 rows[-1] 抛 IndexError 让定时任务红屏
        raise RuntimeError("拉取开奖数据失败：福彩官网与新浪走势图均未返回数据")
    drawn = {r["code"]: r["nums"] for r in rows}
    pos = {r["code"]: i for i, r in enumerate(rows)}   # 期号 → 在 rows 中的位置（取"开奖前"历史用）
    next_issue = str(int(rows[-1]["code"]) + 1)
    st = load_json(ALGO_FILE, {"algos": {}})
    changed = False
    # 配置升级（每玩法 2 注 / 高频重合改为统计八套算法的全部投注号码）：旧的汇总结果作废，按新规则重算
    if st.get("cfg_version") != CFG_VERSION:
        st["algos"].pop(OVERLAP_ID, None)
        st["cfg_version"] = CFG_VERSION
        changed = True
    llm_cache = {"picks": None, "tried": False}     # 大模型每次请求最多调用一次
    out = []
    for aid, name, desc in ALGOS:
        box = st["algos"].setdefault(aid, {"history": []})
        # 重复号码：先用其余算法的往期推荐回填历史记录（随后由第 1 步统一结算）
        if aid == CONSENSUS_ID and consensus_history(st, box):
            changed = True
        # 高频重合：用其余算法"每玩法 2 注"的往期记录重新计票回填
        if aid == OVERLAP_ID and overlap_history(st, box):
            changed = True
        # 第 1 步：结算该套算法已到期的预测
        for rec in box["history"]:
            if rec["target"] in drawn:
                actual = drawn[rec["target"]]
                if "actual" not in rec:
                    rec["actual"] = sorted(actual)      # 记录实际开奖号码，供前端对照展示
                    changed = True
                # 每玩法 10 注：只用以该期之前的数据重算排序再滑窗取号，
                # 方便开奖后在本期记录上展示每个玩法的中奖号码
                if bets_need_rebuild(rec.get("bets")):
                    before = rows[:pos.get(rec["target"], len(rows))]
                    if len(before) < 20:
                        before = rows[:20]
                    order = algo_order(before, aid, algo_stats(before), rec["picks10"])
                    rec["bets"] = {str(n): play_bets(order, n) for n in range(1, 11)}
                    changed = True
                if not rec.get("settled"):
                    rec["settled"] = True
                    rec["hit"] = rec["picks10"][0] in actual
                    rec["hits10"] = {str(n): len(set(rec["picks10"][:n]) & actual)
                                     for n in range(1, 11)}
                    # 十种玩法各自奖金与单期总奖金（每种玩法 1 注 2 元）
                    rec["prize10"] = {str(n): prize_amount(n, rec["hits10"][str(n)])
                                      for n in range(1, 11)}
                    rec["prize_total"] = round(sum(rec["prize10"].values()), 2)
                    changed = True
        # 第 2 步：生成该套算法的下一期预测（每期只生成一次）
        cur = None
        for rec in box["history"]:
            if rec["target"] == next_issue:
                cur = rec
                break
        if cur is None:
            notes = None          # 留空则按各算法逻辑自动生成标注；特殊算法可自行覆盖
            if aid == "ai":
                # 优先复用主预测（predictions.json）中对同一期的推荐：
                # 保证与主页"AI 预测"一致，且避免重复调用大模型导致接口超时
                reuse = None
                for rec in load_predictions().get("history", []):
                    if rec["target_issue"] == next_issue and rec.get("picks10"):
                        reuse = rec
                        break
                if reuse:
                    picks10 = reuse["picks10"][:10]
                    m = reuse.get("method", "AI 大模型")
                else:
                    if not llm_cache["tried"]:
                        llm_cache["picks"] = llm_pick10(rows)
                        llm_cache["tried"] = True
                    if llm_cache["picks"]:
                        local = ranked_numbers(rows)
                        picks10 = (llm_cache["picks"]
                                   + [x for x in local if x not in llm_cache["picks"]])[:10]
                        m = "AI 大模型"
                    else:
                        picks10 = ranked_numbers(rows)[:10]
                        m = "本地五维（未配置大模型）"
            elif aid == CONSENSUS_ID:
                # 重复号码：汇总其余算法对同一期的前 10 推荐，按票数取前 10
                votes = consensus_votes(st, next_issue)
                order = sorted(votes, key=lambda v: (-votes[v], v))
                picks10 = order[:10]
                if len(picks10) < 10:      # 票数不足 10 个时，用本地评分补齐
                    picks10 += [v for v in ranked_numbers(rows)
                                if v not in picks10][:10 - len(picks10)]
                m = name
                notes = ["%d票" % votes.get(v, 0) for v in picks10]
            elif aid == OVERLAP_ID:
                # 高频重合：统计其余算法十种玩法全部注的号码出现次数，按重合度取前 10
                votes = overlap_votes(st, next_issue)
                order = sorted(votes, key=lambda v: (-votes[v], v))
                picks10 = order[:10]
                if len(picks10) < 10:      # 出现次数不足 10 个时，用本地评分补齐
                    picks10 += [v for v in ranked_numbers(rows)
                                if v not in picks10][:10 - len(picks10)]
                m = name
                notes = ["%d次" % votes.get(v, 0) for v in picks10]
            else:
                stat = algo_stats(rows)
                picks10 = algo_rank(rows, aid, stat)[:10]
                m = name
            # 每个号码附该算法独立逻辑的标注，供前端 UI 展示
            cur = {"target": next_issue, "picks10": picks10, "method": m,
                   "notes": notes if notes else algo_notes(rows, aid, picks10),
                   "settled": False, "hit": None,
                   "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            box["history"].append(cur)
            changed = True
        # 当前预测也要有"每玩法 10 注"：前 10 用本算法推荐，其余号码按同算法评分补齐
        if cur is not None and bets_need_rebuild(cur.get("bets")):
            order = algo_order(rows, aid, algo_stats(rows), cur.get("picks10") or [])
            cur["bets"] = {str(n): play_bets(order, n) for n in range(1, 11)}
            changed = True
        settled = [r for r in box["history"] if r.get("settled")]
        hits = sum(1 for r in settled if r.get("hit"))
        # 各玩法 10 注的注级命中率：命中注数 / 总注数（按已结算期统计）
        play_rates = {}
        for n in range(1, 11):
            win = cnt = 0
            tiers = {}                                  # 各中奖档位（中 k 个）对应的注数
            for r in settled:
                act = drawn.get(r["target"]) or set(r.get("actual") or [])
                for bet in ((r.get("bets") or {}).get(str(n)) or []):
                    cnt += 1
                    k = len(set(bet) & act)
                    if prize_amount(n, k) > 0:
                        win += 1
                        tiers[str(k)] = tiers.get(str(k), 0) + 1
            play_rates[str(n)] = {"win": win, "cnt": cnt, "tiers": tiers,
                                  "rate": round(win / cnt * 100, 1) if cnt else None}
        cost = len(settled) * 20                            # 每期 10 种玩法各 1 注 = 20 元
        prize = round(sum(r.get("prize_total", 0) for r in settled), 2)
        out.append({
            "id": aid, "name": name, "desc": desc, "current": cur,
            "total": len(settled), "hits": hits,
            "rate": round(hits / len(settled) * 100, 2) if settled else None,
            "cost": cost, "prize": prize,
            "net": round(prize - cost, 2),                  # 累计盈亏
            "play_rates": play_rates,                       # 每玩法 10 注的命中率
            "history": list(reversed(box["history"])),
        })
    if changed:
        save_json(ALGO_FILE, st)
    # 附带官方奖金表，前端展示每种玩法的中奖数与金额对照
    return {"state": 0, "algos": out, "next_issue": next_issue,
            "prize_table": {str(n): {str(k): v for k, v in PRIZE_TABLE[n].items()}
                            for n in PRIZE_TABLE}}


# ================= HTTP 服务 =================

class Handler(SimpleHTTPRequestHandler):
    """静态文件 + /api/kl8 + /api/predict"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE, **kwargs)  # 指定根目录

    def do_GET(self):
        if self.path.startswith("/api/kl8"):
            self.handle_api()
        elif self.path.startswith("/api/predict"):
            self.handle_predict()
        elif self.path.startswith("/api/upick"):
            self.handle_upick()
        elif self.path.startswith("/api/algos"):
            self.handle_algos()
        elif self.path.startswith("/api/llm_config"):
            self.handle_llm_status()
        else:
            super().do_GET()

    def do_POST(self):
        """页面表单提交：保存配置 / 测试连接"""
        if self.path.startswith("/api/llm_config"):
            self.handle_llm_save()
        elif self.path.startswith("/api/llm_test"):
            self.handle_llm_test()
        elif self.path.startswith("/api/upick"):
            self.handle_upick_save()
        elif self.path.startswith("/api/ai_learn"):
            self.handle_ai_learn()
        else:
            self._send_json({"state": -1, "message": "未知接口"}, 404)

    def _read_body(self):
        """读取 POST 请求体并解析为 dict"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def handle_algos(self):
        """返回 6 套算法的并行预测状态（含自动结算）"""
        try:
            rows = fetch_rows(100)
            self._send_json(settle_algos(rows))
        except Exception as exc:
            self._send_json({"state": -1, "message": "获取失败：%s" % exc}, 502)

    def handle_upick(self):
        """返回用户定稿追踪状态（含自动结算）"""
        try:
            rows = fetch_rows(100)
            self._send_json(settle_upicks(rows))
        except Exception as exc:
            self._send_json({"state": -1, "message": "获取失败：%s" % exc}, 502)

    def handle_upick_save(self):
        """保存用户定稿号码为下一期预测（同一目标期重复定稿则覆盖）"""
        body = self._read_body()
        raw = body.get("nums") or []
        try:
            nums = sorted({int(x) for x in raw if 1 <= int(x) <= 80})
        except Exception:
            nums = []
        if not (1 <= len(nums) <= 20):
            self._send_json({"state": -1, "message": "请选择 1—20 个号码（01—80）"}, 400)
            return
        try:
            rows = fetch_rows(100)
        except Exception as exc:
            self._send_json({"state": -1, "message": "获取期号失败：%s" % exc}, 502)
            return
        target = str(int(rows[-1]["code"]) + 1)
        st = load_upicks()
        # 同一目标期只允许一份定稿，重复提交则覆盖旧记录
        st["history"] = [r for r in st["history"]
                         if not (r["target"] == target and not r.get("settled"))]
        st["history"].append({
            "target": target, "nums": nums,
            "settled": False, "hit_count": None,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        save_upicks(st)
        self._send_json({"state": 0, "message": "已定稿", "target": target})

    def handle_llm_status(self):
        """返回当前大模型配置状态（密钥脱敏）"""
        conf = load_llm_conf()
        if conf:
            key = conf["key"]
            masked = ("****" + key[-4:]) if len(key) > 4 else "****"
            self._send_json({"state": 0, "configured": True,
                             "url": conf["url"], "model": conf.get("model", ""),
                             "key_masked": masked})
        else:
            self._send_json({"state": 0, "configured": False})

    def handle_llm_save(self):
        """保存页面提交的大模型配置到 llm_config.json"""
        body = self._read_body()
        url = str(body.get("url", "")).strip()
        key = str(body.get("key", "")).strip()
        model = str(body.get("model", "")).strip() or "deepseek-chat"
        if not url or not key:
            self._send_json({"state": -1, "message": "接口地址和密钥不能为空"}, 400)
            return
        with open(LLM_CONF_FILE, "w", encoding="utf-8") as f:
            json.dump({"url": url, "key": key, "model": model}, f,
                      ensure_ascii=False, indent=2)
        self._send_json({"state": 0, "message": "配置已保存"})

    def handle_llm_test(self):
        """用提交的（或已保存的）配置实际调用一次大模型，验证连通性"""
        body = self._read_body()
        conf = None
        if body.get("url") and body.get("key"):
            conf = {"url": str(body["url"]).strip(), "key": str(body["key"]).strip(),
                    "model": str(body.get("model", "")).strip() or "deepseek-chat"}
        else:
            conf = load_llm_conf()
        if not conf:
            self._send_json({"state": -1, "message": "尚未配置大模型 API"}, 400)
            return
        payload = json.dumps({
            "model": conf["model"],
            "messages": [{"role": "user", "content": "只回复数字7"}],
            "temperature": 0,
        }).encode("utf-8")
        req = urllib.request.Request(conf["url"], data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + conf["key"],
        })
        try:
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            reply = data["choices"][0]["message"]["content"].strip()
            self._send_json({"state": 0, "message": "连接成功，模型回复：%s" % reply[:50]})
        except urllib.error.HTTPError as e:
            hint = {401: "（密钥无效）", 402: "（账户欠费）",
                    404: "（接口地址或模型名错误）", 429: "（请求过频）"}.get(e.code, "")
            self._send_json({"state": -1, "message": "HTTP %d %s" % (e.code, hint)}, 200)
        except Exception as exc:
            self._send_json({"state": -1, "message": "连接失败：%s" % exc}, 200)

    def handle_ai_learn(self):
        """AI 自学习：把各算法近 20 期命中率 + 近 50 期开奖交给大模型，让它给出下一期 10 个号"""
        conf = load_llm_conf()
        if not conf:
            self._send_json({"ok": False,
                             "message": "尚未配置大模型（llm_config.json 或环境变量 KL8_LLM_URL / KL8_LLM_KEY）"})
            return
        body = self._read_body()
        weights = body.get("weights") or []
        draws = body.get("draws") or []
        # 上下文一：各算法近 20 期推荐 10 号的命中率（越高说明该逻辑近期越准）
        wtxt = "\n".join("%s：%.1f%%" % (str(w.get("name", ""))[:20],
                                         float(w.get("rate", 0)) * 100)
                         for w in weights[:30])
        # 上下文二：最近 50 期开奖号码
        recent = draws[-50:]
        dtxt = "\n".join("%s: %s" % (str(d.get("code", "")),
                                     " ".join("%02d" % int(x) for x in d.get("nums", [])))
                         for d in recent)
        prompt = (
            "你是快乐8（80 选 20）数据分析助手。\n"
            "下面是 18 套统计算法在最近 20 期的推荐命中率，数值越高说明该逻辑近期越准：\n%s\n\n"
            "最近 %d 期开奖号码：\n%s\n\n"
            "请结合这些命中率权重以及号码冷热、遗漏、区间分布，给出下一期最值得关注的 10 个号码"
            "（01-80、不重复），按推荐度从高到低排序，用逗号分隔。只回答数字，不要解释。"
            % (wtxt, len(recent), dtxt))
        payload = json.dumps({
            "model": conf["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }).encode("utf-8")
        req = urllib.request.Request(conf["url"], data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + conf["key"],
        })
        try:
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"]
        except Exception as exc:
            self._send_json({"ok": False, "message": "调用失败：%s" % exc})
            return
        # 只按出现顺序取 1-80 内的数字，去重后取前 10 个
        # 用 \d{1,2} 而不是带 \b 的写法：否则「07」这类带前导零的号码会被漏掉
        nums = []
        for x in re.findall(r"\d{1,2}", text):
            v = int(x)
            if 1 <= v <= 80 and v not in nums:
                nums.append(v)
        if len(nums) < 10:
            self._send_json({"ok": False, "message": "模型返回号码不足：%s" % text[:60]})
            return
        self._send_json({"ok": True, "nums": nums[:10], "model": conf["model"]})

    def _send_json(self, obj, code=200):
        """统一 JSON 响应"""
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_api(self):
        """返回合并后的开奖数据（官网优先，官网未发布的新期由新浪补齐）"""
        count = 50
        if "count=" in self.path:
            try:
                # 上限放到 500：算法验证页要回测 300 期，需要更多历史
                count = max(1, min(500, int(self.path.split("count=")[1].split("&")[0])))
            except Exception:
                count = 50
        try:
            rows = fetch_rows(count)
            if not rows:
                raise ValueError("无开奖数据")
            # 输出与官方接口一致的 result 结构，前端解析逻辑无需改动
            self._send_json({
                "state": 0, "message": "查询成功", "total": len(rows),
                "result": [{
                    "code": r["code"],
                    "date": r["date"],
                    "red": ",".join("%02d" % v for v in sorted(r["nums"])),
                } for r in reversed(rows)],
            })
        except Exception as exc:
            self._send_json({"state": -1, "message": "获取失败：%s" % exc}, 502)

    def handle_predict(self):
        """AI 预测：结算 + 生成 + 返回滚动命中率"""
        try:
            self._send_json(settle_and_predict())
        except Exception as exc:
            self._send_json({"state": -1, "message": "预测服务异常：%s" % exc}, 502)

    def end_headers(self):
        """静态页面禁用缓存，避免改版后浏览器仍显示旧内容"""
        if not self.path.startswith("/api"):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):
        """简化控制台输出"""
        pass


def lan_ips():
    """列出本机局域网 IP，便于手机在同一 Wi-Fi 下访问"""
    import socket
    ips = set()
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    # 兜底：通过本机网卡探测（无外网时会失败，忽略即可）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(ips)


def main():
    server = None
    port = PORT
    # 端口被占用时自动顺延
    for p in range(PORT, PORT + 10):
        try:
            # 仅监听本机（手机访问需另行走小程序/公网方案）
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print("端口被占用，启动失败")
        return

    url = "http://127.0.0.1:%d/" % port
    print("快乐8 预测台已启动：%s" % url)
    for ip in lan_ips():
        print("手机（同一 Wi-Fi）访问：http://%s:%d/" % (ip, port))
    print("关闭此窗口即停止同步服务")
    if "--no-open" not in sys.argv:       # 调试时可加 --no-open 跳过自动打开
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
