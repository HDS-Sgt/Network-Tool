#!/usr/bin/env python3
"""直连统计的单元测试:解析 ss / inode→pid / 差量 / 回退归零 / clash 排除。

跑法:python3 tests/test_direct.py   (不连 Clash、不改真实数据,全部用假输入)
"""
import importlib.util
import os
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("flow", os.path.join(ROOT, "traffic", "flow.py"))
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)


class _R:
    def __init__(self, out):
        self.stdout = out


SS = """ESTAB 0 0 203.0.113.10:45678 1.2.3.4:443 timer:(keepalive,9sec,0) uid:1000 ino:111 sk:1
     cubic wscale:7,7 rto:204 rtt:1.2/0.3 mss:1448 bytes_sent:1000 bytes_received:2000
ESTAB 0 0 127.0.0.1:40000 127.0.0.1:7890 uid:1000 ino:222 sk:2
     cubic bytes_sent:5 bytes_received:7
ESTAB 0 0 203.0.113.10:45679 10.0.0.1:80 uid:1000 ino:333 sk:3
     cubic bytes_sent:10 bytes_received:20
ESTAB 0 0 203.0.113.10:45680 [::ffff:127.0.0.1]:8080 uid:1000 ino:444 sk:4
     cubic bytes_sent:1 bytes_received:2
"""

_cur = {"out": SS}
flow.subprocess = type("_S", (), {"run": staticmethod(lambda *a, **k: _R(_cur["out"]))})


def test_parse():
    socks = flow.ss_tcp_sockets()
    assert [s["ino"] for s in socks] == [111, 222, 333, 444], socks
    assert socks[0]["sent"] == 1000 and socks[0]["recv"] == 2000, socks[0]
    assert socks[2]["port"] == "45679" and socks[2]["peer"] == "10.0.0.1:80", socks[2]


def test_inode_owner():
    tmp = tempfile.mkdtemp()

    def mk(pid, ino, comm, cmd):
        os.makedirs(os.path.join(tmp, str(pid), "fd"), exist_ok=True)
        os.symlink("socket:[%d]" % ino, os.path.join(tmp, str(pid), "fd", "5"))
        with open(os.path.join(tmp, str(pid), "comm"), "w") as fh:
            fh.write(comm + "\n")
        with open(os.path.join(tmp, str(pid), "cmdline"), "wb") as fh:
            fh.write((cmd + "\0").encode())

    mk(999, 111, "curl", "curl https://example.com")
    mk(888, 333, "clash", "clash -d /opt/clash")
    old = flow.PROC_ROOT
    flow.PROC_ROOT = tmp
    try:
        owners = flow.inode_owner_map({111, 333})
        assert owners == {111: 999, 333: 888}, owners
    finally:
        flow.PROC_ROOT = old


def test_direct_diff():
    owners = {111: 999, 333: 888}
    old_proc = flow.proc_info
    flow.proc_info = lambda pid: (pid, "curl" if pid == 999 else "clash", "curl https://x")
    try:
        st = {}
        r1 = flow.direct_recs(flow.ss_tcp_sockets(), owners, {"40000"}, st, 1000)
        # 222 是被代理的(端口在 sourcePort 集合);333 属主 clash 要排除;444 是回环要排除
        assert len(r1) == 1 and r1[0]["pid"] == 999, r1
        assert r1[0]["up"] == 1000 and r1[0]["down"] == 2000, r1[0]
        assert flow.direct_recs(flow.ss_tcp_sockets(), owners, {"40000"}, st, 1001) == []

        _cur["out"] = SS.replace("bytes_sent:1000", "bytes_sent:500")
        r3 = flow.direct_recs(flow.ss_tcp_sockets(), owners, {"40000"}, st, 1002)
        assert len(r3) == 1 and r3[0]["up"] == 500, r3   # 计数回退 -> 当新 socket 算全量
    finally:
        flow.proc_info = old_proc
        _cur["out"] = SS


if __name__ == "__main__":
    test_parse()
    test_inode_owner()
    test_direct_diff()
    print("OK: parse / inode->pid / diff / shrink-as-new / clash+loopback excluded")
