#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flow.py —— 把 Clash 的流量按「进程 / 网站」归因记账,数据存 JSON。

子命令
  sample   采一次:读 Clash /connections,和上次快照求差量,追加进 flows.jsonl
  report   汇总 flows.jsonl -> flow_report.json(JSON)+ 终端表格
  show     只看此刻的实时归属,不落盘

进程名怎么来的(按优先级)
  1) Clash 自报的 metadata.process —— 需要 config.yaml 里加 `find-process-mode: always` 并重启 clash
  2) 拿 metadata.sourcePort 去 `ss -tnp` / `ss -unp` 反查 socket 属主
     ⚠ 第 2 条只在**宿主命名空间**里有效:用户自己的终端、systemd --user 服务里都能查;
        AI 的 bash 沙箱是独立 PID namespace,/proc 里看不到宿主进程,会得到 attributed=false
        (此时 host/网站 级统计照常,只是没有进程名)。

环境变量
  CLASH_API  默认 http://127.0.0.1:9090
  FLOW_DIR   默认脚本所在目录(flows.jsonl / flow_state.json / flow_report.json)
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FLOW_DIR = os.environ.get("FLOW_DIR") or HERE
API = (os.environ.get("CLASH_API") or "http://127.0.0.1:9090").rstrip("/")
FLOWS = os.path.join(FLOW_DIR, "flows.jsonl")
STATE = os.path.join(FLOW_DIR, "flow_state.json")
REPORT = os.path.join(FLOW_DIR, "flow_report.json")
PRUNE_SEC = 900          # 连接消失超过这么久,就从 state 里清掉
TWO_LEVEL = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
    "com.hk", "com.tw", "com.au", "co.jp", "ne.jp", "or.jp", "co.kr", "com.br", "com.sg",
    "github.io", "pages.dev", "vercel.app", "workers.dev", "blogspot.com", "appspot.com",
}


def clash_get(path, timeout=3.0):
    with urllib.request.urlopen(API + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def site_of(host):
    """粗略 eTLD+1:example.com / a.b.example.com 都归 example.com。"""
    h = (host or "").strip().lower().strip(".")
    if not h:
        return "(无域名)"
    if re.fullmatch(r"[\d.:]+", h):
        return "(纯 IP)"
    p = h.split(".")
    if len(p) >= 3 and ".".join(p[-2:]) in TWO_LEVEL:
        return ".".join(p[-3:])
    return ".".join(p[-2:]) if len(p) >= 2 else h


def ss_proc_map():
    """local port -> {'proc': ss 报的名字, 'pids': [...]},来自 ss -tnp / ss -unp。

    注意:iproute2 新版的 pid 是**线程 id**、名字是**线程名**(所以会看到 main / MainThread 这种),
    真正的进程名要用 Tgid 再查一次 —— 见 proc_name_from_pid()。
    """
    m = {}
    for args in (["ss", "-tnp"], ["ss", "-unp"]):
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=5).stdout
        except Exception:
            continue
        for line in out.splitlines()[1:]:
            f = line.split()
            if len(f) < 5:
                continue
            local = f[3]
            if not (local.startswith("127.0.0.1:") or local.startswith("[::1]:")):
                continue
            port = local.rsplit(":", 1)[-1]
            tail = " ".join(f[5:])
            u = re.search(r'\("([^"]+)",(?:pid=)?(\d+)', tail)
            if not u:
                continue
            d = m.setdefault(port, {"proc": u.group(1), "pids": []})
            d["pids"].append(int(u.group(2)))
    return m


def label_of(comm, cmd):
    """comm 可能是线程名(main / MainThread / node 的线程…),用 cmdline 拼一个看得懂的名字。"""
    generic = {"", "main", "MainThread", "Thread", "python3", "python", "node", "java",
               "sh", "bash", "sleep"}
    toks = (cmd or "").split(" ")
    base = os.path.basename(toks[0]) if toks and toks[0] else ""
    if not base:
        return comm or "(未知)"
    extra = ""
    for t in toks[1:3]:
        b = os.path.basename(t)
        if b.endswith((".js", ".mjs", ".cjs", ".py", ".jar", ".asar")) or ".asar" in b:
            extra = " " + b
            break
    if comm in generic:
        return base + extra
    return comm


def proc_info(pid):
    """ss 给的是线程 id、名字是线程名(会看到 main / MainThread 这种)。
    这里用 /proc/<tid>/status 的 Tgid 归到真正的进程,再取 comm 和 cmdline。
    返回 (tgid, comm, cmdline)。"""
    tgid = pid
    try:
        with open("/proc/%d/status" % pid) as fh:
            for line in fh:
                if line.startswith("Tgid:"):
                    tgid = int(line.split()[1])
                    break
    except Exception:
        pass
    comm = cmd = ""
    try:
        with open("/proc/%d/comm" % tgid) as fh:
            comm = fh.read().strip()
    except Exception:
        pass
    try:
        with open("/proc/%d/cmdline" % tgid, "rb") as fh:
            cmd = " ".join(x.decode("utf-8", "replace")
                           for x in fh.read().split(b"\x00") if x)[:160]
    except Exception:
        pass
    return tgid, comm, cmd


