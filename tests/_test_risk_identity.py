"""#64 身份头对齐官方 + 风控计数。

    python tests/_test_risk_identity.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_sign.py", "qoder_proxy.py"])
import qoder_sign as S             # noqa: E402


def _hdr(uid, sse=True):
    sess = S.CosySession(uid)
    return sess.headers("{}", "https://api3.qoder.sh/algo/x", sse=sse)


# ---- 官方 infer-sse 头：machineos / machinehostname
_h1 = _hdr("uid-a")
check("#64 推理头含 cosy-machineos", "cosy-machineos" in _h1, list(_h1))
check("#64 推理头含 cosy-machinehostname", "cosy-machinehostname" in _h1, list(_h1))
check("#64 主机名形如 DESKTOP-XXXXXXX",
      str(_h1.get("cosy-machinehostname", "")).startswith("DESKTOP-")
      and len(_h1["cosy-machinehostname"]) == 15, _h1.get("cosy-machinehostname"))
_h1b = _hdr("uid-a")
check("#64 同一账号主机名稳定",
      _h1["cosy-machinehostname"] == _h1b["cosy-machinehostname"])
_h2 = _hdr("uid-b")
check("#64 不同账号主机名不同（避免多号关联）",
      _h1["cosy-machinehostname"] != _h2["cosy-machinehostname"],
      (_h1["cosy-machinehostname"], _h2["cosy-machinehostname"]))
check("#64 非 sse 请求不带 infer-sse 专属头",
      "cosy-machinehostname" not in _hdr("uid-a", sse=False))

os.environ["QD_COSY_OS_HEADERS"] = "0"
try:
    _h3 = _hdr("uid-a")
finally:
    os.environ.pop("QD_COSY_OS_HEADERS", None)
check("#64 开关可关闭（QD_COSY_OS_HEADERS=0）",
      "cosy-machineos" not in _h3 and "cosy-machinehostname" not in _h3)

# ---- 风控计数
_before = P.risk_snapshot()
P.note_risk("queue", "10605 test")
P.note_risk("rate", "429 test")
_snap = P.risk_snapshot()
check("#64 risk 计数累加（queue/rate）",
      _snap["queue"] == _before["queue"] + 1
      and _snap["rate"] == _before["rate"] + 1, _snap)
check("#64 risk 记录最近一次信号（kind/at/detail）",
      isinstance(_snap.get("last"), dict)
      and _snap["last"].get("kind") == "rate"
      and _snap["last"].get("at"), _snap.get("last"))
_src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
check("#64 /health 暴露 risk 快照", '"risk": risk_snapshot()' in _src)

# ---- 流量守卫：把「团队并发闸门」扩展到所有账号
_acc = A.Account({"uid": "guard-u", "realm": "cn", "accessToken": "t"})
check("#64 默认不强制（非团队账号不闸）", P._team_guard_on(_acc) is False)
os.environ["QD_TRAFFIC_GUARD"] = "on"
try:
    _on = P._team_guard_on(_acc)
    _conc = P._team_max_concurrency()
    _gap = P._team_min_gap()
finally:
    os.environ.pop("QD_TRAFFIC_GUARD", None)
check("#64 QD_TRAFFIC_GUARD=on -> 所有账号走闸门", _on is True)
check("#64 默认并发 1、间隔 400ms（自伤防护）",
      _conc == 1 and abs(_gap - 0.4) < 1e-6, (_conc, _gap))

# ---- 容器自检（时钟漂移 / 目录可写 / 身份缓存）
_env = P.env_report()
check("#64 env_report 覆盖容器判定与关键自检项",
      set(["container", "proxy", "accounts_writable", "identity_cached",
           "clock_skew_s", "traffic_guard"]) <= set(_env), sorted(_env))
check("#64 时钟漂移测量走代理感知 opener（不新增直连 urlopen）",
      callable(getattr(A, "upstream_clock_skew", None))
      and "urllib.request.urlopen(" not in
      open(os.path.join(ROOT, "qoder_accounts.py"), encoding="utf-8").read())
check("#64 启动时会 WARN 时钟漂移与目录不可写",
      "clock skew %ss detected" in _src
      and "accounts dir is not writable" in _src)

finish()
