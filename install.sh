#!/usr/bin/env bash
# ninngyo：一键部署 PagerMaid-Pyro，并让它只通过 Shadowsocks-2022 代理访问外网。
#
# 架构（服务器 A 上只新增两个容器，不改宿主机网络，不影响其他服务）：
#   pagermaid ──(内部网络，无外网出口)──▶ ss-local ══SS-2022══▶ 服务器 B ──▶ Telegram
#
# 用法：bash install.sh [命令]，不带命令时等同 install。详见 bash install.sh help
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${NINNGYO_DIR:-/opt/ninngyo}"
PROJECT="${NINNGYO_PROJECT:-ninngyo}"
PGM_IMAGE="${PGM_IMAGE:-teampgm/pagermaid_pyro:latest}"
SS_IMAGE="${SS_IMAGE:-ghcr.io/shadowsocks/sslocal-rust:v1.25.0}"
SOCKS_PORT=1080
HTTP_PORT=8118
TG_TEST_IP="149.154.167.51" # Telegram DC2

SS_METHODS=(2022-blake3-aes-128-gcm 2022-blake3-aes-256-gcm 2022-blake3-chacha20-poly1305)

# ---------- 输出 ----------

info() { printf '\033[32m[信息]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[警告]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

ask() { # ask 变量名 提示 [默认值]
    local __var="$1" __prompt="$2" __default="${3:-}" __reply
    [[ -n "$__default" ]] && __prompt+=" [${__default}]"
    read -r -p "${__prompt}：" __reply </dev/tty
    printf -v "$__var" '%s' "${__reply:-$__default}"
}

ask_secret() {
    local __var="$1" __reply
    read -r -s -p "$2：" __reply </dev/tty
    echo >/dev/tty
    printf -v "$__var" '%s' "$__reply"
}

confirm() {
    local reply
    read -r -p "$1 [y/N]：" reply </dev/tty
    [[ "$reply" =~ ^[Yy]$ ]]
}

# ---------- 基础 ----------

compose() {
    docker compose --project-directory "$INSTALL_DIR" -f "$INSTALL_DIR/docker-compose.yml" -p "$PROJECT" "$@"
}

require_root() {
    [[ $EUID -eq 0 ]] || die "请使用 root 运行（sudo bash $0 ${CMD:-}）"
}

require_installed() {
    [[ -f "$INSTALL_DIR/docker-compose.yml" ]] || die "尚未安装，请先执行：bash $0 install"
}

ensure_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        confirm "未检测到 Docker，是否用官方脚本（get.docker.com）安装？" || die "需要 Docker"
        curl -fsSL https://get.docker.com | sh
        systemctl enable --now docker
    fi
    docker compose version >/dev/null 2>&1 || die "需要 Docker Compose v2（docker compose 命令）"
}

# ---------- Shadowsocks 配置 ----------

urldecode() {
    local s="${1//+/%2B}" # SIP002 里的 + 不是空格
    printf '%b' "${s//%/\\x}"
}

b64url_decode() {
    local s="${1//-/+}"
    s="${s//_//}"
    while ((${#s} % 4)); do s+="="; done
    printf '%s' "$s" | base64 -d 2>/dev/null
}

# 解析 ss:// 链接（SIP002 或旧版整段 base64）
parse_ss_uri() {
    local uri="${1#ss://}" userinfo hostport decoded
    uri="${uri%%#*}"
    uri="${uri%%\?*}"
    uri="${uri%/}"
    if [[ "$uri" == *@* ]]; then
        userinfo="${uri%@*}"
        hostport="${uri##*@}"
        if [[ "$userinfo" == *:* || "$userinfo" == *%3A* || "$userinfo" == *%3a* ]]; then
            userinfo="$(urldecode "$userinfo")"
        else
            userinfo="$(b64url_decode "$userinfo")"
        fi
    else
        decoded="$(b64url_decode "$uri")"
        userinfo="${decoded%@*}"
        hostport="${decoded##*@}"
    fi
    SS_METHOD="${userinfo%%:*}"
    SS_PASSWORD="${userinfo#*:}"
    if [[ "$hostport" == \[*\]:* ]]; then
        SS_SERVER="${hostport%%]:*}"
        SS_SERVER="${SS_SERVER#[}"
        SS_PORT="${hostport##*]:}"
    else
        SS_SERVER="${hostport%:*}"
        SS_PORT="${hostport##*:}"
    fi
}

validate_ss() {
    local key_len seg
    [[ -n "${SS_SERVER:-}" ]] || die "服务器 B 地址为空"
    [[ "${SS_PORT:-}" =~ ^[0-9]+$ ]] && ((SS_PORT >= 1 && SS_PORT <= 65535)) || die "端口无效：${SS_PORT:-}"
    case "${SS_METHOD:-}" in
        2022-blake3-aes-128-gcm) key_len=16 ;;
        2022-blake3-aes-256-gcm | 2022-blake3-chacha20-poly1305) key_len=32 ;;
        *) die "不支持的加密方式：${SS_METHOD:-}（只支持 ${SS_METHODS[*]}）" ;;
    esac
    [[ "$SS_PASSWORD" =~ ^[A-Za-z0-9+/=:]+$ ]] || die "密钥格式错误：应为 base64"
    # 多用户格式 iPSK:uPSK，每一段都要是正确长度的 base64 密钥
    IFS=':' read -r -a segs <<<"$SS_PASSWORD"
    for seg in "${segs[@]}"; do
        [[ "$(printf '%s' "$seg" | base64 -d 2>/dev/null | wc -c)" -eq "$key_len" ]] ||
            die "密钥长度错误：${SS_METHOD} 需要 base64 编码的 ${key_len} 字节密钥"
    done
}

