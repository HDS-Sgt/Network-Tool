# Network-Tool

**无需 root 的本机流量观测工具**:统计每张网卡的累计/实时/按天流量,并借 Clash 控制端口把流量归因到**进程、网站、规则**;不经代理的**直连 TCP** 流量也通过 `ss` 的内核字节计数一起记进来。

纯 Python 3 标准库,零依赖,不需要 `vnstat` / `nethogs` / `tcpdump`,不需要 `CAP_NET_RAW`,也不需要 `sudo`。

适用环境:Linux(依赖 `/proc`)+ 正在运行且开放 RESTful 控制端口的 **Clash / Clash Premium / mihomo** + 可选 systemd 用户级定时器。

```
traffic/traffic.py   网卡视角:累计 / 实时速率 / 按天 / 汇总
traffic/flow.py      连接视角:按进程 / 网站 / 规则归因,含直连
traffic/systemd/     两个 systemd --user 定时器 + 安装脚本
tests/               纯假输入的单测(不碰 Clash、不改真实数据)
```

---

## 1. 功能

### `traffic.py` —— 网卡视角

| 子命令 | 作用 | 数据源 |
|---|---|---|
| `ifaces` | 各网卡自开机以来的累计收/发(默认排除 `lo`,加 `-lo` 包含) | `/proc/net/dev` |
| `rate [秒数]` | 采样 N 秒(默认 3)算每张网卡的实时速率 | `/proc/net/dev` 两次快照 |
| `clash` | Clash 的实时上下行 + 活跃连接按**域名/进程**汇总 | Clash `/traffic`、`/connections` |
| `record` | 追加一条「此刻所有网卡累计计数器」快照到 `traffic.csv`,给定时器用 | `/proc/net/dev` |
| `daily` | 由 `traffic.csv` 相邻快照求正增量,输出**按天**收/发与合计 | `traffic.csv` |
| `sum [秒数]` | 一屏四栏汇总:**开机累计 / 实时速率 / 按天用量 / Clash 概览**,每栏默认只列前 10 大头 | 以上全部 |

- 所有输出都自动选 `B / KiB / MiB / GiB / TiB` 单位;`sum` 的中文表格按东亚字符宽度对齐,不会串列。
- `sum` 是**降级容错**的:`traffic.csv` 不存在就不显示「按天」栏,Clash 连不上就跳过 Clash 栏,其余照常输出。
- `daily` 只累加**正增量**:`/proc/net/dev` 是自开机累计,机器重启后归零,负增量会被忽略;`daily` 同时也忽略计数器回绕。

### `flow.py` —— 连接 / 进程视角

| 子命令 | 作用 |
|---|---|
| `sample` | 采一次样:读 Clash `/connections`,与上次快照求差量,归因到进程与网站,追加一行 JSON 到 `flows.jsonl`。定时器每分钟跑它 |
| `report [N]` | 汇总 `flows.jsonl` 全部历史,输出**窗口/总量/进程/网站/Chrome 站点/目标 host/命中规则/直连进程/全部进程** 多张排行表(每张列前 N 行,默认 15) |
| `report --json` | 同上,但把结果写到 `flow_report.json` 并在 stdout 打印 JSON(字段见 §5) |
| `show` | 只看此刻的活动连接与实时归属,不落盘、不改状态 |

`report` 里几个需要解释的口径:

- **进程**:`proc` 是进程标签;`proc_src=clash` 表示来自 Clash 自报的 `metadata.process`,`proc_src=ss` 表示靠 `sourcePort` 反查 `ss` 得到的。
- **网站 / site**:从连接 host 取 eTLD+1 的近似值(内置 `co.uk`、`com.cn`、`github.io`、`workers.dev` 等两级后缀表),`ika.zip`、`github.io` 这类不会被错误截断。
- **直连进程(TCP)**:本地端口不在 Clash `sourcePort` 集合里、对端不是回环、属主不是 Clash 自己的 socket → 判定为不经代理的直连。
- **全部进程(代理+直连)**:把上面两张表加起来,这才是「这个进程一共用了多少」。
- **Chrome 站点**:Chrome 的网络流量汇聚在 `NetworkService` utility 进程里,系统层面无法区分标签页,所以这张表是「Chrome 进程的流量按目标站点拆分」——**能到站点,不能到标签页**。

