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


def main():
    started = time.time()
    print("开始生成快照 ...")

    # 多抓一些：算法验证页要回测 300 期，需要 500 期历史；其它快照只用最近 100 期
    rows = fetch_with_retry()
    if len(rows) < 120:
        # 保留上一次已生成的快照，不让线上数据被清空，同时以成功状态退出避免误报任务失败
        print("拉取失败（%d 期），本次不更新快照，保留上一次结果" % len(rows))
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
