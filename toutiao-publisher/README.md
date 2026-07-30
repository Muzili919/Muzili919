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
./setup.sh
```

一条命令搞定虚拟环境、依赖、Chromium、配置文件，最后跑一遍测试，并列出还需要你手动填的项。幂等，重复执行安全。

> **Python 需要 3.10–3.13**，上限是硬的。playwright 依赖 greenlet，后者带 C 扩展，新版 Python 发布后往往几个月都没有预编译轮子，源码编译又对不上新的 C API——在 3.14 上装会甩出一屏 `Py_C_RECURSION_LIMIT` 未定义之类的编译错误。`setup.sh` 会自动挑一个区间内的解释器，默认的 `python3` 太新也不影响；想指定就 `PYTHON_BIN=/path/to/python3.13 ./setup.sh`。

装完编辑三个文件：

- `.env` —— LLM key 和至少一个告警渠道
- `config/config.yaml` —— `content.domain` 等账号定位信息
- `config/sources.json` —— 换成你关注的 RSS 源

> **告警渠道一定要配。** 不配的话失败了你收不到通知，等于回到原来那个"悄无声息"的状态。三选一：
>
> - **邮件**（`SMTP_*` 四项）——手上有 SMTP 授权码就能用，不用注册任何服务，是唯一能立刻配好的
> - **Server酱**——微信收推送最方便，但要先注册：https://sct.ftqq.com
> - **钉钉群机器人**——要先建群拿 webhook

## 首次配置（三步，各做一次）

这三步需要你的账号和手机，没法自动化。

**1. 存头条登录态**

```bash
python scripts/login.py
```

开浏览器 → 扫码登录 → 回终端按回车。登录态存到 `state/storage_state.json`，有效期通常几周。

> 如果你本来就有一个开着调试端口、已经登录着头条的 Chrome（比如别的自动化脚本在用那个），连扫码都不用：
>
> ```bash
> python scripts/import_login_from_cdp.py --port 9228
> ```
>
> 它把 cookie 和 localStorage 直接搬过来。全程只读，不新建标签页、不导航、不动源浏览器。登录态过期时也是跑这个。

**2. 启动带调试端口的 Chrome**

```bash
./scripts/launch_chrome.sh          # 默认端口 9230
CDP_PORT=9235 ./scripts/launch_chrome.sh
```

在打开的窗口里登录 ChatGPT。这个 Chrome 用独立配置目录（`~/.chrome-cdp-profile`），和你日常用的 Chrome 互不干扰。

> **不需要退出正在跑的 Chrome。** 常见说法是"必须先完全退出 Chrome，否则 `--remote-debugging-port` 不生效"——那只在复用**同一个** `--user-data-dir` 时成立：那种情况下新命令只是给已有进程递个信号然后自己退了，端口不会开，也不报错。本脚本用的是独立配置目录，所以会真的起一个新进程，端口正常打开。已在同时跑着 5 个其它调试实例的机器上实测过。
>
> ⚠️ 真正要小心的是**端口撞车**。脚本发现端口已在监听就直接说"已就绪"退出——如果占用者是另一个 Chrome 实例，你会得到一个静默走错浏览器的链路：`doctor` 跑去那个浏览器里找 ChatGPT 标签页，永远找不到。多实例环境下先确认端口没人用，或用 `CDP_PORT=` 换一个，并同步改 `config.yaml` 的 `image.cdp_port`。

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
>
> 演练不占当天发布配额，也不会让"多久没发布"的健康检查闭嘴——只有真实发布才算数。所以想演练几次就演练几次，不会把 `--live` 挡在上限外面。

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

scripts/login.py                   首次登录，扫码存登录态
scripts/import_login_from_cdp.py   从已登录的 CDP 实例搬登录态，免扫码
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
| `连不上 http://127.0.0.1:9230` | Chrome 没在这个端口上开调试。跑 `./scripts/launch_chrome.sh`（不用退出别的 Chrome） |
| `没找到包含 chatgpt.com 的标签页` | 那个 Chrome 窗口里得开着 ChatGPT 并保持登录。也可能是**端口被另一个 Chrome 实例占了**，脚本会误报"已就绪"——换 `CDP_PORT=` 并同步改 `config.yaml` |
| `头条登录态已失效` | 重跑 `python scripts/login.py`，或 `python scripts/import_login_from_cdp.py --port <已登录实例端口>` |
| `编辑器内容与预期不符` | 草稿没清干净或输入被编辑器吞了。已经中止、什么都没发，重跑即可；反复出现就看 `state/dry-run-preview.png` |
| `等了 N 秒没等到新图`（图其实生成了） | ChatGPT 换过图片存放地址。打开 `state/` 里留着的那个临时标签页，看 `<img src>` 长什么样，把新形态加进 `cdp_chatgpt.py` 的 `_SELECTORS['generated_image']`。2026-07 是 `/backend-api/estuary/content` |
| `上传弹窗里没找到可点的「确定」` | `no-list` = 缩略图没出来，文件没被接收；`no-button` = 图在弹窗里但确认按钮一直禁用或改了名。看 `state/image-confirm-failed.png` |
| `「头条首发」点不上` | 已知限制，见下。不影响发布，只是少一份独发分成 |
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
python -m pytest tests/ -q          # 34 项
```

**单元测试**（`test_core.py`，24 项）覆盖质量闸、去重、状态存储、LLM JSON 抽取、钉钉签名等纯逻辑。

**集成测试**（`test_integration.py`，10 项）拿真 Chromium 跑 CDP 生图和 Playwright 发布两条链路。用本地仿真页面代替 ChatGPT 和头条，DOM 结构复刻生产代码依赖的特征，交互行为（React 式的按钮禁用、异步出图、隐藏 file input）也一并模拟。验证到的东西：

- CDP WebSocket 协议层：命令/响应配对、事件流过滤
- `execCommand` 插入文本能触发页面状态更新——直接改 `innerText` 不会，发送按钮会一直禁用，这条路径必须实测才发现得了
- 轮询等待能正确识别"新增的"图片
- 页面内 `fetch` + `FileReader` 转 base64 的回传路径
- 完整发布流程：输入正文 → 上传配图（走隐藏 input 兜底）→ 点发布 → 确认成功
- 演练模式确实不点发布，且留下预览截图

**选择器本身仍需对真实站点验证**——仿真页面证明的是协议层和交互逻辑正确，不能证明 `_SELECTORS` 匹配得上今天的 ChatGPT 和头条。这部分靠 `doctor` 和演练模式在本机确认。

没有 Chromium 的环境会自动跳过集成测试，不影响单元测试。

## 已知限制

- **真实发布这一步还没在线上验证过。** 演练模式已经在真实头条发布页跑通：登录态、找编辑器、逐字符输入、字数核对、关话题下拉框、上传配图并确认挂上，全部实测正常。但**点"发布"那一下没有真发过**，所以有一个风险还没排除：头条对合成的鼠标/键盘事件有过滤，同一个账号的另一套脚本被迫改用 pyautogui 发 OS 级点击才点得动。如果 `--live` 之后卡在"点了发布但 N 秒内没等到成功提示"，那就是撞上这个了——不是选择器错了，改 `_SELECTORS` 没用，得换成 OS 级点击。第一次 `--live` 请盯着看，别直接挂定时任务。
- **「头条首发」勾不上（同一个根因）。** 代码会检查状态，未勾选时尝试补勾，但实测点不动：JS 点隐藏 input / `.byte-checkbox-mask` / `label` / `wrapper`，以及 Playwright 对 mask 的真实点击，五种方式都不改变状态。影响只是这一条拿不到 72 小时独发的额外分成，发布本身照常。变通办法：在自己的浏览器里手动勾一次，头条会记住这个偏好，之后日志会显示「已勾选，不动它」。
- **没有内容合规过滤。** 头条号没有新闻许可就不能发时政、军事、外交、经济评论，没有财经资质不能荐股，加密货币也敏感。`config/sources.json` 里只留 AI/科技源已经把风险压低了，但 AI 新闻本身也可能扯到出口管制、中美博弈这类话题。真要挂定时任务无人值守，发布前得加一道关键词黑名单。
- **必须有人登录的桌面会话**：Playwright 有头模式和 CDP 都需要图形环境，不能跑在纯 SSH 会话或服务器上。
- **头条有风控**：已经做了逐字符随机延时输入、有头模式、复用真实登录态。但发得太频繁仍可能触发限制，`max_posts_per_day` 默认 1 条是有意保守。
- **CDP 生图这条链路天然脆**：依赖 ChatGPT 前端 DOM，官方改版就会失效。所以设计成失败可降级——挂了就发纯文字，不阻断发布。如果哪天想彻底稳下来，换成图片 API（即梦/通义/DALL-E）是更省心的路，`images/` 下加一个新实现、在 `pipeline.py` 里换掉即可。
