"""诊断默认模型（免费模型优先）套件。

    python tests/_test_diag_default_model.py

判据：
  - 默认诊断模型是 Qwen3.8-Flash（免费），不是付费模型；
  - 显式传模型时不被覆盖；
  - 默认模型不在当前区目录时，回落到该区第一个免费模型，绝不回落到付费模型；
  - 面板默认选中免费模型并显示「不消耗付费额度」提示。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
os.environ.setdefault("QD_NATIVE_IDENTITY", "0")

import qoder_proxy as P

PASS = FAIL = 0


def check(label, ok, detail=None):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %r" % (label, detail))


def main():
    print("[diag-default-model] 诊断默认使用免费模型")
    check("DIAG_DEFAULT_MODEL == Qwen3.8-Flash",
          P.DIAG_DEFAULT_MODEL == "Qwen3.8-Flash", P.DIAG_DEFAULT_MODEL)

    check("空模型 -> Qwen3.8-Flash",
          P.resolve_diag_model(None) == "Qwen3.8-Flash",
          P.resolve_diag_model(None))
    check("空字符串 -> Qwen3.8-Flash",
          P.resolve_diag_model("") == "Qwen3.8-Flash",
          P.resolve_diag_model(""))
    check("intl 区 -> Qwen3.8-Flash",
          P.resolve_diag_model("", realm="intl") == "Qwen3.8-Flash",
          P.resolve_diag_model("", realm="intl"))
    check("cn 区 -> Qwen3.8-Flash",
          P.resolve_diag_model("", realm="cn") == "Qwen3.8-Flash",
          P.resolve_diag_model("", realm="cn"))
    check("显式 Qwen3.8-Max 不被覆盖",
          P.resolve_diag_model("Qwen3.8-Max") == "Qwen3.8-Max",
          P.resolve_diag_model("Qwen3.8-Max"))
    check("显式任意模型名原样返回",
          P.resolve_diag_model("  my-model  ") == "my-model",
          P.resolve_diag_model("  my-model  "))

    # 默认模型不在目录里 -> 回落到免费模型（而不是付费的 Max）
    real_fetch = P.fetch_models
    real_resolve = P.qoder_catalog.resolve_upstream_key
    try:
        P.fetch_models = lambda realm=None: [
            ("pmodel", {"key": "pmodel", "display_name": "Paid-One"}),
            ("fmodel", {"key": "fmodel", "display_name": "Free-One",
                        "is_free": True}),
        ]
        P.qoder_catalog.resolve_upstream_key = lambda model, realm=None: "qfm"
        got = P.resolve_diag_model("", realm="intl")
        check("默认模型缺失时回落到免费模型", got == "Free-One", got)
        P.fetch_models = lambda realm=None: []
        check("目录为空时仍返回默认模型",
              P.resolve_diag_model("", realm="intl") == "Qwen3.8-Flash")
    finally:
        P.fetch_models = real_fetch
        P.qoder_catalog.resolve_upstream_key = real_resolve

    html = io.open(os.path.join(ROOT, "dashboard.html"),
                   encoding="utf-8").read()
    for token in ("DIAG_DEFAULT_MODEL", "freeIds", "不消耗付费额度",
                  "诊断默认使用免费模型"):
        check("面板接线: %s" % token, token in html)
    check("下拉静态首项就是 Qwen3.8-Flash（清单未加载时也正确）",
          '<option value="Qwen3.8-Flash" selected>' in html)
    check("默认不会被付费模型顶掉（钉在免费模型上）",
          "defaultId = pinned" in html)
    # /accounts/test 也必须默认免费模型，而不是 auto
    src = io.open(os.path.join(ROOT, "qoder_proxy.py"),
                  encoding="utf-8").read()
    check("/accounts/test 默认使用免费诊断模型",
          'test_model = resolve_diag_model(' in src
          and 'test_model = payload.get("model") or "auto"' not in src)
    check("面板不再因空模型直接 return",
          "toast('请选择测试模型'" not in html)

    print("")
    print("SUMMARY: TOTAL %d checks, %d passed, %d failed"
          % (PASS + FAIL, PASS, FAIL))
    print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN",
                                    1 if FAIL else 0))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
