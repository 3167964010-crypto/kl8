# -*- coding: utf-8 -*-
"""
临时探针（非交付）：在 GitHub Actions 的海外 runner 上逐个测试数据源可达性。
结果写入 probe_result.txt 并提交，用来判断哪条链路在海外可用。
"""

import ssl
import urllib.parse
import urllib.request

# 候选数据源：名称 -> 地址（覆盖官方接口、走势图、公开 txt 数据、跨域代理）
CWL = ("https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice"
       "?name=kl8&issueCount=5&pageNo=1&pageSize=5&systemType=PC")

SOURCES = [
    ("福彩官网接口", CWL),
    ("新浪走势图", "https://lotto.sina.cn/trend/qxc_qlc_proxy.d.html?lottoType=kl8&actionType=chzs&type=30"),
    ("17500升序txt", "https://data.17500.cn/kl8_asc.txt"),
    ("17500降序txt", "https://data.17500.cn/kl8_desc.txt"),
    ("500彩票历史", "https://datachart.500.com/kl8/history/newinc/history.php?limit=10&sort=0"),
    ("中彩网开奖", "https://www.zhcw.com/kjxx/kl8/"),
    ("jina代理官方", "https://r.jina.ai/" + CWL),
    ("allorigins代理", "https://api.allorigins.win/raw?url=" + urllib.parse.quote(CWL, safe="")),
    ("codetabs代理", "https://api.codetabs.com/v1/proxy?quest=" + urllib.parse.quote(CWL, safe="")),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "*/*",
}


def main():
    ctx = ssl._create_unverified_context()
    lines = ["探针结果（GitHub runner 视角）", ""]
    for name, url in SOURCES:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25, context=ctx) as resp:
                body = resp.read()
            # 取前 120 字符做特征判断，确认拿到的确实是开奖数据而不是错误页
            head = body[:120].decode("utf-8", "ignore").replace("\n", " ").replace("\r", " ")
            lines.append("[OK]   %-14s 状态=%s 长度=%d 开头=%s" % (name, resp.status, len(body), head))
        except Exception as e:
            lines.append("[FAIL] %-14s %s: %s" % (name, type(e).__name__, str(e)[:100]))
    text = "\n".join(lines)
    print(text)
    with open("probe_result.txt", "w", encoding="utf-8") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
