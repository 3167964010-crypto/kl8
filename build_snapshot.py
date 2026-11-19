# -*- coding: utf-8 -*-
"""
生成静态数据快照（供免费静态托管使用）
- 复用 sync_server.py 的全部抓取与计算逻辑，避免两套口径
- 输出 snapshot.json（首页预测）与 algos_snapshot.json（算法页）
- 页面优先请求本地接口，接口不可用时自动回退读取这两个文件
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import sync_server as S  # noqa: E402  复用现有实现


def dump(name, obj):
    """把对象写入同目录的 JSON 文件，使用紧凑格式减小体积"""
    path = os.path.join(BASE, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    print("已写入 %s（%d 字节）" % (name, os.path.getsize(path)))


def fetch_with_retry(count=501, times=3, wait=20, least=120):
    """抓取开奖数据，失败或过少时重试；仍失败返回空/少量结果由调用方兜底"""
    rows = []
    for i in range(times):
        rows = S.fetch_rows(count)
        if len(rows) >= least:
            return rows
        print("第 %d 次拉取仅 %d 期，%d 秒后重试 ..." % (i + 1, len(rows), wait))
        time.sleep(wait)
    return rows


def latest_issue():
    """读取仓库里 kl8_snapshot.json 的最新期号，用于判断这一轮是否已经抓到新开奖"""
    try:
        with open(os.path.join(BASE, "kl8_snapshot.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data["result"][-1]["code"]
    except Exception:
        return ""


def bj_now():
    """北京时间（runner 默认 UTC，+8 得到北京时间）"""
    return datetime.utcnow() + timedelta(hours=8)


def wait_for_new(before, wait_min):
    """
    开奖后刚触发任务时，官方数据往往还没发布。
    在 wait_min 分钟内每 30 秒重抓一次，一旦出现新期号就立刻返回，
    这样一轮任务就能覆盖整个发布窗口，不用指望下一次 cron（GitHub 定时常有延迟）。
    """
    if wait_min <= 0:
        return fetch_with_retry()
    today = bj_now().strftime("%Y-%m-%d")   # 北京时间今天
    deadline = time.time() + wait_min * 60
    while True:
        rows = fetch_with_retry(times=2, wait=15)
        cur = rows[-1]["code"] if rows else ""
        # 已经抓到今天的开奖就直接结束，不必空等（避免晚上重复触发时空耗 runner）
        if len(rows) >= 120 and rows[-1].get("date", "")[:10] == today:
            print("今天（%s）的 %s 已在数据里，无需等待" % (today, cur))
            return rows
        if len(rows) >= 120 and cur and cur > before:
            print("已抓到新一期 %s（原 %s），立即生成快照" % (cur, before or "—"))
            return rows
        if time.time() >= deadline:
            print("等待 %d 分钟仍未出现新一期（当前最新 %s）" % (wait_min, cur or "—"))
            return rows
        print("尚无新一期（当前最新 %s），30 秒后重试 ..." % (cur or "—"))
        time.sleep(30)


def main():
    started = time.time()
    print("开始生成快照 ...")

    # 多抓一些：算法验证页要回测 300 期，需要 500 期历史；其它快照只用最近 100 期
    wait_min = int(os.environ.get("KL8_WAIT_MIN", "0") or 0)
    # 北京时间 20 点以后把等待窗口放大到 100 分钟：
    # GitHub 的定时任务会被大幅延迟甚至漏掉（实测 6 天只跑了 23 次，应约 190 次），
    # 只要这一轮在 20:00 后启动，就一直等到抓到当天开奖再生成，避免白跑。
    if bj_now().hour >= 20 and wait_min < 100:
        wait_min = 100
    rows = wait_for_new(latest_issue(), wait_min)
    if len(rows) < 120:
        # 保留上一次已生成的快照，不让线上数据被清空；同时打警告，避免"全绿但没更新"
        print("::warning::拉取失败（%d 期），本次不更新快照，保留上一次结果" % len(rows))
        return

    # 主预测与 9 套算法沿用原有 100 期口径，避免改变正式页结果
    recent = rows[-101:]
    pred = S.settle_and_predict(recent)
    algos = S.settle_algos(recent)
    kl8 = {
        "state": 0,
        "result": [
            {
                "code": r["code"],
                "date": r["date"],
                "red": ",".join("%02d" % v for v in sorted(r["nums"])),
            }
            for r in rows
        ],
    }

    dump("snapshot.json", pred)
    dump("algos_snapshot.json", algos)
    dump("kl8_snapshot.json", kl8)

    # 最新一期取自 100 期对照的首条，下一期取自当前预测
    newest = (pred.get("records") or [{}])[0].get("target_issue", "—")
    cur = pred.get("current") or {}
    print("完成：最新期 %s，下一期 %s（计划开奖 %s），耗时 %.1fs" % (
        newest, cur.get("target_issue", "—"), pred.get("next_draw_time", "—"),
        time.time() - started))


if __name__ == "__main__":
    main()