### 定时器

| 单元 | 频率 | 做什么 | 产出 |
|---|---|---|---|
| `traffic-record.timer` | 每 5 分钟(`OnCalendar=*:0/5`,`Persistent=true`) | `traffic.py record` | `traffic/traffic.csv` |
| `traffic-flow.timer` | 每分钟(`OnCalendar=*:*:00`) | `flow.py sample` | `traffic/flows.jsonl` + `flow_state.json` |

`Persistent=true` 只加在 record 上:网卡计数器在关机期间不会增长,补跑没有意义;而 flow 的差量强依赖「上次快照的累计值」,停机期间连接早已消失,补跑反而会制造假差量。

---

## 2. 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Linux,可读 `/proc/net/dev`、`/proc/<pid>/fd` | 全程不依赖内核模块或 eBPF |
| Python | ≥ 3.6(推荐 3.8+) | 只用标准库:`csv` `json` `os` `re` `subprocess` `sys` `time` `urllib.request` `unicodedata`。**无第三方包** |
| iproute2 | `ss` 可用,且支持 `-t` `-i` `-n` `-e` `-p` `-H` | `-H`(去掉表头)需要 iproute2 ≥ 4.16;`ss -tin` 的 `bytes_sent`/`bytes_received` 在 4.x 及以上内核均有 |
| Clash | 运行中,`external-controller` 对本机可达(默认 `127.0.0.1:9090`) | 需要 `/traffic`、`/connections` 两个接口,Clash Premium 与 mihomo 都有;原版 Clash 的部分旧版没有 `/traffic` |
| Clash 配置 | **`secret` 留空**(或只在本机回环上且不设 secret) | 本工具不发 `Authorization` 头。设了 secret 就只读不到 `/traffic`、`/connections` |
| 权限 | 普通用户,**不需要 root / sudo** | 但 root 进程的 `/proc/<pid>/fd` 读不到 → 它们的直连流量会显示为 `(未归因)` |
| 命名空间 | 与目标进程相同的 **PID 命名空间**(且能读 `/proc`) | 容器 / 独立 PID ns 里跑只能拿到 site 与 host 级统计,进程名为空(`attributed=false`) |
| systemd(可选) | `systemd --user` 会话 | 只为了自动采样;不装也能手动 `record` / `sample` |
| 磁盘 | `flows.jsonl` 视连接数约 **7~12 MB/天**(≈200~350 MB/月) | 上游连接数越多越大;建议配 logrotate 或定期截断 |

### Clash 侧:让进程名更准(可选)

默认情况下本工具靠 `sourcePort` 反查进程,已经够用。若想让 Clash **自己**报出进程名,在 Clash 配置里加一行并重启 Clash:

```yaml
find-process-mode: always
```

重启后 `/connections` 的 `metadata.process` / `processPath` 会有值,报告里会出现 `proc_src=clash` 的条目。**不改也能用**——只是少一路交叉验证。

> 重启 Clash 会瞬断正在走的连接。工具本身从不重启或改写 Clash 配置,这一步请自行决定。

---

## 3. 安装

```bash
git clone https://github.com/HDS-Sgt/Network-Tool.git
cd Network-Tool
python3 traffic/traffic.py sum 2     # 先看能不能跑
```

不需要 `pip install`、不需要虚拟环境、不需要编译。**目录可以随便放**,数据文件默认写在脚本同一目录;要改位置用环境变量(见 §4)。

### 装成定时采集(可选,推荐)

```bash
./traffic/systemd/install.sh
```

脚本做的事:

1. 把 4 个 unit 拷到 `~/.config/systemd/user/`,并把模板里的 `@TRAFFIC_DIR@` 替换成**本仓库 `traffic/` 目录的绝对路径**(所以仓库搬家后要重跑一次);
2. `systemctl --user daemon-reload`;
3. `systemctl --user enable --now traffic-record.timer traffic-flow.timer`;
4. 打印 `list-timers 'traffic-*'` 与后续常用命令。