collect_ss() {
    if [[ -n "${SS_URI:-}" ]]; then
        parse_ss_uri "$SS_URI"
    elif [[ -z "${SS_SERVER:-}" ]]; then
        local uri choice
        echo
        info "配置服务器 B 的 Shadowsocks-2022（可直接粘贴 ss:// 链接，或回车后逐项填写）"
        ask_secret uri "ss:// 链接（输入不回显）"
        if [[ -n "$uri" ]]; then
            parse_ss_uri "$uri"
        else
            ask SS_SERVER "服务器 B 地址（IP 或域名）"
            ask SS_PORT "服务器 B 端口"
            echo "加密方式：1) ${SS_METHODS[0]}  2) ${SS_METHODS[1]}  3) ${SS_METHODS[2]}"
            ask choice "请选择" 2
            [[ "$choice" =~ ^[1-3]$ ]] || die "选择无效"
            SS_METHOD="${SS_METHODS[$((choice - 1))]}"
            ask_secret SS_PASSWORD "密钥（输入不回显）"
        fi
    fi
    validate_ss
    info "代理：${SS_SERVER}:${SS_PORT}（${SS_METHOD}）"
}

write_ss_config() {
    umask 077
    cat >"$INSTALL_DIR/ss-local.json" <<EOF
{
    "server": "${SS_SERVER}",
    "server_port": ${SS_PORT},
    "method": "${SS_METHOD}",
    "password": "${SS_PASSWORD}",
    "locals": [
        {"local_address": "0.0.0.0", "local_port": ${SOCKS_PORT}, "protocol": "socks", "mode": "tcp_only"},
        {"local_address": "0.0.0.0", "local_port": ${HTTP_PORT}, "protocol": "http"}
    ]
}
EOF
    chmod 600 "$INSTALL_DIR/ss-local.json"
}

# ---------- Compose ----------

write_compose() {
    local http="http://ss-local:${HTTP_PORT}" no_proxy="localhost,127.0.0.1,ss-local"
    cat >"$INSTALL_DIR/docker-compose.yml" <<EOF
# 由 ninngyo/install.sh 生成。pagermaid 只接入 internal 网络（无外网出口），
# 所有流量只能经 ss-local 走服务器 B；两个容器都不在宿主机上映射端口。
services:
  ss-local:
    image: ${SS_IMAGE}
    container_name: ${PROJECT}-ss-local
    restart: unless-stopped
    networks: [internal, egress]
    volumes:
      - ./ss-local.json:/etc/shadowsocks-rust/config.json:ro

  pagermaid:
    image: ${PGM_IMAGE}
    container_name: ${PROJECT}-pagermaid
    restart: unless-stopped
    depends_on: [ss-local]
    # 跳过镜像自带的交互式配置脚本，配置由 install.sh 预先生成在 data/config.yml
    entrypoint: ["python", "-m", "pagermaid"]
    stdin_open: true
    tty: true
    networks: [internal]
    volumes:
      - ./data:/pagermaid/workdir/data
      - ./plugins:/pagermaid/workdir/plugins
    environment:
      # Telegram 连接（Pyrogram）走 SOCKS5
      PGM_PROXY_ADDRESS: ss-local
      PGM_PROXY_PORT: "${SOCKS_PORT}"
      # 插件下载、更新检查、pip、git 等 HTTP(S) 请求走 HTTP 代理
      HTTP_PROXY: ${http}
      HTTPS_PROXY: ${http}
      http_proxy: ${http}
      https_proxy: ${http}
      NO_PROXY: ${no_proxy}
      no_proxy: ${no_proxy}
      # 关闭统计和错误上报
      PGM_ALLOW_ANALYTIC: "false"
      PGM_ERROR_REPORT: "false"

networks:
  internal:
    internal: true
  egress: {}
EOF
}

