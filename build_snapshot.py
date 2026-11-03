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


def main():
    started = time.time()
    print("开始生成快照 ...")

    # 先跑主预测：会完成结算、生成下一期预测，并刷新 100 期对照
    pred = S.settle_and_predict()
    # 再跑 7 套算法：复用同一份最新开奖数据
    algos = S.settle_algos(S.fetch_rows(100))

    # 开奖记录：按官网原格式输出（red 为逗号分隔的两位数字），供页面直接解析
    rows = S.fetch_rows(100)
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
