# DEEPSEEK-TB

本机(noilinux@Ubuntu 20.04)的流量观测工作区。**纯本地 git 仓库,没有远端** —— 数据只在这台机器上。

## 目录

| 路径 | 是什么 |
|---|---|
| `traffic/traffic.py` | 无 root 的网卡流量工具:`ifaces` / `rate` / `clash` / `record` / `daily`。数据源 `/proc/net/dev` + Clash 控制端口 9090 |
| `traffic/flow.py` | 把 Clash 连接按**进程/网站**归因:`sample` / `report` / `show`,数据源 Clash `/connections` + `ss` 反查 `sourcePort` |
| `traffic/systemd/` | 两个 `systemd --user` 定时器的 unit 与 `install.sh`:<br>`traffic-record.timer` 每 5 分钟记一次网卡累计计数器<br>`traffic-flow.timer` 每分钟记一次连接级明细 |
| `traffic/traffic.csv` · `traffic/flows.jsonl` | 采样数据(**已被 `.gitignore` 排除**,见下) |

仓库里只有**代码和配置**。根目录那些一次性抓取存证(Token Harbor 的 `*.html`、`p_valid.json`、`headers.txt`、`connect.sh`、`dsh-proxy.sh`)**留在磁盘上但被 `.gitignore` 排除**,不进版本控制;删掉也不影响 `traffic/` 里的工具。

## 常用命令

```bash
cd traffic

python3 traffic.py sum 2        # 一条命令看全:网卡累计 / 实时速率 / 按天 / Clash,每栏只列前 10 个
python3 traffic.py daily        # 按天用量
python3 traffic.py ifaces       # 各网卡自开机累计
python3 traffic.py rate 3       # 实时速率

python3 flow.py report 20       # 进程/网站/Chrome/规则 排行(全历史)
python3 flow.py report --json   # 同一份报告,JSON
python3 flow.py show            # 此刻的活动连接

python3 flow.py sample          # = 定时器每次做的事;手动补一次采样
```

## 定时器

```bash
systemctl --user list-timers 'traffic-*'      # 看下次触发
systemctl --user start traffic-flow.service  # 立刻采一次(不等定时器)
journalctl --user -u traffic-flow.service -n 20 --no-pager
```

重装/换机:在**你自己的终端**里跑 `traffic/systemd/install.sh`(沙箱里 `systemctl --user` 必失败,原因见下)。

## 三个已经踩过的坑

1. **沙箱内 `systemctl --user` / `crontab` 都不可用**:私有 socket 的对端认证依赖 PID 命名空间,沙箱是独立 PID ns,宿主 systemd 在 `/proc` 找不到该 PID。要装/改单元,得在宿主的桌面终端里做。
2. **进程名要靠 `sourcePort` 反查**:Clash `/connections` 的 `metadata.process` 默认是空的(缺 `find-process-mode: always`),得拿 `metadata.sourcePort` 去 `ss -tnp` 对应;而 `/proc` 只有在宿主命名空间才看得到别的进程。所以 `flow.py sample` 必须由 systemd --user 跑,手动在沙箱里跑会 `attributed=false`。
3. **字节级统计不需要 root**:`ss -tin` 打印每条 TCP socket 的 `bytes_sent` / `bytes_received`(纯无特权可读)。但只有 TCP 有,QUIC/UDP 只能看网卡总量。`tcpdump` / `nethogs` 则需要 `CAP_NET_RAW`,沙箱里被拒(`CapEff=0`);另外沙箱是独立 user namespace,用不了 setuid,所以**在沙箱里** `sudo` 必失败,你自己终端里的 `sudo` 是正常的。

## 直连流量(不经 Clash)也进了统计

`flow.py sample` 除了读 Clash `/connections`,还会跑 `ss -tineH` 拿**所有 TCP socket** 的内核字节计数,把「本地端口不在 Clash `sourcePort` 集合里、对端不是回环、属主不是 clash」的 socket 记成直连,写进同一行的 `direct[]`。`report` 因此多两张表:

- **直连进程(TCP,不经 Clash)**:按进程
- **全部进程(代理+直连)**:代理与直连合在一起

三个限制:

1. 只有 TCP 有内核字节计数;QUIC/纯 UDP 的直连看不到,只能用网卡 `iface_delta` 兜底。
2. 归因要读 `/proc/<pid>/fd`,必须在宿主命名空间跑(systemd --user 满足);沙箱里会得到 `(未归因)`。
3. 第一次见到某个 socket 时按「全生命周期累计」计入,所以重装/重启/清空 `flow_state.json` 后的第一次采样会有一个大尖峰;之后都是差量。Clash 自己的出口 socket 靠「属主进程名含 clash」排除,拿不到属主时可能被重复计入,这种条目会显示为 `(未归因)`。

## 为什么数据文件不进 git

`traffic.csv` / `flows.jsonl` 是**定时器持续追加**的采样数据(`flows.jsonl` 约 7~12 MB/天),append-only 文件每提交一次就存一整份新快照,几天就能把仓库撑到几百 MB。它们躺在原目录里照常使用,只是不纳入版本控制。

真要纳管,把 `.gitignore` 里对应四行注释掉,并考虑 `git gc` / 定期轮转切分。