检查与排障:

```bash
systemctl --user list-timers 'traffic-*' --no-pager   # 下次触发时间
systemctl --user start traffic-flow.service           # 立刻采一次,不等定时器
journalctl --user -u traffic-flow.service -n 20       # 看日志
systemctl --user disable --now traffic-flow.timer     # 停掉
```

**注意**:`systemd --user` 单元是「登录后」才起来的。默认 `Linger=no` 时,开机停在登录界面、或只通过 SSH 不建立用户会话,定时器不会跑。要让它脱离登录常驻:

```bash
loginctl enable-linger "$USER"     # 一般需要 polkit 授权或 sudo
loginctl show-user "$USER" -p Linger
```

不需要自动采集的话,跳过整节即可——所有子命令都能手动跑。

---

## 4. 用法与环境变量

```bash
cd traffic

python3 traffic.py sum 2                 # 一条命令看全,实时速率取 2 秒
python3 traffic.py ifaces                # 各网卡自开机累计
python3 traffic.py rate 5                # 5 秒实时速率
python3 traffic.py clash                 # Clash 实时 + 活跃连接汇总
python3 traffic.py record                # 手动补一条快照(定时器做同样的事)
python3 traffic.py daily                 # 按天用量

python3 flow.py sample                   # 手动补一次连接采样
python3 flow.py report 20                # 每张表列前 20 行(全历史)
python3 flow.py report --json            # 写 flow_report.json 并打印 JSON
python3 flow.py show                    # 此刻的活动连接与实时归属
```

`report N` 的 **N 是「每张表显示多少行」,不是时间窗口**;`report` 永远聚合 `flows.jsonl` 里的全部历史。想看某个时间段,用 `FLOW_DIR` 指向一份裁剪过的 `flows.jsonl` 副本,或自行 `grep '"iso":"2026-11-26T0'` 后再喂给它。

| 环境变量 | 适用于 | 默认值 | 含义 |
|---|---|---|---|
| `CLASH_API` | 两者 | `http://127.0.0.1:9090` | Clash 控制端口基址(不要带结尾 `/`) |
| `TRAFFIC_CSV` | `traffic.py` | 脚本同目录的 `traffic.csv` | 快照 CSV 路径 |
| `FLOW_DIR` | `flow.py` | 脚本同目录 | `flows.jsonl` / `flow_state.json` / `flow_report.json` 的目录 |
| `PROC_ROOT` | `flow.py` | `/proc` | 读 `/proc` 的根,仅用于测试时造假目录 |

---

## 5. 数据格式

数据文件**默认不纳入版本控制**(见 `.gitignore`):它们是定时器持续追加的,append-only 文件每次提交都会存一整份新快照。

### `traffic.csv`(`traffic.py record` 产出)

每 5 分钟、每张网卡一行;数值是**自开机以来的累计字节**,不是增量。

```csv
epoch,iso,iface,rx,tx
1795477201,2026-11-24T07:40:01+0800,docker0,0,0
1795477201,2026-11-24T07:40:01+0800,enp2s0,15495769276,2246063470
1795477201,2026-11-24T07:40:01+0800,wlxf8ce212d282f,50531526715,5594837847
```

| 列 | 含义 |
|---|---|
| `epoch` | Unix 秒 |
| `iso` | 本地时间 ISO 8601 |
| `iface` | 网卡名 |
| `rx` / `tx` | 该网卡自开机累计的 接收 / 发送 字节数 |

### `flows.jsonl`(`flow.py sample` 产出)

一行一次采样(JSON,UTF-8),字段:

