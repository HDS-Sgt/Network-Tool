#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机流量分析工具(无需 root,无需 vnstat/nethogs)。

数据源:
  1) /proc/net/dev        —— 内核按网卡的累计字节数(自开机,不含"每天")
  2) Clash 控制端口 9090  —— 实时速率 / 活跃连接(按域名、目标地址)

为什么需要 record + daily:/proc/net/dev 里的计数器是"自开机"的,内核不保存历史,
sysstat(sar)默认也没开网卡统计。所以想要"按天/按月"的用量,只能自己定期把累计值
存下来(record),再对相邻快照求正增量(daily)。全程普通用户权限,不需要 root。

用法:
  python3 traffic.py ifaces [LO_ALSO]   # 各网卡累计流量(自开机)
  python3 traffic.py rate [SECONDS]     # 采样 N 秒(默认 3)算实时速率
  python3 traffic.py clash              # Clash 实时速率 + 活跃连接按域名/进程汇总
  python3 traffic.py record             # 追加一条快照到 traffic.csv(配合定时任务)
  python3 traffic.py daily              # 由 traffic.csv 汇总每日用量
  python3 traffic.py sum [SECONDS]      # 一条命令汇总:网卡累计/实时速率/按天/Clash,每栏只列前 10 个
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


def pad(s, width):
    """按终端显示宽度左对齐(中文算 2 列),避免 %-20s 在中英混排时错位。"""
    import unicodedata
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
    return s + " " * max(0, width - w)


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


def rate_rows(sec=3.0):
    """采样 sec 秒,返回 ([(网卡, rx/s, tx/s)], 实际秒数);空闲网卡不返回。"""
    a = usable(snap())
    t0 = time.time()
    time.sleep(sec)
    b = usable(snap())
    dt = time.time() - t0
    rows = []
    for k in sorted(b):
        if k not in a:
            continue
        drx = (b[k][0] - a[k][0]) / dt
        dtx = (b[k][1] - a[k][1]) / dt
        if drx or dtx:
            rows.append((k, drx, dtx))
    return rows, dt


def cmd_rate(sec=3.0):
    rows, dt = rate_rows(sec)
    print("%.1f 秒采样窗口,实时速率:" % dt)
    if not rows:
        print("  (全部网卡都空闲)")
        return
    for k, drx, dtx in rows:
        print("  %-20s ↓ %10s/s   ↑ %10s/s" % (k, human(drx), human(dtx)))


def curl_json(path, timeout=5):
    try:
        raw = subprocess.check_output(["curl", "-s", "--max-time", str(timeout), CLASH_API + path],
                                      stderr=subprocess.DEVNULL)
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:  # Clash 没开 / 端口变了
        return {"__error__": str(e)}


