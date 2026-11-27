# 直连流量 + 按进程统计 Implementation Plan

> 状态:已实现(2026-11-26)。下列任务清单保留为设计记录,勾选表示已落地并有对应验证。

**Goal:** 让 `flow.py` 的采样同时统计「不经 Clash 的直连 TCP 流量」,并按进程归因,最终 `report` 能把代理流量与直连流量合在一起看。

**Architecture:** 每次 `sample` 在原有 Clash `/connections` 之外,再读一次 `ss -tineH` 拿到**所有 TCP socket** 的 inode 与内核字节计数(`bytes_sent`/`bytes_received`);本地端口出现在 Clash `sourcePort` 集合里的算「被代理」并排除,剩下的就是直连;用 inode 扫 `/proc/<pid>/fd` 找到属主进程;逐 socket 做差量后写进同一行 JSONL 的 `direct[]`。

**Tech Stack:** Python 3.8 标准库 + iproute2 `ss`;无新依赖。

**Spec:** 本文件即设计。

## Global Constraints
- 只改 `traffic/flow.py`;本特性不新增 systemd unit,也**不改写、不重启** Clash —— 只读它的控制端口。
- 旧数据兼容:历史行没有 `direct` 字段,`report` 必须能照常跑。
- 不得把 Clash 自己的出口 socket 当成直连(否则与 `/connections` 已记的流量重复计算)。
- 采样必须在与被观测进程相同的 PID 命名空间运行(systemd --user 满足);容器 / 独立 PID namespace 里 `/proc` 看不到外面的进程,只能得到「未归因」。

## Review Focus
- **重复计数**:Clash 自己的出站 socket(本地端口不在 `sourcePort` 白名单里)—— 必须按「属主进程是 clash」排除;若无法解析属主,要显式标成未归因而非静默计入。
- **socket 复用/重连**:inode 相同但字节回退 → 按新 socket 处理,不能产生负差量。
- **计数器语义**:`bytes_sent`/`bytes_received` 是 socket 生命周期累计,必须做差量;首次见到按全量计。
- **非 TCP**:QUIC/UDP 没有字节计数 —— 不参与统计,文档里写清,不许假装覆盖。
- **权限**:root 进程的 `/proc/<pid>/fd` 读不到 → 归因为空,不能让整个采样抛异常。

---

### Task 1: 读 TCP socket 与各 socket 的累计字节

**Files:** Modify `traffic/flow.py`(新增 `ss_tcp_sockets()`)

**Interfaces:**
- Produces: `ss_tcp_sockets() -> list[dict]`,每个 dict:`{"state","local","peer","port","ino","sent","recv"}`,`port` 为本地端口字符串(解析失败为 `""`),`ino` 为 int(缺失为 0)。

- [x] **Step 1:** 写解析函数,按 `ss -tineH` 的两行一组(地址行 + info 行)配对,info 行用 `bytes_sent:(\d+)` / `bytes_received:(\d+)` 取值。
- [x] **Step 2:** 验证:在真实环境跑一次,断言 `len(...) > 0` 且抽样 socket 的 `sent`/`recv` 为正整数、`ino > 0`。

### Task 2: inode → 进程

**Files:** Modify `traffic/flow.py`(新增 `inode_owner_map(inodes)`)

**Interfaces:**
- Consumes: Task 1 的 `ino`。
- Produces: `inode_owner_map(inodes: set[int]) -> dict[int, int]`(inode → pid);读 `/proc/<pid>/fd`,路径根取 `PROC_ROOT`(默认 `/proc`,便于用假目录测试)。

- [x] **Step 1:** 遍历 `PROC_ROOT` 下的数字目录,读 `fd/*` 的 readlink,匹配 `socket:\[(\d+)\]`。
- [x] **Step 2:** 验证:用临时目录造假 `proc/<pid>/fd/3 -> socket:[12345]`,断言返回 `{12345: pid}`;再对真实 `/proc` 跑一次,断言不抛异常。

### Task 3: 直连差量与状态

**Files:** Modify `traffic/flow.py`(`snapshot()` 内新增 `direct_snapshot(proxied_ports, owners, st)`,state 增加 `__direct__` 子字典)

**Interfaces:**
- Produces: `direct_snapshot(...) -> list[dict]`,字段 `{"proc","proc_src","pid","tgid","comm","cmd","local_port","peer","remote_ip","up","down","ino"}`。
- 状态键:`st["__direct__"][str(ino)] = {"up","down","ts"}`;消失超过 `PRUNE_SEC` 清理。

- [x] **Step 1:** 过滤规则:跳过本地端口在 `proxied_ports` 的、跳过 peer 为回环(`127.` / `::1`)的、跳过属主进程 comm/cmd 含 `clash`/`mihomo` 的。
- [x] **Step 2:** 差量:首次见按全量;`sent - prev < 0` 视为新 socket 按全量;否则取差。
- [x] **Step 3:** 验证:同一份假 `ss` 输出跑两次,第二次差量为 0;把 `sent` 调小后断言按新 socket 计全量。

### Task 4: 写进 JSONL + report 出两张表

**Files:** Modify `traffic/flow.py`(`cmd_sample` / `cmd_report`)

**Interfaces:**
- JSONL 行新增:`"direct": [...]`、`"direct_conns": N`。
- `flow_report.json` 新增键:`"by_process_direct"`、`"by_proc_all"`、`"direct_totals"`。
- 终端新增两张表:「直连进程」「全部进程(代理+直连)」。

- [x] **Step 1:** `cmd_sample` 打印行加直连条数与字节。
- [x] **Step 2:** `cmd_report` 对旧行用 `rec.get("direct") or []`,保证老数据不炸。
- [x] **Step 3:** 验证:`report` 跑通、老 JSONL 正常;数据用 `--json` 断言三个新键存在。
- [x] **Step 4:** README 补「直连统计」小节与三条限制。
