#!/usr/bin/env bash
# 以调试端口启动 Chrome，供 CDP 连接生图用。
#
# 重要：Chrome 只有在"完全没有实例在跑"的时候，--remote-debugging-port 才会生效。
# 如果已经开着 Chrome 再执行这个脚本，新窗口会挂到已有进程上，端口不会打开——
# 这是这套链路最常见的静默失败原因。所以脚本会先检测并提示。
#
# 用法：  ./scripts/launch_chrome.sh
set -euo pipefail

PORT="${CDP_PORT:-9230}"
# 独立的用户数据目录，和你日常用的 Chrome 配置隔离开，
# 避免调试端口影响日常浏览，也避免日常操作把 ChatGPT 标签页关掉。
PROFILE_DIR="${HOME}/.chrome-cdp-profile"

case "$(uname -s)" in
  Darwin) CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ;;
  Linux)  CHROME="$(command -v google-chrome || command -v chromium || true)" ;;
  *)      echo "不支持的系统：$(uname -s)" >&2; exit 1 ;;
esac

if [[ -z "${CHROME}" || ! -x "${CHROME}" ]]; then
  echo "找不到 Chrome 可执行文件：${CHROME:-<空>}" >&2
  echo "请先安装 Chrome，或用 CHROME=/path/to/chrome 指定路径。" >&2
  exit 1
fi

# 端口已经通了就不用重复启动
if curl -s --max-time 2 "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
  echo "✅ 端口 ${PORT} 已经在监听，无需重复启动。"
  echo "   确认 ChatGPT 标签页开着：python -m toutiao_publisher doctor"
  exit 0
fi

echo "启动 Chrome，调试端口 ${PORT}，配置目录 ${PROFILE_DIR}"
# nohup + disown：Chrome 必须活得比这个脚本的调用者长。
# 只写 `&` 的话，从别的脚本或自动化里调用时，调用方的 shell 一退出就给整个
# 进程组发 SIGHUP，Chrome 跟着死——手动在终端里跑看不出问题（终端一直开着），
# 一放进自动化就变成"端口刚才还在，现在没了"。
nohup "${CHROME}" \
  --remote-debugging-port="${PORT}" \
  --user-data-dir="${PROFILE_DIR}" \
  --no-first-run \
  --no-default-browser-check \
  "https://chatgpt.com" \
  >/dev/null 2>&1 &
disown

# 等端口起来
for _ in $(seq 1 20); do
  sleep 0.5
  if curl -s --max-time 2 "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
    echo "✅ 调试端口 ${PORT} 已就绪。"
    echo
    echo "下一步：在打开的窗口里登录 ChatGPT（这个 profile 是独立的，需要单独登录一次）。"
    echo "登录后验证：python -m toutiao_publisher doctor"
    exit 0
  fi
done

echo "❌ 等了 10 秒端口 ${PORT} 仍未就绪。" >&2
echo "   检查是否有其他 Chrome 实例占用了该 profile，或换个端口：CDP_PORT=9231 $0" >&2
exit 1