| 字段 | 类型 | 含义 |
|---|---|---|
| `ts` / `iso` | int / str | 采样时刻 |
| `ss_sockets` | int | `ss` 里能匹配到本机 Clash 端口的 socket 数 |
| `live_conns` | int | Clash `/connections` 当时的活跃连接数 |
| `attributed` | bool | 是否至少有一条连接拿到了进程名 |
| `iface` | dict | `{网卡: {"rx": 累计, "tx": 累计}}`,即同刻 `/proc/net/dev` 快照,用于对账 |
| `conns` | list | 经 Clash 的连接,每条是**本次相对上次的差量** |
| `direct` | list | 直连 TCP 连接,每条是**本次相对上次的差量** |
| `direct_conns` / `direct_attributed` | int / bool | 直连条数、直连是否有进程名 |

`conns[]` 每条:

```json
{"id":"...","proc":"google-chrome","proc_src":"ss","pid":302021,"tgid":302021,
 "comm":"chrome","cmd":"/opt/google/chrome/chrome --type=utility ...",
 "host":"example.com","site":"example.com","dest":"203.0.113.10:443",
 "src_port":"45678","type":"HTTP Connect","rule":"DomainSuffix",
 "chains":["节点A","节点B"],"up":1234,"down":56789}
```

`direct[]` 每条:同上但**没有** `host`/`site`/`dest`/`src_port`/`type`/`rule`/`chains`,改为 `peer`(对端 `ip:port`)、`remote_ip`、`local_port`、`ino`(socket inode)。

兼容性:早期版本写出的行没有 `direct` 字段,`report` 用 `rec.get("direct") or []` 读,照样能跑。

### `flow_report.json`(`report --json` 产出)

顶层键:`generated`、`window`(`first`/`last`/`samples`)、`totals`、`iface_delta`、`by_process`、`by_process_site`、`by_site`、`by_host`、`by_rule`、`chrome_sites`、`by_process_direct`、`by_proc_all`、`direct_totals`。

---

## 6. 工作原理

```
┌ traffic.py ─────────────────────────────────────────────┐
│ /proc/net/dev  ──snapshot──▶  traffic.csv (累计计数器)     │
│      │                                                  │
│      └─相邻快照作差(负值丢弃)─▶ 按天用量 / 实时速率         │
└─────────────────────────────────────────────────────────┘

┌ flow.py ────────────────────────────────────────────────┐
│ Clash /connections ──┬─ metadata.sourcePort ─▶ ss -tnp   │──▶ 进程(代理侧)
│ (每连接 upload/download 累计)                            │
│                      └─ host ─▶ site(eTLD+1 近似)        │
│                        │                                 │
│                        └─ 与本地上次快照作差 ─▶ 增量字节   │
│                                                          │
│ ss -tineH ─▶ 所有 TCP socket 的 inode + bytes_sent/recv   │
│      │                                                   │
│      ├─ 本地端口 ∈ Clash sourcePort 集合 ?  → 已计,跳过    │
│      ├─ 对端是回环 ?                        → 本地,跳过   │
│      └─ 其余 → 直连;inode ⇢ /proc/<pid>/fd ─▶ 属主进程     │
│                    (属主是 clash/mihomo ? → 跳过,防重复)  │
└─────────────────────────────────────────────────────────┘
```

三个要点:

1. **为什么不用 root**:`/proc/net/dev` 世界可读;`ss` 通过 netlink(`NETLINK_SOCK_DIAG`)读 socket 表,普通用户就能拿到,连 `tcp_info` 里的 `bytes_sent`/`bytes_received` 也可以。只有 `tcpdump`/`nethogs` 那种**要抓包**的方式才需要 `CAP_NET_RAW`。
2. **为什么必须做差量**:`bytes_sent`/`bytes_received` 是 **socket 生命周期累计**;Clash `/connections` 的 `upload`/`download` 也是连接累计。直接相加会把同一个 socket 每分钟重复计一遍,所以状态文件 `flow_state.json` 按连接 id / socket inode 记住上次的值,只写差量;计数器变小(重连、inode 复用)就当作新 socket 按全量计。
3. **为什么直连要排除 Clash 的 socket**:Clash 自己的出站连接在 `ss` 里表现为「本地端口不在 `sourcePort` 白名单」的 socket —— 如果不排除属主是 clash 的,这部分流量会与 `/connections` 里已记的代理流量**重复计算**。所以过滤条件里必须有「属主进程名不含 clash/mihomo」。