# ---------- PagerMaid 配置 ----------

write_pgm_config() {
    local cfg="$INSTALL_DIR/data/config.yml" qr="False" choice
    if [[ -f "$cfg" ]]; then
        info "已存在 data/config.yml，保留原配置"
        return
    fi
    if [[ -z "${API_ID:-}" || -z "${API_HASH:-}" ]]; then
        echo
        info "填写 Telegram API（https://my.telegram.org/apps）"
        ask API_ID "api_id"
        ask_secret API_HASH "api_hash（输入不回显）"
    fi
    [[ "$API_ID" =~ ^[0-9]+$ ]] || die "api_id 应为纯数字"
    [[ "$API_HASH" =~ ^[0-9a-fA-F]{32}$ ]] || die "api_hash 应为 32 位十六进制"
    if [[ -n "${PGM_QRCODE_LOGIN:-}" ]]; then
        qr="$PGM_QRCODE_LOGIN"
    else
        echo "登录方式：1) 手机号 + 验证码  2) 扫描二维码（用已登录的手机 Telegram 扫）"
        ask choice "请选择" 1
        [[ "$choice" == 2 ]] && qr="True"
    fi

    umask 077
    docker run --rm --entrypoint cat "$PGM_IMAGE" config.gen.yml >"$cfg"
    sed -i \
        -e "s/^api_id: .*/api_id: \"${API_ID}\"/" \
        -e "s/^api_hash: .*/api_hash: \"${API_HASH}\"/" \
        -e "s/^qrcode_login: .*/qrcode_login: \"${qr}\"/" \
        -e 's/^allow_analytic: .*/allow_analytic: "False"/' \
        -e 's/^error_report: .*/error_report: "False"/' \
        "$cfg"
    chmod 600 "$cfg"
    info "已生成 data/config.yml（其余选项可之后手动修改）"
}

# ---------- 插件 ----------

