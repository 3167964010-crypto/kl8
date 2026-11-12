# 快乐8 预测台 · 使用说明

## 一、线上地址

| 页面 | 地址 |
| --- | --- |
| 首页（开奖记录 + 预测） | https://3167964010-crypto.github.io/kl8/ |
| 算法对比（7 套算法） | https://3167964010-crypto.github.io/kl8/algos.html |
| 奖金统计（官方奖级对照） | https://3167964010-crypto.github.io/kl8/prize.html |

手机浏览器打开首页后，用「添加到主屏幕」可以像 App 一样使用，断网也能看上次同步的数据。

## 二、目录结构

| 文件 | 作用 |
| --- | --- |
| `index.html` | 首页：最新开奖、100 期对照、AI 预测、十种玩法推荐 |
| `algos.html` | 算法对比：7 套算法各自滚动命中率与历史表 |
| `prize.html` | 奖金统计：官方奖金档位对照表 + 各算法累计奖金 |
| `sync_server.py` | 本地同步服务（端口 8767）：抓官方数据、算预测、提供接口 |
| `build_snapshot.py` | 生成静态快照，供线上无接口环境读取 |
| `snapshot.json` / `algos_snapshot.json` / `kl8_snapshot.json` | 页面实际读取的数据快照 |
| `manifest.json` / `sw.js` | 添加到主屏幕（PWA）与离线缓存 |
| `gate.js` | 页面访问口令 |
| `.github/workflows/snapshot.yml` | 云端定时生成快照并提交 |
| `启动预测台.bat` | 双击启动本地服务并打开浏览器 |

## 三、本地运行

双击 `启动预测台.bat`，或在该目录执行：

```bat
python sync_server.py
```

浏览器打开 http://127.0.0.1:8767/ 。本地模式比线上多两项能力：**自选号码定稿追踪**、**页面内配置大模型**。

## 四、数据来源与自动更新

- 开奖数据：中国福利彩票官网（`www.cwl.gov.cn`）为主，新浪走势图为补充。
- 云端更新：GitHub Actions 每天北京时间 **21:35** 和 **22:05** 自动执行 `python build_snapshot.py`，把结果提交回仓库，Pages 随后自动刷新。
- 手动更新：仓库 `Actions` → `更新快乐8数据快照` → `Run workflow`。
- 本地更新：首页右上角「↻ 立即同步」。

## 五、大模型配置

**线上（推荐，密钥不外泄）**

1. 仓库 `Settings` → `Secrets and variables` → `Actions` → `New repository secret`
2. 依次添加三个：

| Name | Value |
| --- | --- |
| `KL8_LLM_URL` | `https://api.xiaomimimo.com/v1/chat/completions` |
| `KL8_LLM_KEY` | 你的 API 密钥 |
| `KL8_LLM_MODEL` | `mimo-v2.6-flash` |

3. 到 `Actions` 手动 `Run workflow` 一次；成功后「AI 预测」即改由大模型生成。
   未配置时自动回退为本地五维统计模型，页面不会报错。

**本地**

页面点「配置大模型 API」填入地址 / 密钥 / 模型名，保存到 `llm_config.json` 即可。
该文件已被 `.gitignore` 排除，**不会被上传到公开仓库**；线上页面不显示配置入口，浏览器里也拿不到密钥。

## 六、访问口令

`gate.js` 里的口令按当天日期自动生成（年月日各取两位）：

```js
// 2026-11-12 → 261112，次日自动变为 261113
function dailyPwd() {
  var d = new Date();
  return String(d.getFullYear()).slice(2) +
         ('0' + (d.getMonth() + 1)).slice(-2) +
         ('0' + d.getDate()).slice(-2);
}
```

不需要手动改口令；浏览器会记住"今天已通过"，隔天打开需重新输入当天日期（如 `261113`）。

需要说明：这是**前端口令**，能挡住随手打开的人，但懂技术的人查看源码仍可绕过。
如果要真正的访问控制，可以换到 Cloudflare Pages 并使用 Cloudflare Access（邮箱白名单，免费额度足够个人使用），代价是需要注册 Cloudflare 并重新部署一次。

## 七、常见问题

- **页面 404 / 打开是旧内容**：Pages 首次部署需要 1—2 分钟；改过配置后可用 `Ctrl + F5` 强刷。
- **数据不更新**：看 `Actions` 是否有失败的运行；若日志里抓取官方数据失败，多为云端出口访问受限，可在本地跑 `python build_snapshot.py` 后提交。
- **免费账号的 Pages 只能用于公开仓库**：仓库一旦转私有，线上站点会停止发布。
