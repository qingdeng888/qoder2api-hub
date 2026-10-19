"""可自定义「检测模型」套件。

    python tests/_test_probe_model.py

判据：
  - settings 里存 probe_model，空串 = 内置默认（Qwen3.8-Flash）；
  - 冷却口径跟随自定义模型：只有 probe_model 的 403 才禁用账号，
    其它模型（含其它免费模型）的 403 一律不动账号；
  - 诊断入口（/diag、/accounts/test）默认用 probe_model；
  - 面板有输入框/保存/恢复默认/测试按钮。
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QD_NATIVE_IDENTITY", "0")

import qoder_proxy as P
import qoder_settings as S

PASS = FAIL = 0


def check(label, ok, detail=None):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %r" % (label, detail))


DETAIL = '{"code":"403","message":"account_banned"}'


def main():
    print("[probe-model] 可自定义检测模型")
    temp = tempfile.mkdtemp(prefix="qd-probe-")
    old_dir = P.ACCOUNTS_DIR
    try:
        P.ACCOUNTS_DIR = temp
        S.save(temp, {})

        check("默认没有自定义检测模型", S.probe_model(temp) == "",
              S.probe_model(temp))
        check("未配置时生效值是内置默认 Qwen3.8-Flash",
              P.probe_model() == "Qwen3.8-Flash", P.probe_model())
        check("内置默认下 Qwen3.8-Flash 的 403 会禁用账号",
              P.should_auto_disable_403(DETAIL, model="Qwen3.8-Flash")[0])
        # 探测模型撞付费墙：说明配置选错了模型，绝不禁用账号
        pay = '{"code":"112","message":"{\\"pricingUrl\\":\\"https://qoder.com/pricing\\"}"}'
        ok, why = P.should_auto_disable_403(pay, model="Qwen3.8-Flash")
        check("探测模型撞付费墙 -> 不禁用（配置问题，不是账号问题）",
              not ok and why == "probe_model_needs_payment", (ok, why))
        check("内置默认下其它模型（付费）的 403 不禁用",
              not P.should_auto_disable_403(DETAIL, model="Qwen3.8-Max")[0])

        saved = S.set_probe_model(temp, "  GLM-5.3-Flash  ")
        check("保存自定义检测模型并去空白",
              saved == "GLM-5.3-Flash" and S.probe_model(temp) == "GLM-5.3-Flash",
              saved)
        check("生效值跟随自定义模型", P.probe_model() == "GLM-5.3-Flash",
              P.probe_model())
        check("自定义模型上的 403 会禁用账号",
              P.should_auto_disable_403(DETAIL, model="GLM-5.3-Flash")[0])
        check("原默认模型 Qwen3.8-Flash 的 403 不再禁用",
              not P.should_auto_disable_403(DETAIL,
                                            model="Qwen3.8-Flash")[0])
        check("is_probe_model 认上游 key / 别名",
              P.is_probe_model("GLM-5.3-Flash")
              and P.is_probe_model(P.qoder_catalog.resolve_upstream_key(
                  "GLM-5.3-Flash", realm="intl") or "GLM-5.3-Flash"))

        check("诊断默认用自定义检测模型",
              P.resolve_diag_model(None) == "GLM-5.3-Flash",
              P.resolve_diag_model(None))
        check("显式指定的模型仍然优先",
              P.resolve_diag_model("Qwen3.8-Max") == "Qwen3.8-Max")

        S.set_probe_model(temp, "")
        check("清空后回到内置默认", P.probe_model() == "Qwen3.8-Flash",
              P.probe_model())
        check("清空后 JSON 里不再留 probe_model 键",
              "probe_model" not in S.load(temp), S.load(temp))

        S.set_probe_model(temp, "X" * 200)
        check("超长模型名被截断到 80",
              len(S.probe_model(temp)) == 80, len(S.probe_model(temp)))
        S.set_probe_model(temp, "")

        # 排队/内容审核/参数错在自定义模型上也不禁用
        for detail, reason in (
                ('{"code":"403","message":"{\\"code\\":\\"10605\\"}"}', "queue"),
                ('{"code":"403","message":"DataInspectionFailed"}',
                 "content_policy"),
                ('{"code":"403","message":"invalid_parameter_error"}',
                 "client_fault")):
            ok, why = P.should_auto_disable_403(detail, model="Qwen3.8-Flash")
            check("免费模型上 %s 不禁用" % reason,
                  not ok and why == reason, (ok, why))
    finally:
        P.ACCOUNTS_DIR = old_dir
        shutil.rmtree(temp, ignore_errors=True)

    html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    for token in ("sectionProbeModel", "setProbeModel", "saveProbeModel",
                  "clearProbeModel", "testProbeModel", "检测模型"):
        check("面板接线: %s" % token, token in html)
    src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
    check("设置保存接口支持 probe_model", '"probe_model" in payload' in src)

    print("")
    print("SUMMARY: TOTAL %d checks, %d passed, %d failed"
          % (PASS + FAIL, PASS, FAIL))
    print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN",
                                    1 if FAIL else 0))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
