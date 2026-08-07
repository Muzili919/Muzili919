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
# 版本上下限都要卡。下限是语法（3.10 的 match / X | Y 类型写法）；
# 上限是 greenlet——playwright 依赖它，而它带 C 扩展，新版 Python 出来后
# 往往几个月都没有预编译轮子，源码编译又对不上新的 C API，直接炸一屏错误。
# 所以默认解释器太新时，主动挑一个装得上的，而不是让 pip 去撞墙。
PY_MIN_MINOR=10
PY_MAX_MINOR=13

step "挑选 Python 解释器"

py_minor() { "$1" -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo 0; }
py_ok() {
  local m
  m="$(py_minor "$1")"
  [[ "$m" -ge "${PY_MIN_MINOR}" && "$m" -le "${PY_MAX_MINOR}" ]]
}

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -n "${PYTHON_BIN}" ]]; then
  # 显式指定的就不再猜，但仍然校验，版本不对要当场说清楚
  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    fail "PYTHON_BIN=${PYTHON_BIN} 找不到"
    exit 1
  fi
  if ! py_ok "${PYTHON_BIN}"; then
    fail "PYTHON_BIN=${PYTHON_BIN} 是 3.$(py_minor "${PYTHON_BIN}")，需要 3.${PY_MIN_MINOR}–3.${PY_MAX_MINOR}"
    exit 1
  fi
else
  # 从新到旧找一个在区间内的：优先具体版本号，最后才试默认 python3
  for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "${cand}" >/dev/null 2>&1 && py_ok "${cand}"; then
      PYTHON_BIN="$(command -v "${cand}")"
      break
    fi
  done
fi

if [[ -z "${PYTHON_BIN}" ]]; then
  fail "找不到 3.${PY_MIN_MINOR}–3.${PY_MAX_MINOR} 的 Python。"
  if command -v python3 >/dev/null 2>&1; then
    warn "当前默认 python3 是 3.$(py_minor python3)$([[ "$(py_minor python3)" -gt "${PY_MAX_MINOR}" ]] && echo "（太新，playwright 的 greenlet 还没有对应轮子）")"
  fi
  warn "装一个：brew install python@3.13"
  warn "或指定：PYTHON_BIN=/path/to/python3.13 ./setup.sh"
  exit 1
fi
ok "使用 ${PYTHON_BIN}（3.$(py_minor "${PYTHON_BIN}")）"

# ---------- 2. 虚拟环境 ----------
step "准备虚拟环境"
PIP=".venv/bin/pip"
PYTHON=".venv/bin/python"

# 已存在的 venv 如果是用界外版本建的，留着只会在装依赖时炸，直接重建
if [[ -d .venv ]] && ! py_ok "${PYTHON}"; then
  warn ".venv 是 3.$(py_minor "${PYTHON}") 建的，超出支持区间，重建"
  rm -rf .venv
fi

if [[ ! -d .venv ]]; then
  "${PYTHON_BIN}" -m venv .venv
  ok "已创建 .venv（3.$(py_minor "${PYTHON}")）"
else
  ok ".venv 已存在（3.$(py_minor "${PYTHON}")），跳过"
fi
# ---------- 3. 依赖 ----------
step "安装依赖（约 1-2 分钟）"
"${PIP}" install -q --upgrade pip
"${PIP}" install -q -r requirements.txt
ok "Python 依赖已安装"

# 装包本身。少了这步，README 里所有 `python -m toutiao_publisher ...` 都会
# ModuleNotFoundError——代码在 src/ 下，不装就不在 import 路径上。
# 测试自己 sys.path.insert 了 src，所以测试全过也盖不住这个问题。
"${PIP}" install -q -e . --no-deps
ok "toutiao_publisher 已安装（可执行 python -m toutiao_publisher）"

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

if grep -qE "^(SERVERCHAN_SENDKEY|DINGTALK_WEBHOOK|SMTP_HOST)=.+" .env 2>/dev/null; then
  ok "告警渠道已配置"
else
  warn "告警渠道一个都没配 —— 失败时你收不到通知，等于白做监控"
  warn "  手上有 SMTP 授权码就填 SMTP_* 四项，不用注册任何服务"
  warn "  想用微信推送就去 Server酱：https://sct.ftqq.com"
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