sync_plugins() {
    local dir name count=0
    [[ -d "$SCRIPT_DIR/plugins" ]] || return 0
    for dir in "$SCRIPT_DIR"/plugins/*/; do
        [[ -f "$dir/main.py" ]] || continue
        name="$(basename "$dir")"
        install -m 644 "$dir/main.py" "$INSTALL_DIR/plugins/${name}.py"
        count=$((count + 1))
    done
    info "已同步 ${count} 个自定义插件到 ${INSTALL_DIR}/plugins"
}

# ---------- 检查 ----------

check_proxy() {
    info "检查代理链路..."
    compose up -d ss-local >/dev/null
    compose run --rm --no-deps -T --entrypoint python pagermaid - <<PY
import asyncio, socket
import httpx
from python_socks.async_.asyncio import Proxy

async def main():
    ok = True
    try:
        data = httpx.get("https://ipinfo.io/json", timeout=20).json()
        print(f"  [HTTP 出口]   {data.get('ip')}  {data.get('country')}  {data.get('org', '')}")
    except Exception as e:
        ok = False
        print(f"  [HTTP 出口]   失败：{e!r}")
    try:
        sock = await asyncio.wait_for(
            Proxy.from_url("socks5://ss-local:${SOCKS_PORT}").connect("${TG_TEST_IP}", 443), 20
        )
        sock.close()
        print("  [Telegram]    通过 SOCKS5 连接 DC2 成功")
    except Exception as e:
        ok = False
        print(f"  [Telegram]    失败：{e!r}")
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection("${TG_TEST_IP}", 443), 5)
        w.close()
        ok = False
        print("  [网络隔离]    警告：容器可以绕过代理直连外网！")
    except Exception:
        print("  [网络隔离]    直连已被阻断（正常）")
    raise SystemExit(0 if ok else 1)

asyncio.run(main())
PY
}

is_logged_in() {
    [[ -s "$INSTALL_DIR/data/pagermaid.session" ]] || return 1
    compose run --rm --no-deps -T --entrypoint python pagermaid -c '
import sqlite3, sys
try:
    row = sqlite3.connect("data/pagermaid.session").execute("select user_id from sessions").fetchone()
except Exception:
    row = None
sys.exit(0 if row and row[0] else 1)
' >/dev/null 2>&1
}

# ---------- 命令 ----------

cmd_login() {
    require_installed
    if is_logged_in; then
        info "账号已登录"
        return
    fi
    [[ -t 0 ]] || die "登录需要交互终端"
    compose stop pagermaid >/dev/null 2>&1 || true
    compose up -d ss-local >/dev/null
    echo
    info "即将进入登录界面，按提示输入手机号（带国际区号，如 +8613800000000）、验证码和两步验证密码。"
    info "看到 PagerMaid 启动成功的日志后，按 Ctrl+C 退出，脚本会自动转入后台运行。"
    echo
    read -r -p "按回车继续..." </dev/tty
    compose run --rm --name "${PROJECT}-pagermaid-login" pagermaid || true
    is_logged_in || die "未检测到有效登录，可重新执行：bash $0 login"
    info "登录成功"
    compose up -d
}

cmd_install() {
    require_root
    ensure_docker
    mkdir -p "$INSTALL_DIR"/{data,plugins}
    chmod 700 "$INSTALL_DIR"

    if [[ -f "$INSTALL_DIR/ss-local.json" ]] && [[ -z "${SS_URI:-}${SS_SERVER:-}" ]]; then
        info "已存在代理配置，保留（如需修改：bash $0 proxy）"
    else
        collect_ss
        write_ss_config
    fi
    write_compose
    info "拉取镜像..."
    compose pull
    write_pgm_config
    sync_plugins
    check_proxy || die "代理检查未通过，请确认服务器 B 的 ssserver 配置、端口和防火墙（需放行 A 的 IP）"

    if [[ -n "${NINNGYO_SKIP_LOGIN:-}" ]]; then
        info "已跳过登录，之后执行：bash $0 login"
        return
    fi
    cmd_login
    echo
    info "安装完成，目录：${INSTALL_DIR}"
    info "在任意对话发送 ,help 查看命令，,wf 查看转发插件用法"
}

cmd_proxy() {
    require_root
    require_installed
    unset SS_SERVER
    collect_ss
    write_ss_config
    compose up -d --force-recreate ss-local
    check_proxy
}

cmd_plugins() {
    require_installed
    if git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "$SCRIPT_DIR" pull --ff-only || warn "git pull 失败，使用本地已有的插件文件"
    fi
    sync_plugins
    compose restart pagermaid
}

cmd_update() {
    require_installed
    write_compose
    compose pull
    sync_plugins
    compose up -d
}

cmd_uninstall() {
    require_root
    require_installed
    confirm "停止并删除 ${PROJECT} 的容器和网络？" || exit 0
    compose down
    if confirm "同时删除 ${INSTALL_DIR}（包含登录会话 session，删除后需重新登录）？"; then
        rm -rf "$INSTALL_DIR"
        info "已删除 ${INSTALL_DIR}"
    else
        info "保留了 ${INSTALL_DIR}"
    fi
}

usage() {
    cat <<EOF
用法：bash $0 [命令]

  install     安装/修复（默认）：配置代理、生成配置、拉镜像、检查链路、登录
  login       登录 Telegram 账号（会话失效时也用它）
  check       检查代理出口、Telegram 连通性和网络隔离
  proxy       修改服务器 B 的代理配置
  plugins     git pull 本仓库并同步自定义插件，然后重启
  update      更新镜像和插件
  start | stop | restart | status | logs
  uninstall   卸载

可用环境变量（用于免交互安装）：
  NINNGYO_DIR=${INSTALL_DIR}  安装目录
  SS_URI=ss://...             或 SS_SERVER / SS_PORT / SS_METHOD / SS_PASSWORD
  API_ID / API_HASH           Telegram API
  PGM_QRCODE_LOGIN=True       使用扫码登录
  NINNGYO_SKIP_LOGIN=1        安装时跳过登录
EOF
}

CMD="${1:-install}"
case "$CMD" in
    install) cmd_install ;;
    login) require_root && cmd_login ;;
    check) require_installed && check_proxy ;;
    proxy) cmd_proxy ;;
    plugins) require_root && cmd_plugins ;;
    update) require_root && cmd_update ;;
    start) require_installed && compose up -d ;;
    stop) require_installed && compose stop ;;
    restart) require_installed && compose restart ;;
    status) require_installed && compose ps ;;
    logs) require_installed && compose logs -f --tail 100 "${2:-pagermaid}" ;;
    uninstall) cmd_uninstall ;;
    -h | --help | help) usage ;;
    *) usage && exit 1 ;;
esac
