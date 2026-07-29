# 今日头条微头条自动发布

每天定时：**抓 RSS → AI 选题 → AI 成文 → ChatGPT 生图 → 发布到头条**，全程无人值守。

跑在你自己的 Mac 上（需要本地 Chrome 和头条登录态）。

---

## 这套东西解决了什么

原来那版最大的问题不是功能不够，是**出事了你不知道**。这版针对性地补了四层：

| 问题 | 解法 |
|---|---|
| 定时任务压根没触发，悄无声息 | 独立的 `check` 命令，超过 26 小时没成功发布就推送告警 |
| 某个环节挂了，没有任何记录 | 每次运行都落一份 JSON 到 `state/runs/`，含每个环节耗时与成败 |
| 生图失败/出废图，照样发出去了 | 图片质量闸：尺寸、体积、格式、纯色检测，不达标就降级成纯文字 |
| Mac 合盖过夜，cron 任务直接丢了 | 用 launchd 而不是 cron，唤醒后会补跑 |

## 架构

```
RSS 源 (feedparser)
    ↓  按权重+时效排序，取候选池
AI 选题 (DeepSeek)         ← 硬去重(URL/标题) + 软去重(近期已发列表)
    ↓
AI 成文 (DeepSeek)         ← 字数不合格自动重写一次
    ↓
ChatGPT 生图 (CDP :9230)   ← 失败可降级，不阻断发布
    ↓  质量闸
头条发布 (Playwright)      ← storage_state.json 复用登录态
    ↓
运行记录 + 告警
```

**分级失败策略**是核心设计：配图失败降级为纯文字继续发；其余任何环节失败就中止并告警。图是锦上添花，不该让它阻断发布。

---

## 安装

```bash
cd toutiao-publisher

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp config/config.example.yaml config/config.yaml
cp config/sources.example.json config/sources.json
cp .env.example .env
```

编辑 `.env`（填 LLM key 和至少一个告警渠道），编辑 `config/config.yaml`（改 `content.domain` 等定位信息），编辑 `config/sources.json`（换成你关注的 RSS 源）。

> **告警渠道一定要配。** 不配的话失败了你收不到通知，等于回到原来那个"悄无声息"的状态。Server酱最省事，微信直接收推送：https://sct.ftqq.com

## 首次配置（三步，各做一次）

**1. 存头条登录态**

```bash
python scripts/login.py
```

开浏览器 → 扫码登录 → 回终端按回车。登录态存到 `state/storage_state.json`，有效期通常几周。

**2. 启动带调试端口的 Chrome**

```bash
./scripts/launch_chrome.sh
```

在打开的窗口里登录 ChatGPT。这个 Chrome 用独立配置目录（`~/.chrome-cdp-profile`），和你日常用的 Chrome 互不干扰。

> ⚠️ **最常见的坑**：Chrome 只有在"完全没有实例在跑"时 `--remote-debugging-port` 才生效。如果已经开着 Chrome 再启动，新窗口会挂到已有进程上，端口根本不会打开，而且**没有任何报错**。脚本会检测并提示。

**3. 体检**

```bash
python -m toutiao_publisher doctor
```

七项逐个检查，每个失败都会告诉你具体怎么修。全绿了再往下走。

## 日常使用

```bash
# 演练：走完全流程但不真的发布，会存一张预览截图
python -m toutiao_publisher run

# 真实发布
python -m toutiao_publisher run --live

# 看最近 15 次运行
python -m toutiao_publisher status

# 健康检查：判断是否停摆（给定时任务用）
python -m toutiao_publisher check

# 逐项体检
python -m toutiao_publisher doctor
```

退出码：`0` 成功 / `1` 失败 / `2` 正常跳过（比如今天没合适选题、或已达当日上限）。

> **第一次务必先跑演练。** 确认选题、文案、配图都符合预期，再加 `--live`。

## 装定时任务

```bash
./scripts/install_launchagent.sh                 # 默认 09:30 发布，11:00 健康检查
RUN_HOUR=8 RUN_MIN=0 ./scripts/install_launchagent.sh
./scripts/install_launchagent.sh --uninstall
```

装的是两个 launchd 任务：

- `studio.muzi.toutiao.publish` — 每天跑发布流程
- `studio.muzi.toutiao.healthcheck` — 每天晚一点检查"今天到底发出去没有"

**为什么必须是两个任务**：发布流程内部的告警，只能在流程跑起来之后才发得出去。如果定时任务压根没触发，它是哑的。健康检查作为独立任务，专门发现这种情况——这正是原来那版缺的一环。

