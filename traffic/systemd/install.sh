#!/bin/sh
# 安装/卸载「流量持续采样」的 systemd 用户级定时器。
#
# 用法:
#   ./install.sh              安装并立即启用两个 timer
#   ./install.sh --dry-run    只生成替换好路径的 unit 到临时目录,不碰 systemd
#   ./install.sh --uninstall  停用并删除这两个 timer 的 unit(数据文件保留)
#
# 为什么要有这个脚本:unit 文件里的 @TRAFFIC_DIR@ 是占位符,必须替换成
# 本仓库 traffic/ 目录的绝对路径,ExecStart 才能找到脚本。仓库搬家后
# 重跑一次即可(install.sh 就地覆盖旧 unit)。
#
# 必须在**你自己的桌面/登录终端**里运行,不要在有独立 PID 命名空间的
# 容器里跑:那样 systemctl --user 连不上宿主 bus,而且装好了服务也读不到
# 宿主的 /proc(进程名会全是空的)。
set -eu

SRC=$(cd "$(dirname "$0")" && pwd)          # 本目录:traffic/systemd
TRAFFIC_DIR=$(cd "$SRC/.." && pwd)          # unit 里要引用的:traffic/
DEST="$HOME/.config/systemd/user"
UNITS="traffic-record.service traffic-record.timer traffic-flow.service traffic-flow.timer"

PY=/usr/bin/python3
[ -x "$PY" ] || PY=$(command -v python3 || true)
[ -f "$TRAFFIC_DIR/traffic.py" ] && [ -f "$TRAFFIC_DIR/flow.py" ] || {
  echo "在 $TRAFFIC_DIR 里找不到 traffic.py / flow.py,目录结构不对" >&2; exit 1; }
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "找不到 python3(需要绝对路径写进 unit)" >&2
  exit 1
fi

# 用临时目录生成替换后的 unit,避免把占位符版本拷进 systemd
render() {
  out=$1
  mkdir -p "$out"
  for u in $UNITS; do
    sed -e "s|@TRAFFIC_DIR@|$TRAFFIC_DIR|g" \
        -e "s|^ExecStart=/usr/bin/python3 |ExecStart=$PY |" \
        "$SRC/$u" > "$out/$u"
  done
}

case "${1:-}" in
  --dry-run|-n)
    OUT=$(mktemp -d)
    render "$OUT"
    echo "已生成到 $OUT:"
    grep -H '^ExecStart=' "$OUT"/*.service
    echo "(临时目录,未安装)"
    ;;

  --uninstall)
    systemctl --user disable --now traffic-record.timer traffic-flow.timer 2>/dev/null || true
    for u in $UNITS; do rm -f "$DEST/$u"; done
    systemctl --user daemon-reload
    echo "已卸载两个 timer;数据文件 traffic.csv / flows.jsonl 未删除。"
    ;;

  "")
    mkdir -p "$DEST"
    render "$DEST"
    systemctl --user daemon-reload
    systemctl --user enable --now traffic-record.timer traffic-flow.timer
    echo
    systemctl --user list-timers 'traffic-*' --no-pager

    echo
    echo "查看结果:"
    echo "  python3 $TRAFFIC_DIR/traffic.py daily        # 按天用量"
    echo "  python3 $TRAFFIC_DIR/traffic.py sum 2        # 一屏看全"
    echo "  python3 $TRAFFIC_DIR/flow.py report 20       # 进程/网站归因"
    echo "  journalctl --user -u traffic-flow.service -n 20"
    echo
    echo "数据文件: $TRAFFIC_DIR/traffic.csv 与 $TRAFFIC_DIR/flows.jsonl"
    echo "开机不登录也要采样(可选,需要 polkit 授权): loginctl enable-linger $USER"
    echo "停止:  ./install.sh --uninstall"
    ;;

  *)
    echo "未知参数: $1" >&2
    echo "用法: $0 [--dry-run|--uninstall]" >&2
    exit 2
    ;;
esac