---

## 7. 限制与已知问题

1. **只有 TCP 能按字节归因**。`ss -tin` 的 `bytes_sent`/`bytes_received` 只存在于 TCP socket。QUIC、纯 UDP(游戏、部分视频、DNS)在 `direct[]` 里看不到,只能用网卡总量(`traffic.py` 那一路)兜底。**不要**把 `report` 的合计当作「全部网卡流量」,两者口径不同。
2. **UDP 连接的进程归因也不完整**。`ss_proc_map()` 对 UDP 只做端口匹配,没有字节数。
3. **root 进程的直连归不了因**:普通用户读 `/proc/<root-pid>/fd` 会被拒,这些条目落成 `(未归因)`,但字节不会丢(仍在 `direct_totals`)。
4. **Chrome 只能到进程级**:所有标签页共享 `NetworkService` utility 进程;`chrome_sites` 是「Chrome 的流量按目标站点拆」,不是「按标签页拆」。
5. **首次采样会尖峰**:第一次见到某个 socket 时,它的生命周期累计值全部算进这一条。清空 `flow_state.json`、重装、重启之后都会出现一次这样的尖峰;之后恢复正常。
6. **Clash 侧与网卡侧对不上是正常的**:Clash 只记经过它的连接,直连、UDP/QUIC、以及 Clash 自身与网关的通信都在它之外。`report` 里的 `iface_delta` 就是让你拿网卡增量来对账的,不做强制配平。
7. **`secret` 不支持**:Clash 控制端口设了 `secret` 时,本工具不带认证头,会读不到数据。
8. **重启期间的缺口**:`systemd --user` 定时器在注销/停机时停摆;`traffic-record.timer` 的 `Persistent=true` 只能补一次,这之前的历史补不回来。安装前的时间段**没有任何数据可以回溯**。
9. **数据文件不进 git**:见 §5,append-only 大文件不适合直接版本化,需要留档请自行轮转归档。

---

## 8. 测试

```bash
python3 tests/test_direct.py
```

纯假输入,不连 Clash、不读写真实数据、不需要 root。覆盖:

- `ss -tineH` 两行一组(地址行 + `tcp_info` 行)的解析;
- 假 `/proc`(通过 `PROC_ROOT`)里 `socket:[ino]` → 属主 pid;
- 同一份输入连采两次,第二次差量为 0;
- `bytes_sent` 变小 ⇒ 按新 socket 计全量(不产生负增量);
- 回环对端、被代理端口、属主是 clash 的 socket 三类排除。

期望输出最后一行 `OK: ...`。

---

## 9. 目录结构

```
Network-Tool/
├── README.md
├── .gitignore                        采样数据 / Python 产物
├── traffic/
│   ├── traffic.py                    网卡视角(6 个子命令)
│   ├── flow.py                       连接/进程视角(3 个子命令)
│   ├── traffic.csv                   ← 运行时生成:网卡累计快照
│   ├── flows.jsonl                   ← 运行时生成:连接级差量
│   ├── flow_state.json               ← 运行时生成:逐连接/逐 socket 上次累计
│   └── systemd/
│       ├── traffic-record.service    模板:@TRAFFIC_DIR@ 由 install.sh 替换
│       ├── traffic-record.timer
│       ├── traffic-flow.service      模板:同上
│       ├── traffic-flow.timer
│       └── install.sh
├── tests/
│   └── test_direct.py
└── docs/
    └── design/
        └── 2026-11-26-direct-traffic.md   直连统计的设计与取舍
```

unit 文件里的 `@TRAFFIC_DIR@` 是安装期占位符,**不要**直接拷进 `~/.config/systemd/user/`(systemd 会把 `@` 当普通字符,路径不存在)。请走 `install.sh`,或自己 `sed -i "s#@TRAFFIC_DIR@#$(pwd)#"` 后再拷。

---

## 10. 许可

未附许可证文件。若需在别的项目里复用,请先提 issue 约定用途。