**为什么用 launchd 不用 cron**：cron 在 Mac 睡眠期间到点不补跑，醒来也不补，任务就那么丢了；而且 macOS 从 Catalina 起对 cron 有全盘访问限制，也常导致静默失败。launchd 的 `StartCalendarInterval` 会在唤醒后补跑。**你上次"某天突然没发"，大概率就是这个原因。**

常用命令：

```bash
launchctl list | grep studio.muzi.toutiao                      # 看是否装上
launchctl kickstart -k gui/$(id -u)/studio.muzi.toutiao.publish  # 立刻手动触发一次
tail -f state/logs/$(date +%F).log                             # 实时看日志
```

## 目录结构

```
config/config.yaml           行为配置（不进版本库）
config/sources.json          RSS 源（不进版本库）
.env                         密钥（不进版本库）

src/toutiao_publisher/
  __main__.py                CLI 入口
  config.py                  配置加载与校验
  pipeline.py                流程编排 + 分级失败策略
  state.py                   运行记录 + 去重历史
  notify.py                  告警（Server酱 / 钉钉）
  healthcheck.py             check（停摆检测）+ doctor（逐项体检）
  sources/rss.py             RSS 抓取，单源失败不拖垮整批
  content/llm.py             LLM 客户端（OpenAI 兼容，默认 DeepSeek）
  content/selector.py        AI 选题 + 两层去重
  content/writer.py          AI 成文 + 字数校验重写
  images/cdp_chatgpt.py      CDP 驱动 ChatGPT 生图
  images/quality.py          图片质量闸
  publish/toutiao.py         Playwright 发布

scripts/login.py                   首次登录，存登录态
scripts/launch_chrome.sh           启动带调试端口的 Chrome
scripts/install_launchagent.sh     装/卸定时任务

state/                       运行时数据（不进版本库）
  storage_state.json         头条登录态
  runs/*.json                每次运行的记录
  published.json             已发布历史，用于去重
  logs/*.log                 日志
  images/*.png               生成的配图
```

## 排查

**先看这两个，八成能定位：**

```bash
python -m toutiao_publisher status     # 最近跑了几次、成没成、失败在哪
python -m toutiao_publisher doctor     # 外部依赖逐项体检
```

| 现象 | 原因与解法 |
|---|---|
| `连不上 http://127.0.0.1:9230` | Chrome 没开调试端口。**先完全退出 Chrome**，再 `./scripts/launch_chrome.sh` |
| `没找到包含 chatgpt.com 的标签页` | 那个 Chrome 窗口里得开着 ChatGPT 并保持登录 |
| `头条登录态已失效` | 重跑 `python scripts/login.py` |
| `等了 240 秒没等到新图` | ChatGPT 拒绝了提示词或额度用尽。打开标签页看它实际回了什么 |
| `找不到「editor」对应的元素` | 头条改版了。更新 `publish/toutiao.py` 里的 `_SELECTORS` |
| 配图总是被质量闸拦下 | 看 `status` 里的"无图"原因。确实太严就调 `config.yaml` 的 `image.quality` |
| 完全没运行记录 | 定时任务没触发。`launchctl list \| grep toutiao`，看 `state/logs/launchd-*.err.log` |

**DOM 选择器**都集中在两个地方，ChatGPT 或头条改版时只改这两处：

- `src/toutiao_publisher/images/cdp_chatgpt.py` → `_SELECTORS`
- `src/toutiao_publisher/publish/toutiao.py` → `_SELECTORS`

每项都配了多个候选选择器，从前往后试，能扛住小改动。

## 测试

```bash
python -m pytest tests/ -q
```

覆盖质量闸、去重、状态存储、LLM JSON 抽取、钉钉签名等纯逻辑。浏览器和 LLM 相关的部分不做 mock——那种 mock 测不出真问题，靠 `doctor` 做真实体检更有意义。

## 已知限制

- **必须有人登录的桌面会话**：Playwright 有头模式和 CDP 都需要图形环境，不能跑在纯 SSH 会话或服务器上。
- **头条有风控**：已经做了逐字符随机延时输入、有头模式、复用真实登录态。但发得太频繁仍可能触发限制，`max_posts_per_day` 默认 1 条是有意保守。
- **CDP 生图这条链路天然脆**：依赖 ChatGPT 前端 DOM，官方改版就会失效。所以设计成失败可降级——挂了就发纯文字，不阻断发布。如果哪天想彻底稳下来，换成图片 API（即梦/通义/DALL-E）是更省心的路，`images/` 下加一个新实现、在 `pipeline.py` 里换掉即可。