def iface_totals():
    """/proc/net/dev 累计字节(不含 lo)。"""
    tot = {}
    try:
        with open("/proc/net/dev") as fh:
            for line in fh.readlines()[2:]:
                name, rest = line.split(":", 1)
                name = name.strip()
                if name == "lo":
                    continue
                cols = rest.split()
                tot[name] = {"rx": int(cols[0]), "tx": int(cols[8])}
    except Exception:
        pass
    return tot


def load_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def snapshot():
    """取一次 /connections + ss 归属,返回 (记录列表(含差量), 元信息)。"""
    data = clash_get("/connections")
    conns = data.get("connections") or []
    pmap = ss_proc_map()
    st = load_state()
    now = int(time.time())
    recs, live = [], set()
    self_unknown = os.getpid()
    pidname = {}
    for c in conns:
        m = c.get("metadata") or {}
        cid = c.get("id") or f"{m.get('sourcePort')}-{m.get('destinationIP')}-{m.get('destinationPort')}"
        live.add(cid)
        up, down = int(c.get("upload") or 0), int(c.get("download") or 0)
        p = st.get(cid) or {}
        d_up = up - int(p.get("up") or 0) if p else up
        d_dn = down - int(p.get("down") or 0) if p else down
        if d_up < 0:   # 连接被复用/计数器回绕,当新连接
            d_up, d_dn = up, down
        st[cid] = {"up": up, "down": down, "ts": now}
        if d_up <= 0 and d_dn <= 0:
            continue
        proc = m.get("process") or ""
        src = "clash"
        pid = tgid = 0
        comm = ""
        cmd = m.get("processPath") or ""
        if not proc:
            hit = pmap.get(str(m.get("sourcePort") or ""))
            if hit:
                # 排除记账脚本自己(它也可能经代理,别把它自己算成大流量)
                for cand in hit["pids"]:
                    if cand == self_unknown:
                        continue
                    if cand not in pidname:
                        pidname[cand] = proc_info(cand)
                    tgid, comm, cl = pidname[cand]
                    proc, pid, src = label_of(comm, cl) or hit["proc"], cand, "ss"
                    cmd = cmd or cl
                    break
        host = m.get("host") or m.get("destinationIP") or ""
        recs.append({
            "id": cid, "proc": proc or "", "proc_src": src if proc else "",
            "pid": pid, "tgid": tgid, "comm": comm, "cmd": cmd,
            "host": host, "site": site_of(host),
            "dest": f"{m.get('destinationIP')}:{m.get('destinationPort')}",
            "src_port": m.get("sourcePort"), "type": m.get("type") or m.get("network"),
            "rule": c.get("rule"), "chains": c.get("chains") or [],
            "up": d_up, "down": d_dn,
        })
    # 清理长时间没出现的连接
    for cid in list(st):
        if cid not in live and now - int(st[cid].get("ts") or 0) > PRUNE_SEC:
            del st[cid]
    save_json(STATE, st)
    return recs, {"ss_sockets": len(pmap), "live_conns": len(conns)}


