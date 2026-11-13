# -*- coding: utf-8 -*-
"""
AI 自学习号码的云端生成脚本（供 GitHub Actions 定时任务使用）
- 读取最近 50 期开奖 + algos_snapshot.json 里各算法的滚动命中率
- 调用大模型给出下一期 10 个号码，写入 ai_learn.json 供静态页读取
- 未配置 Key 或调用失败时只打印原因，不写文件、不让任务失败
"""

import json
import os
import ssl
import sys
import time
import urllib.request

import sync_server as S

OUT_FILE = os.path.join(S.BASE, "ai_learn.json")        # 供前端读取的 AI 自学习结果
ALGO_SNAP = os.path.join(S.BASE, "algos_snapshot.json")  # 各算法滚动命中率来源


def parse_nums(text):
    """从模型输出里按顺序提取 1-80 的号码（\d{1,2} 才能吃到 07 这种前导零）"""
    nums = []
    for x in __import__("re").findall(r"\d{1,2}", text):
        v = int(x)
        if 1 <= v <= 80 and v not in nums:
            nums.append(v)
    return nums[:10]


def main():
    conf = S.load_llm_conf()
    if not conf:
        print("未配置大模型（KL8_LLM_URL / KL8_LLM_KEY / KL8_LLM_MODEL），跳过 AI 自学习生成")
        return

    rows = S.fetch_rows(60)[-50:]
    if not rows:
        print("拉取开奖数据失败，跳过 AI 自学习生成")
        return

    # 同一期只生成一次：定时任务窗口内每 10 分钟会跑一次，没有新开奖就不必重复调用模型
    next_issue = str(int(rows[-1]["code"]) + 1)
    if not os.environ.get("KL8_AI_FORCE"):
        try:
            with open(OUT_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
            if old.get("issue") == next_issue and old.get("based") == rows[-1]["code"]:
                print("第 %s 期已有 AI 结果（基于 %s），跳过" % (next_issue, old["based"]))
                return
        except Exception:
            pass

    # 各算法近期表现：用快照里的滚动命中率，越高说明这套逻辑近期越准
    weights = []
    try:
        with open(ALGO_SNAP, "r", encoding="utf-8") as f:
            snap = json.load(f)
        for a in snap.get("algos", []):
            weights.append({
                "name": a.get("name") or a.get("id") or "",
                "rate": round((a.get("rate") or 0) / 100.0, 4),
            })
    except Exception as exc:
        print("读取算法快照失败（不影响出号）：%s" % exc)

    wtxt = "\n".join("%s：%.1f%%" % (w["name"][:20], w["rate"] * 100) for w in weights) or "（无）"
    dtxt = "\n".join("%s: %s" % (r["code"], " ".join("%02d" % v for v in sorted(r["nums"])))
                     for r in rows)
    prompt = (
        "你是快乐8（80 选 20）数据分析助手。\n"
        "下面是各套统计算法最近 13 期的滚动中奖率，数值越高说明该逻辑近期越准：\n%s\n\n"
        "最近 %d 期开奖号码：\n%s\n\n"
        "请结合这些命中率权重与号码冷热、遗漏、区间分布，给出下一期最值得关注的 10 个号码"
        "（01-80、不重复），按推荐度从高到低排序，用逗号分隔。只回答数字，不要解释。"
        % (wtxt, len(rows), dtxt))
    body = json.dumps({
        "model": conf["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(conf["url"], data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + conf["key"],
    })
    try:
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=180, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
    except Exception as exc:
        print("调用大模型失败，跳过：%s" % exc)
        return

    nums = parse_nums(text)
    if len(nums) < 10:
        print("模型返回号码不足 10 个，跳过：%s" % text[:80])
        return

    out = {
        "issue": str(int(rows[-1]["code"]) + 1),      # 预测的是下一期
        "based": rows[-1]["code"],                    # 生成时机：最新已开奖期
        "model": conf["model"],
        "generated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime()),
        "nums": nums,
        "weights": weights,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("已写入 ai_learn.json：第 %s 期 %s（%s）" % (out["issue"], nums, out["model"]))


if __name__ == "__main__":
    sys.exit(main())
