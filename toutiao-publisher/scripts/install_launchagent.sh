#!/usr/bin/env bash
# 安装 macOS 定时任务（launchd），装两个：
#   1. 每天定时跑发布流程
#   2. 每天晚一点跑健康检查，发现"压根没跑"就告警
#
# 为什么用 launchd 而不是 cron：
#   cron 在 Mac 睡眠期间到点不会补跑，醒来也不会补——机器合盖过夜，任务就直接丢了，
#   而且悄无声息。launchd 的 StartCalendarInterval 会在唤醒后补跑错过的任务。
#   这大概率就是"某天突然没发"的真正原因。
#   另外 macOS 从 Catalina 起对 cron 有全盘访问限制，也常导致静默失败。
#
# 用法：
#   ./scripts/install_launchagent.sh              # 默认 09:30 发布、11:00 健康检查
#   RUN_HOUR=8 RUN_MIN=0 ./scripts/install_launchagent.sh
#   ./scripts/install_launchagent.sh --uninstall  # 卸载
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL_RUN="studio.muzi.toutiao.publish"
LABEL_CHECK="studio.muzi.toutiao.healthcheck"
AGENTS_DIR="${HOME}/Library/LaunchAgents"

RUN_HOUR="${RUN_HOUR:-9}"
RUN_MIN="${RUN_MIN:-30}"
CHECK_HOUR="${CHECK_HOUR:-11}"
CHECK_MIN="${CHECK_MIN:-0}"

# ---------- 卸载 ----------
if [[ "${1:-}" == "--uninstall" ]]; then
  for label in "${LABEL_RUN}" "${LABEL_CHECK}"; do
    plist="${AGENTS_DIR}/${label}.plist"
    launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
    rm -f "${plist}"
    echo "已卸载：${label}"
  done
  exit 0
fi

# ---------- 找 Python ----------
# launchd 的环境变量极简，PATH 里往往没有 venv，必须写绝对路径。
if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  PYTHON="${PROJECT_DIR}/.venv/bin/python"
else
  PYTHON="$(command -v python3 || true)"
  echo "⚠️  没找到 ${PROJECT_DIR}/.venv/bin/python，退回系统 python3：${PYTHON}"
  echo "   建议用虚拟环境，否则依赖升级容易把定时任务搞挂。"
fi

if [[ -z "${PYTHON}" || ! -x "${PYTHON}" ]]; then
  echo "找不到可用的 python3。" >&2
  exit 1
fi

mkdir -p "${AGENTS_DIR}" "${PROJECT_DIR}/state/logs"

# ---------- 生成 plist ----------
# $1=label  $2=命令参数  $3=小时  $4=分钟  $5=日志名
write_plist() {
  local label="$1" args="$2" hour="$3" minute="$4" logname="$5"
  local plist="${AGENTS_DIR}/${label}.plist"

  cat > "${plist}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>-m</string>
        <string>toutiao_publisher</string>
$(for a in ${args}; do echo "        <string>${a}</string>"; done)
    </array>

    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>

    <!-- 让 python 找得到 src/ 下的包 -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key>
        <string>${PROJECT_DIR}/src</string>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>${hour}</integer>
        <key>Minute</key>
        <integer>${minute}</integer>
    </dict>

    <!-- 睡眠错过的任务，唤醒后补跑一次。这是 cron 做不到的关键差别。 -->
    <key>RunAtLoad</key>
    <false/>

    <key>StandardOutPath</key>
    <string>${PROJECT_DIR}/state/logs/${logname}.out.log</string>
    <key>StandardErrorPath</key>
    <string>${PROJECT_DIR}/state/logs/${logname}.err.log</string>
</dict>
</plist>
PLIST

  # 重装：先卸再装，避免旧定义残留
  launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "${plist}"
  echo "✅ 已安装 ${label}  →  每天 ${hour}:$(printf '%02d' "${minute}")"
}

write_plist "${LABEL_RUN}"   "run --live" "${RUN_HOUR}"   "${RUN_MIN}"   "launchd-publish"
write_plist "${LABEL_CHECK}" "check"      "${CHECK_HOUR}" "${CHECK_MIN}" "launchd-healthcheck"

echo
echo "查看已安装：  launchctl list | grep studio.muzi.toutiao"
echo "立刻试跑一次：launchctl kickstart -k gui/$(id -u)/${LABEL_RUN}"
echo "卸载：        ./scripts/install_launchagent.sh --uninstall"
echo
echo "⚠️  发布任务用的是 --live（真实发布）。先手动跑演练确认没问题："
echo "    cd ${PROJECT_DIR} && PYTHONPATH=src ${PYTHON} -m toutiao_publisher run"
