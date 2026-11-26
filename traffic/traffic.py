#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机流量分析工具(无需 root,无需 vnstat/nethogs)。

数据源:
  1) /proc/net/dev        —— 内核按网卡的累计字节数(自开机,不含"每天")
  2) Clash 控制端口 9090  —— 实时速率 / 活跃连接(按域名、目标地址)

本机现实(2026-11 实测):sudo 不可用、sysstat 的 ENABLED="false" 且 /var/log/sysstat 为空
=> 没有任何历史流量数据,想要"按天/按月"必须先自己采样(record + daily)。

用法:
  python3 traffic.py ifaces [LO_ALSO]   # 各网卡累计流量(自开机)
  python3 traffic.py rate [SECONDS]     # 采样 N 秒(默认 3)算实时速率
  python3 traffic.py clash              # Clash 实时速率 + 活跃连接按域名/进程汇总
  python3 traffic.py record             # 追加一条快照到 traffic.csv(配合定时任务)
  python3 traffic.py daily              # 由 traffic.csv 汇总每日用量
环境变量 TRAFFIC_CSV 可指定 CSV 路径(默认与本脚本同目录的 traffic.csv)。
"""
import csv
import json
import os
import subprocess
import sys
import time
from collections import Counter

CSV = os.environ.get("TRAFFIC_CSV", os.path.join(os.path.dirname(os.path.abspath(__file__)), "traffic.csv"))
CLASH_API = os.environ.get("CLASH_API", "http://127.0.0.1:9090")
SKIP = {"lo"}
GIB = 1024 ** 3
MIB = 1024 ** 2


def human(n):
    n = float(n)
    for unit, div in (("GiB", GIB), ("MiB", MIB), ("KiB", 1024.0)):
        if abs(n) >= div:
            return "%.2f %s" % (n / div, unit)
    return "%.0f B" % n


def snap():
    """返回 {iface: (rx_bytes, tx_bytes)}"""
    out = {}
    with open("/proc/net/dev") as f:
        for line in f.read().splitlines()[2:]:
            name, rest = line.split(":")
            fld = rest.split()
            out[name.strip()] = (int(fld[0]), int(fld[8]))
    return out


def usable(d):
    return {k: v for k, v in d.items() if k not in SKIP}


def cmd_ifaces(show_lo=False):
    data = snap() if show_lo else usable(snap())
    if not data:
        print("(无网卡数据)")
        return
    trx = ttx = 0
    print("%-20s %12s %12s %12s" % ("网卡", "收(RX)", "发(TX)", "合计"))
    for k in sorted(data):
        rx, tx = data[k]
        trx += rx
        ttx += tx
        print("%-20s %12s %12s %12s" % (k, human(rx), human(tx), human(rx + tx)))
    print("%-20s %12s %12s %12s" % ("总计", human(trx), human(ttx), human(trx + ttx)))
    print("\n注:以上为自开机累计,重启即清零;要按天累计用 record + daily。")


def cmd_rate(sec=3.0):
    a = usable(snap())
    t0 = time.time()
    time.sleep(sec)
    b = usable(snap())
    dt = time.time() - t0
    print("%.1f 秒采样窗口,实时速率:" % dt)
    busy = False
    for k in sorted(b):
        if k not in a:
            continue
        drx = (b[k][0] - a[k][0]) / dt
        dtx = (b[k][1] - a[k][1]) / dt
        if drx or dtx:
            busy = True
            print("  %-20s ↓ %10s/s   ↑ %10s/s" % (k, human(drx), human(dtx)))
    if not busy:
        print("  (全部网卡都空闲)")


def curl_json(path, timeout=5):
    try:
        raw = subprocess.check_output(["curl", "-s", "--max-time", str(timeout), CLASH_API + path],
                                      stderr=subprocess.DEVNULL)
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:  # Clash 没开 / 端口变了
        return {"__error__": str(e)}


def cmd_clash():
    ver = curl_json("/version")
    if "__error__" in ver:
        print("连不上 Clash API %s:%s" % (CLASH_API, ver["__error__"]))
        print("提示:确认 clash 在跑(ss -ltnp | grep 9090),或改 CLASH_API 环境变量。")
        return
    print("Clash: version=%s premium=%s" % (ver.get("version"), ver.get("premium")))

    # 实时速率: /traffic 是流式接口,取两次报文即得 up/down 字节每秒
    print("\n-- 实时速率(Clash 视角,单位 字节/秒)--")
    try:
        p = subprocess.Popen(["curl", "-s", "-N", "--max-time", "3", CLASH_API + "/traffic"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        shown = 0
        for line in iter(p.stdout.readline, b""):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                print("  ↓ %10s/s   ↑ %10s/s" % (human(d.get("down", 0)), human(d.get("up", 0))))
                shown += 1
            except ValueError:
                pass
            if shown >= 3:
                p.kill()
                break
    except Exception as e:
        print("  /traffic 读取失败:", e)

    conn = curl_json("/connections")
    conns = (conn or {}).get("connections") or []
    print("\n-- 活跃连接 %d 条 --" % len(conns))
    if not conns:
        return
    by_host = Counter()
    by_proc = Counter()
    up = dn = 0
    for c in conns:
        m = c.get("metadata") or {}
        u, d = c.get("upload", 0), c.get("download", 0)
        up += u
        dn += d
        key = m.get("host") or m.get("destinationIP") or "?"
        by_host[key] += d + u
        by_proc[m.get("processPath") or m.get("process") or "(进程未知,需 find-process-mode: always)"] += d + u
    print("  活跃连接累计: ↓ %s  ↑ %s" % (human(dn), human(up)))
    print("  按目标(top8):")
    for k, v in by_host.most_common(8):
        print("    %10s  %s" % (human(v), k))
    print("  按进程(top5):")
    for k, v in by_proc.most_common(5):
        print("    %10s  %s" % (human(v), k))


def cmd_record():
    new = not os.path.exists(CSV)
    t = time.time()
    data = usable(snap())
    with open(CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["epoch", "iso", "iface", "rx", "tx"])
        for k, (rx, tx) in sorted(data.items()):
            w.writerow([int(t), time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)), k, rx, tx])
    print("已记录 %d 张网卡 @ %s -> %s" % (len(data), time.strftime("%F %T"), CSV))


def cmd_daily():
    if not os.path.exists(CSV):
        print("还没有 %s。先加定时任务跑 `record`,例如 systemd --user timer 或 cron 每 5 分钟一次。" % CSV)
        return
    last, per_day = {}, Counter()
    with open(CSV) as f:
        for row in csv.DictReader(f):
            key = (row["iface"],)
            cur = int(row["rx"]) + int(row["tx"])
            day = row["iso"][:10]
            if key in last:
                piface, ptot = last[key]
                delta = cur - ptot  # 网卡重启会让计数器回绕,负值忽略
                if delta > 0:
                    per_day[day] += delta
            last[key] = (row["iface"], cur)
    if not per_day:
        print("样本不足(至少需要同一张网卡的两个快照)。")
        return
    print("%-12s %12s" % ("日期", "用量(收+发)"))
    for day in sorted(per_day):
        print("%-12s %12s" % (day, human(per_day[day])))


def main():
    args = sys.argv[1:]
    mode = args[0] if args else "ifaces"
    if mode == "ifaces":
        cmd_ifaces("-lo" in args)
    elif mode == "rate":
        cmd_rate(float(args[1]) if len(args) > 1 else 3.0)
    elif mode == "clash":
        cmd_clash()
    elif mode == "record":
        cmd_record()
    elif mode == "daily":
        cmd_daily()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
