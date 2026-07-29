#!/usr/bin/env bash
# 一键安装：能自动的全自动，只在必须你本人操作的地方停下来。
#
#   ./setup.sh
#
# 幂等——重复执行安全，已装好的部分会跳过。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
step() { echo; echo "${BOLD}▸ $*${OFF}"; }
ok()   { echo "  ${GREEN}✓${OFF} $*"; }
warn() { echo "  ${YELLOW}!${OFF} $*"; }
fail() { echo "  ${RED}✗${OFF} $*"; }

# ---------- 1. Python ----------
step "检查 Python"
if ! command -v python3 >/dev/null 2>&1; then
  fail "没有 python3。先装：brew install python@3.12"
  exit 1
fi
PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  fail "Python ${PY_VER} 太旧，需要 3.10+。升级：brew install python@3.12"
  exit 1
fi
ok "Python ${PY_VER}"

# ---------- 2. 虚拟环境 ----------
step "准备虚拟环境"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  ok "已创建 .venv"
else
  ok ".venv 已存在，跳过"
fi
PIP=".venv/bin/pip"
PYTHON=".venv/bin/python"

# ---------- 3. 依赖 ----------
step "安装依赖（约 1-2 分钟）"
"${PIP}" install -q --upgrade pip
"${PIP}" install -q -r requirements.txt
ok "Python 依赖已安装"

step "安装 Chromium（Playwright 用，约 1-2 分钟）"
if "${PYTHON}" -m playwright install chromium 2>&1 | tail -2; then
  ok "Chromium 已就绪"
else
  warn "Chromium 安装可能失败，稍后可手动重试：.venv/bin/python -m playwright install chromium"
fi

# ---------- 4. 配置文件 ----------
step "准备配置文件"
for pair in "config/config.example.yaml:config/config.yaml" \
            "config/sources.example.json:config/sources.json" \
            ".env.example:.env"; do
  src="${pair%%:*}"; dst="${pair##*:}"
  if [[ -f "${dst}" ]]; then
    ok "${dst} 已存在，保留不覆盖"
  else
    cp "${src}" "${dst}"
    ok "已生成 ${dst}"
  fi
done

# ---------- 5. 自检 ----------
step "运行测试"
if "${PYTHON}" -m pytest tests/ -q 2>&1 | tail -3; then
  ok "测试通过"
else
  warn "有测试未通过，不影响继续，但建议看一眼"
fi

# ---------- 6. 检查还缺什么 ----------
step "检查待填项"
MISSING=0

check_env() {
  local key="$1" desc="$2"
  local val
  val="$(grep -E "^${key}=" .env 2>/dev/null | cut -d= -f2- || true)"
  if [[ -z "${val}" || "${val}" == sk-xxxxxxxxxxxxxxxx ]]; then
    warn "${key} 还没填 —— ${desc}"
    MISSING=$((MISSING + 1))
  else
    ok "${key} 已填"
  fi
}

check_env LLM_API_KEY "DeepSeek API key，去 https://platform.deepseek.com 拿"

if grep -qE "^(SERVERCHAN_SENDKEY|DINGTALK_WEBHOOK)=.+" .env 2>/dev/null; then
  ok "告警渠道已配置"
else
  warn "告警渠道一个都没配 —— 失败时你收不到通知，等于白做监控"
  warn "  Server酱最省事，微信直接收推送：https://sct.ftqq.com"
  MISSING=$((MISSING + 1))
fi

if [[ -f state/storage_state.json ]]; then
  ok "头条登录态已存在"
else
  warn "头条登录态还没建立"
  MISSING=$((MISSING + 1))
fi

# ---------- 收尾 ----------
echo
echo "${BOLD}════════════════════════════════════════════════════════${OFF}"
if [[ ${MISSING} -eq 0 ]]; then
  echo "${GREEN}${BOLD}  安装完成，所有配置都齐了${OFF}"
  echo
  echo "  下一步："
  echo "    ${BOLD}source .venv/bin/activate${OFF}"
  echo "    ${BOLD}./scripts/launch_chrome.sh${OFF}          # 先完全退出 Chrome 再跑"
  echo "    ${BOLD}python -m toutiao_publisher doctor${OFF}  # 体检"
  echo "    ${BOLD}python -m toutiao_publisher run${OFF}     # 演练，不真发"
else
  echo "${YELLOW}${BOLD}  自动部分装完了，还有 ${MISSING} 项需要你本人操作${OFF}"
  echo
  echo "  这几步替代不了 —— 需要你的账号和手机："
  echo
  echo "  ${BOLD}1.${OFF} 编辑 ${BOLD}.env${OFF}，填 LLM key 和告警渠道"
  echo "  ${BOLD}2.${OFF} ${BOLD}source .venv/bin/activate && python scripts/login.py${OFF}"
  echo "     开浏览器扫码登录头条，一次就行"
  echo "  ${BOLD}3.${OFF} ${BOLD}./scripts/launch_chrome.sh${OFF}"
  echo "     ${YELLOW}先完全退出 Chrome${OFF}，再跑这个；然后在新窗口里登录 ChatGPT"
  echo
  echo "  完事后验证：${BOLD}python -m toutiao_publisher doctor${OFF}"
fi
echo "${BOLD}════════════════════════════════════════════════════════${OFF}"
echo