def cmd_sample():
    recs, meta = snapshot()
    rec = {
        "ts": int(time.time()),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ss_sockets": meta["ss_sockets"], "live_conns": meta["live_conns"],
        "attributed": any(r["proc"] for r in recs),
        "iface": iface_totals(),
        "conns": recs,
    }
    os.makedirs(FLOW_DIR, exist_ok=True)
    with open(FLOWS, "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tot_dn = sum(r["down"] for r in recs)
    print(f"[flow] {rec['iso']} 连接={meta['live_conns']} 差量条目={len(recs)} "
          f"+{sum(r['up'] for r in recs)/1024:.1f}KiB/-{tot_dn/1024:.1f}KiB "
          f"进程名={'有(' + str(meta['ss_sockets']) + ' 个 socket)' if meta['ss_sockets'] else '无(不在宿主命名空间)'}"
          f" -> {FLOWS}")


def load_flows():
    out = []
    if not os.path.exists(FLOWS):
        return out
    with open(FLOWS) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def human(n):
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or u == "TiB":
            return f"{n:.1f}{u}" if u != "B" else f"{int(n)}B"
        n /= 1024.0


def cmd_report(top=15, as_json=False):
    flows = load_flows()
    if not flows:
        print(f"[flow] 还没有数据:{FLOWS} 不存在或为空,先跑 `flow.py sample`")
        return 1
    by_proc, by_site, by_host, by_rule = {}, {}, {}, {}
    for rec in flows:
        for r in rec["conns"]:
            dn, up = r["down"], r["up"]
            pk = r["proc"] or "(未归因)"
            for key, node in ((pk, by_proc), (r["site"], by_site), (r["host"] or "(空)", by_host),
                              (r["rule"] or "(直连/未知)", by_rule)):
                d = node.setdefault(key, {"up": 0, "down": 0, "conns": 0})
                d["up"] += up; d["down"] += dn; d["conns"] += 1
            if not by_proc[pk].get("cmd") and r.get("cmd"):
                by_proc[pk]["cmd"] = r["cmd"]
    # 进程 × 网站
    by_pairs = {}
    for rec in flows:
        for r in rec["conns"]:
            k = (r["proc"] or "(未归因)", r["site"])
            d = by_pairs.setdefault(k, {"up": 0, "down": 0, "conns": 0})
            d["up"] += r["up"]; d["down"] += r["down"]; d["conns"] += 1
    # Chrome(及其它 chrom* 进程)单独按网站看一份
    chrome_sites = {}
    for (pk, site), v in by_pairs.items():
        if "chrom" in pk.lower():
            d = chrome_sites.setdefault(site, {"up": 0, "down": 0, "conns": 0})
            d["up"] += v["up"]; d["down"] += v["down"]; d["conns"] += v["conns"]
    ts = [r["ts"] for r in flows if r.get("ts")]
    iface = {}
    for rec in flows:
        for n, v in (rec.get("iface") or {}).items():
            iface.setdefault(n, []).append((rec["ts"], v))
    iface_delta = {}
    for n, seq in iface.items():
        seq.sort(key=lambda t: t[0])
        iface_delta[n] = {"rx": seq[-1][1]["rx"] - seq[0][1]["rx"],
                          "tx": seq[-1][1]["tx"] - seq[0][1]["tx"]}
    rep = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "window": {"start": min(ts) if ts else None, "end": max(ts) if ts else None,
                   "samples": len(flows)},
        "totals": {"up": sum(v["up"] for v in by_proc.values()),
                   "down": sum(v["down"] for v in by_proc.values())},
        "by_process": by_proc, "by_site": by_site, "by_host": by_host, "by_rule": by_rule,
        "by_process_site": {f"{k[0]} @ {k[1]}": v for k, v in by_pairs.items()},
        "chrome_sites": chrome_sites,
        "iface_delta": iface_delta,
    }
    save_json(REPORT, rep)
    if as_json:
        print(json.dumps(rep, ensure_ascii=False, indent=1, sort_keys=True))
        return 0
    w = rep["window"]
    print(f"窗口 {w['start']}–{w['end']} ({w['samples']} 次采样)  "
          f"合计 ↑{human(rep['totals']['up'])} ↓{human(rep['totals']['down'])}")
    def table(title, node, n=top, show_cmd=False):
        rows = sorted(node.items(), key=lambda kv: kv[1]["down"], reverse=True)[:n]
        print(f"\n== {title} (按下载量) ==")
        for k, v in rows:
            print(f"  {v['down']:>12,}  ↑{v['up']:>11,}  {v['conns']:>5} 条  {k}")
            if show_cmd and v.get("cmd"):
                print(f"        ↳ {v['cmd'][:110]}")
    table("进程", by_proc, show_cmd=True)
    table("网站(eTLD+1)", by_site)
    if chrome_sites:
        table("Chrome 网站", chrome_sites)
    table("规则/策略组", by_rule)
    for n, v in iface_delta.items():
        print(f"\n网卡 {n} 窗口内累计:rx {human(v['rx'])} tx {human(v['tx'])}"
              f"  (含直连/非代理流量,所以会比 Clash 侧大)")
    print(f"\nJSON 已写入 {REPORT}")
    return 0


def cmd_show():
    recs, meta = snapshot()
    pmap = ss_proc_map()
    print(f"活动连接 {meta['live_conns']} 条,ss 里解析到的本机 socket {meta['ss_sockets']} 个")
    rows = sorted(recs, key=lambda r: r["down"], reverse=True)[:20]
    for r in rows:
        print(f"  {human(r['down']):>10} ↓ {human(r['up']):>10} ↑  {r['proc'] or '?':<12} "
              f"{r['site']:<28} {r['host']}")
    if not meta["ss_sockets"]:
        print("\n⚠ 拿不到进程名:当前不在宿主命名空间(/proc 看不到宿主进程)。"
              "在你自己终端里跑,或让 systemd --user 定时器跑,就能带上进程名。")
    return 0


def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "report").lower()
    if cmd == "sample":
        return cmd_sample()
    if cmd == "report":
        top = 15
        as_json = "--json" in argv
        for a in argv[2:]:
            if a.isdigit():
                top = int(a)
        return cmd_report(top, as_json)
    if cmd == "show":
        return cmd_show()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
