"""代理/出站不变量套件：带凭证的请求必须走统一出站函数。

移植自 wb 的 tests/_test_account_proxy_calls.py 思路：不是「功能测试」，而是**不变量测试**
—— 只要有人新增一条绕过统一出站函数（http_json）的直连 urlopen，这里就要红。

    python tests/_test_proxy_invariant.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
import qoder_accounts as A

PASS = FAIL = 0


def check(label, ok, detail=None):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %r" % (label, detail))


import os as _os_fp, sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_accounts.py", "qoder_proxy.py"])
print("[proxy-invariant] 出站不变量")
calls = []
orig = A.http_json


def _stub(url, *a, **k):
    calls.append(url)
    raise RuntimeError("stub: 已拦在统一出站函数")


A.http_json = _stub
try:
    acc = A.Account({"uid": "pi1", "realm": "cn", "accessToken": "dt-x"})
    try:
        acc.fetch_credits()
    except Exception:
        pass
    n_fetch = len(calls)
finally:
    A.http_json = orig
check("fetch_credits 走统一出站函数 http_json（不直连）", n_fetch == 1, n_fetch)

# 基线登记：直连 urlopen 的调用点数量。改动这个数字之前，先确认新点是否带账号凭证。
src = open(os.path.join(ROOT, "qoder_accounts.py"), encoding="utf-8").read()
n_direct = src.count("urllib.request.urlopen(")
# task-83 后基线 2 -> 0：全部出站改走 _opener_for_request()（每请求现建 opener，
# 代理只看环境变量——#27：Windows 注册表继承与首次请求固化已修复）。新增任何
# urllib.request.urlopen 都意味着绕过该策略——必须重新评估。
check("qoder_accounts.py 直连 urlopen 基线 == 0（出站统一走 _opener_for_request）",
      n_direct == 0, n_direct)
src_p = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
n_direct_p = src_p.count("urllib.request.urlopen(")
# 代理池（v1.5.2+）后：chat / 模型清单 / 更新检查改走 _open_upstream()（池化
# 选出口 + 传输层失败换代理）。剩余 2 处直连 = _open_upstream 的 env 分支
# （池未启用时的旧行为）+ 启动时的本机端口自检（绝不能走代理）。
check("qoder_proxy.py 直连 urlopen 基线 == 2（env 分支 + 本机端口自检）",
      n_direct_p == 2, n_direct_p)
check("qoder_proxy.py 上游请求走 _open_upstream（定义 + 3 个调用点）",
      src_p.count("_open_upstream(") >= 4, src_p.count("_open_upstream("))

print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)
