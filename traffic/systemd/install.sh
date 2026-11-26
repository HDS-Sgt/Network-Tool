#!/bin/sh
# 安装「流量历史采样」用户定时器(systemd --user 路由)。
# 必须在你自己的桌面终端里运行 —— 沙箱/AI 会话里 systemctl --user 会因 PID 命名空间
# 导致私有 socket 对端认证失败(报 "Failed to connect to bus: 没有可用的数据")。
# 也要在这里跑才有意义:连接级归因要靠 /proc 反查 socket 属主,宿主命名空间才看得到进程名。
set -e
SRC=$(cd "$(dirname "$0")" && pwd)
DEST="$HOME/.config/systemd/user"
mkdir -p "$DEST"
cp -v "$SRC/traffic-record.service" "$SRC/traffic-record.timer" \
      "$SRC/traffic-flow.service" "$SRC/traffic-flow.timer" "$DEST/"
systemctl --user daemon-reload
systemctl --user enable --now traffic-record.timer traffic-flow.timer
echo
systemctl --user list-timers 'traffic-*' --no-pager
echo
echo "网卡累计日报: python3 $SRC/../traffic.py daily"
echo "进程/网站归因: python3 $SRC/../flow.py report 20"
echo "数据文件:     $SRC/../traffic.csv 、 $SRC/../flows.jsonl"
echo "注销后仍要采样(可选,需要 polkit 授权): loginctl enable-linger $USER"