def clash_rates(max_lines=3, timeout=3):
    """流式读 Clash /traffic 的前 max_lines 条报文,返回 [(down, up)] 字节/秒。"""
    out = []
    try:
        p = subprocess.Popen(["curl", "-s", "-N", "--max-time", str(timeout), CLASH_API + "/traffic"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for line in iter(p.stdout.readline, b""):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            out.append((d.get("down", 0), d.get("up", 0)))
            if len(out) >= max_lines:
                p.kill()
                break
    except Exception as e:
        print("  /traffic 读取失败:", e)
    return out


def clash_conns():
    """Clash /connections 的连接列表(连不上时为空列表)。"""
    return (curl_json("/connections") or {}).get("connections") or []


def cmd_clash():
    ver = curl_json("/version")
    if "__error__" in ver:
        print("连不上 Clash API %s:%s" % (CLASH_API, ver["__error__"]))
        print("提示:确认 clash 在跑(ss -ltnp | grep 9090),或改 CLASH_API 环境变量。")
        return
    print("Clash: version=%s premium=%s" % (ver.get("version"), ver.get("premium")))

    # 实时速率: /traffic 是流式接口,取两条以上报文即得 up/down 字节每秒
    print("\n-- 实时速率(Clash 视角,单位 字节/秒)--")
    for down, up in clash_rates(3):
        print("  ↓ %10s/s   ↑ %10s/s" % (human(down), human(up)))

    conns = clash_conns()
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


def daily_totals():
    """{day: 收+发字节}。对同一网卡的相邻快照求正增量(负数=重启回绕,忽略)。"""
    if not os.path.exists(CSV):
        return None
    last, per_day = {}, Counter()
    with open(CSV) as f:
        for row in csv.DictReader(f):
            cur = int(row["rx"]) + int(row["tx"])
            day = row["iso"][:10]
            key = row["iface"]
            if key in last:
                delta = cur - last[key]
                if delta > 0:
                    per_day[day] += delta
            last[key] = cur
    return dict(per_day)


def cmd_daily():
    per_day = daily_totals()
    if per_day is None:
        print("还没有 %s。先加定时任务跑 `record`,例如 systemd --user timer 或 cron 每 5 分钟一次。" % CSV)
        return
    if not per_day:
        print("样本不足(至少需要同一张网卡的两个快照)。")
        return
    print("%-12s %12s" % ("日期", "用量(收+发)"))
    for day in sorted(per_day):
        print("%-12s %12s" % (day, human(per_day[day])))


def cmd_sum(sec=2.0, top=10):
    """一条命令看全:网卡累计 / 实时速率 / 按天 / Clash。每栏只列前 top 个。"""
    line = "=" * 70
    print(line)
    print("流量汇总  %s    (每栏只列前 %d 个)" % (time.strftime("%Y-%m-%d %H:%M:%S"), top))
    print(line)

    # 1) 自开机累计
    data = usable(snap())
    tot_rx = sum(v[0] for v in data.values())
    tot_tx = sum(v[1] for v in data.values())
    tot = tot_rx + tot_tx
    print("\n-- 1. 自开机累计(重启清零,按合计排)--")
    for k, (rx, tx) in sorted(data.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:top]:
        share = (rx + tx) / tot * 100 if tot else 0
        print("  %s ↓ %10s  ↑ %10s  合计 %10s  %5.1f%%"
              % (pad(k, 20), human(rx), human(tx), human(rx + tx), share))
    print("  %s ↓ %10s  ↑ %10s  合计 %10s"
          % (pad("合计 %d 张" % len(data), 20), human(tot_rx), human(tot_tx), human(tot)))

    # 2) 实时速率
    rows, dt = rate_rows(sec)
    print("\n-- 2. 实时速率(%.1f 秒窗口,网卡视角)--" % dt)
    if not rows:
        print("  (全部网卡都空闲)")
    else:
        for k, drx, dtx in sorted(rows, key=lambda r: -(r[1] + r[2]))[:top]:
            print("  %-20s ↓ %10s/s  ↑ %10s/s" % (k, human(drx), human(dtx)))

    # 3) 按天(来自 traffic.csv,只能从装定时器那天开始)
    per_day = daily_totals()
    print("\n-- 3. 按天用量(traffic.csv)--")
    if per_day is None:
        print("  还没有 %s —— 先让 record 定时跑起来" % CSV)
    elif not per_day:
        print("  样本不足(至少需要同一张网卡的两个快照)")
    else:
        peak = max(per_day.values()) or 1
        for day, v in sorted(per_day.items(), key=lambda kv: -kv[1])[:top]:
            print("  %-12s %10s  %s" % (day, human(v), "#" * max(1, int(v / peak * 24))))
        print("  %s %10s  (%d 天合计)" % (pad("共计", 12), human(sum(per_day.values())), len(per_day)))

    # 4) Clash(连不上就跳过,不影响其它栏)
    print("\n-- 4. Clash 实时 --")
    ver = curl_json("/version")
    if "__error__" in ver:
        print("  连不上 Clash API(%s),跳过本栏 —— 确认 clash 在跑" % CLASH_API)
    else:
        print("  version=%s  premium=%s" % (ver.get("version"), ver.get("premium")))
        rates = clash_rates(1)
        if rates:
            down, up = rates[-1]
            print("  实时 ↓ %s/s  ↑ %s/s" % (human(down), human(up)))
        conns = clash_conns()
        up_sum = sum(c.get("upload", 0) for c in conns)
        dn_sum = sum(c.get("download", 0) for c in conns)
        print("  活跃连接 %d 条,累计 ↓ %s  ↑ %s" % (len(conns), human(dn_sum), human(up_sum)))
        by_host, by_proc = Counter(), Counter()
        for c in conns:
            m = c.get("metadata") or {}
            v = c.get("upload", 0) + c.get("download", 0)
            by_host[m.get("host") or m.get("destinationIP") or "?"] += v
            by_proc[m.get("processPath") or m.get("process") or "(进程未知,需 find-process-mode: always)"] += v
        if by_host:
            print("  目标 top%d:" % top)
            for k, v in by_host.most_common(top):
                print("    %10s  %s" % (human(v), k))
            print("  进程 top%d:" % top)
            for k, v in by_proc.most_common(top):
                print("    %10s  %s" % (human(v), k))

    print("\n提示:连接/进程级历史用 `flow.py report`;本栏「按天」只有装了 record 定时器之后才有。")


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
    elif mode == "sum":
        cmd_sum(float(args[1]) if len(args) > 1 else 2.0)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
