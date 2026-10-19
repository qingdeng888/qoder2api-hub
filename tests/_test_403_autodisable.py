"""403 自动禁用 / 到期自动恢复套件。

    python tests/_test_403_autodisable.py

判据：
  - 账号级 403（付费墙 112 / 权限拒绝）-> 自动禁用 + 冷却 2h；
  - 排队 10605 / 内容审核 / 参数错 / 凭证失效 -> **不**自动禁用；
  - 到期自动翻回 enabled（无需人工），并落盘；
  - 面板可见剩余时间，账户池选号会跳过、到期后重新可选。
"""
import io
import os
import shutil
import sys
import tempfile
import time
import types
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
os.environ.setdefault("QD_NATIVE_IDENTITY", "0")

import qoder_accounts as A
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


PRICING_403 = ('{"code":"112","message":"{\\"pricingUrl\\":'
               '\\"https://qoder.com/pricing?client=qoder\\"}"}')
QUEUE_403 = ('{"code":"403","message":"{\\"code\\":\\"10605\\",'
             '\\"message\\":\\"{\\\\\\"isQueued\\\\\\":true,'
             '\\\\\\"retryAfterSeconds\\\\\\":30}"}"}')
POLICY_403 = '{"code":"403","message":"Input text data may contain inappropriate content"}'


def main():
    print("[403-autodisable] 403 自动禁用与到期恢复")
    check("默认窗口 7200s（2 小时）", P.AUTO_DISABLE_403_SECS == 7200,
          P.AUTO_DISABLE_403_SECS)

    # ---- 判定函数：只有免费模型 Qwen3.8-Flash 上的 403 才算数 ----
    FREE = "Qwen3.8-Flash"
    PAID = "Qwen3.8-Max"

    check("is_free_model 认得官方名与上游 key",
          P.is_free_model("Qwen3.8-Flash") and P.is_free_model("qfmodel")
          and P.is_free_model("qwen3.8-flash"))
    check("is_free_model 认得付费模型为 False",
          P.is_free_model(PAID) is False and P.is_free_model("") is False)

    ok, why = P.should_auto_disable_403(PRICING_403, model=PAID)
    check("付费模型 403（112）-> 不冷却、不禁用",
          not ok and why == "paid_model", (ok, why))
    ok, why = P.should_auto_disable_403(PRICING_403, model=FREE)
    check("免费模型 403（112）-> 自动禁用",
          ok and "403" in why, (ok, why))
    ok, why = P.should_auto_disable_403(
        '{"code":"403","message":"permission_error: not authorized"}',
        model=FREE)
    check("免费模型权限拒绝 403 -> 自动禁用", ok, (ok, why))
    ok, why = P.should_auto_disable_403(
        '{"code":"403","message":"account_banned"}', model=FREE)
    check("免费模型账号被封 403 -> 自动禁用", ok, (ok, why))
    ok, why = P.should_auto_disable_403(
        '{"code":"403","message":"account_banned"}', model=PAID)
    check("付费模型账号被封 403 -> 仍不冷却（口径只看模型）",
          not ok and why == "paid_model", (ok, why))
    ok, why = P.should_auto_disable_403(QUEUE_403, model=FREE)
    check("排队 10605 -> 不自动禁用", not ok and why == "queue", (ok, why))
    check("is_queue_403 识别 RESOURCE_LIMIT",
          P.is_queue_403('{"subCode":"RESOURCE_LIMIT"}') is True)
    ok, why = P.should_auto_disable_403(POLICY_403, model=FREE)
    check("内容审核 403 -> 不自动禁用", not ok and why == "content_policy",
          (ok, why))
    ok, why = P.should_auto_disable_403(
        '{"code":"403","message":"invalid_parameter_error"}', model=FREE)
    check("参数错误 403 -> 不自动禁用", not ok and why == "client_fault",
          (ok, why))
    ok, why = P.should_auto_disable_403(
        '{"code":"403","message":"TOKEN_EXPIRE"}', model=FREE)
    check("凭证失效 -> 不自动禁用（走既有停用）",
          not ok and why == "session_dead", (ok, why))
    ok, why = P.should_auto_disable_403("some unknown 403 body", model=FREE)
    check("免费模型未知 403 -> 仍禁用（免费模型都不通则账号有问题）",
          ok, (ok, why))
    ok, why = P.should_auto_disable_403("some unknown 403 body", model=PAID)
    check("付费模型未知 403 -> 不禁用", not ok and why == "paid_model",
          (ok, why))

    # ---- Account 行为 ----
    tmp = tempfile.mkdtemp(prefix="qd-403-")
    try:
        acc = A.Account({"uid": "auto403-1", "nickname": "测试号",
                         "realm": "intl", "accessToken": "dt-x"})
        acc.save(tmp)
        before = time.time()
        window = acc.auto_disable(7200, "HTTP 403: 付费墙")
        check("auto_disable 返回 7200", window == 7200, window)
        check("自动禁用后 enabled=False", acc.enabled is False)
        check("disabled_until 约 2 小时后",
              abs(acc.disabled_until - (before + 7200)) < 5, acc.disabled_until)
        check("冷却同步延长",
              acc.cooldown_until >= acc.disabled_until - 1, acc.cooldown_until)
        check("记录原因", "付费墙" in acc.disabled_reason, acc.disabled_reason)
        check("ready() 为 False", acc.ready() is False)
        check("public() 暴露剩余秒数",
              acc.public().get("autoDisabledFor", 0) > 7100,
              acc.public().get("autoDisabledFor"))
        check("public() 暴露原因",
              "付费墙" in acc.public().get("autoDisabledReason", ""))

        reloaded = A.AccountPool(tmp)
        reloaded.load()
        again = reloaded.get("auto403-1")
        check("自动禁用状态落盘（重启后仍禁用）",
              again.enabled is False and again.disabled_until > time.time(),
              (again.enabled, again.disabled_until))

        # 模拟到期
        again.disabled_until = time.time() - 1
        check("未到期时 refresh_auto_disable 不动",
              A.Account({"uid": "x", "accessToken": "dt",
                         "disabledUntil": time.time() + 600}).refresh_auto_disable()
              is False)
        restored = again.refresh_auto_disable()
        check("到期后 refresh_auto_disable 返回 True", restored is True)
        check("到期后 enabled 恢复", again.enabled is True)
        check("到期后清空 disabled_until / reason",
              again.disabled_until == 0 and again.disabled_reason == "",
              (again.disabled_until, again.disabled_reason))

        third = A.AccountPool(tmp)
        third.load()
        check("恢复状态也落盘",
              third.get("auto403-1").enabled is True,
              third.get("auto403-1").enabled)

        # ---- 选号：禁用期间跳过，到期后重新可选 ----
        pool = A.AccountPool(tmp, log=lambda *a, **k: None)
        pool.load()
        target = pool.get("auto403-1")
        target.auto_disable(7200, "test")
        pool.accounts.append(A.Account({"uid": "auto403-2", "realm": "intl",
                                        "accessToken": "dt-y", "enabled": True}))
        picked = set()
        for _ in range(6):
            acc2 = pool.pick(realm="intl")
            if acc2:
                picked.add(acc2.uid)
        check("禁用期间不会被选中", "auto403-1" not in picked, picked)
        target.disabled_until = time.time() - 1
        target.refresh_auto_disable()
        picked2 = set()
        for _ in range(6):
            acc2 = pool.pick(realm="intl")
            if acc2:
                picked2.add(acc2.uid)
        check("到期后重新参与轮换", "auto403-1" in picked2, picked2)
        check("count_ready 到期后计入",
              pool.count_ready("intl") == 2, pool.count_ready("intl"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 信封层接线：真跑 _handle_envelope_account_cooldown ----
    tmp2 = tempfile.mkdtemp(prefix="qd-403-env-")
    real_pool = P.POOL
    try:
        pool = A.AccountPool(tmp2, log=lambda *a, **k: None)
        acc = A.Account({"uid": "env403-1", "realm": "intl",
                         "accessToken": "dt-x"})
        acc.save(tmp2)
        pool.accounts.append(acc)
        P.POOL = pool
        # 付费模型 403：既不冷却也不禁用（很多号本来就没有付费额度）
        exc = types.SimpleNamespace(status=403, detail=PRICING_403)
        P._handle_envelope_account_cooldown(acc, exc, model="Qwen3.8-Max",
                                            session_key=None)
        check("信封 403（付费模型 112）-> 账号不冷却、不禁用",
              acc.enabled is True and acc.disabled_until == 0
              and acc.cooldown_until <= time.time()
              and "Qwen3.8-Max" not in acc.model_cooldowns,
              (acc.enabled, acc.cooldown_until - time.time(),
               dict(acc.model_cooldowns)))

        # 免费模型 403：账号确实不可用 -> 自动禁用
        free_acc = A.Account({"uid": "env403-free", "realm": "intl",
                              "accessToken": "dt-f"})
        free_acc.save(tmp2)
        pool.accounts.append(free_acc)
        exc_f = types.SimpleNamespace(
            status=403, detail='{"code":"403","message":"account_banned"}')
        P._handle_envelope_account_cooldown(free_acc, exc_f,
                                            model="Qwen3.8-Flash",
                                            session_key=None)
        check("信封 403（免费模型）-> 账号自动禁用 2h",
              free_acc.enabled is False and free_acc.auto_disabled_for() > 7000,
              (free_acc.enabled, free_acc.auto_disabled_for()))

        queue_acc = A.Account({"uid": "env403-2", "realm": "intl",
                               "accessToken": "dt-y"})
        queue_acc.save(tmp2)
        pool.accounts.append(queue_acc)
        exc2 = types.SimpleNamespace(status=403, detail=QUEUE_403)
        P._handle_envelope_account_cooldown(queue_acc, exc2,
                                            model="Qwen3.8-Flash",
                                            session_key=None)
        # 排队仍然只做模型级冷却（免费模型也一样）
        check("信封 403（10605 排队）-> **不**禁用，只冷却",
              queue_acc.enabled is True and queue_acc.disabled_until == 0
              and queue_acc.model_cooldowns.get("Qwen3.8-Flash", 0) > time.time(),
              (queue_acc.enabled, queue_acc.disabled_until))

        policy_acc = A.Account({"uid": "env403-3", "realm": "intl",
                                "accessToken": "dt-z"})
        policy_acc.save(tmp2)
        pool.accounts.append(policy_acc)
        exc3 = types.SimpleNamespace(status=403, detail=POLICY_403)
        P._handle_envelope_account_cooldown(policy_acc, exc3,
                                            model="Qwen3.8-Flash",
                                            session_key=None)
        check("信封 403（内容审核）-> 不禁用",
              policy_acc.enabled is True and policy_acc.disabled_until == 0,
              (policy_acc.enabled, policy_acc.disabled_until))
    finally:
        P.POOL = real_pool
        shutil.rmtree(tmp2, ignore_errors=True)

    # 手工启用覆盖自动禁用
    tmp3 = tempfile.mkdtemp(prefix="qd-403-man-")
    try:
        pool = A.AccountPool(tmp3, log=lambda *a, **k: None)
        acc = A.Account({"uid": "man-1", "realm": "intl",
                         "accessToken": "dt-x"})
        acc.save(tmp3)
        pool.accounts.append(acc)
        acc.auto_disable(7200, "test reason")
        pool.set_enabled("man-1", True)
        check("手工启用会清掉自动禁用窗口",
              acc.enabled is True and acc.disabled_until == 0
              and acc.ready() is True,
              (acc.enabled, acc.disabled_until, acc.ready()))
    finally:
        shutil.rmtree(tmp3, ignore_errors=True)

    # ---- HTTP 层（open_upstream 分类）也要走同一条规则 ----
    class _Sess(object):
        def headers(self, *a, **k):
            return {"Content-Type": "application/json"}

    def run_http403(detail, model):
        tmp4 = tempfile.mkdtemp(prefix="qd-403-http-")
        real = (P.urllib, P.SESSIONS, P.time, P.POOL)
        try:
            pool = A.AccountPool(tmp4, log=lambda *a, **k: None)
            acc = A.Account({"uid": "http403-1", "realm": "intl",
                             "accessToken": "dt-x"})
            acc.save(tmp4)
            pool.accounts.append(acc)
            fake_time = types.SimpleNamespace(
                **dict((k, getattr(time, k)) for k in dir(time)
                       if not k.startswith("_")))
            fake_time.sleep = lambda s: None
            P.urllib = types.SimpleNamespace(
                request=types.SimpleNamespace(
                    Request=real[0].request.Request,
                    urlopen=lambda *a, **k: (_ for _ in ()).throw(
                        urllib.error.HTTPError(
                            "https://u.invalid", 403, "err", {},
                            io.BytesIO(detail.encode("utf-8"))))),
                error=real[0].error, parse=real[0].parse)
            P.SESSIONS = types.SimpleNamespace(get=lambda a: _Sess())
            P.time = fake_time
            P.POOL = pool
            try:
                P.open_upstream({"model": model,
                                 "messages": [{"role": "user",
                                               "content": "hi"}]},
                                session_key="k-403", target_realm="intl")
            except Exception:
                pass
            return acc
        finally:
            P.urllib, P.SESSIONS, P.time, P.POOL = real
            shutil.rmtree(tmp4, ignore_errors=True)

    acc_paid = run_http403(PRICING_403, "Qwen3.8-Max")
    check("HTTP 403（付费模型 112）-> 不冷却、不禁用",
          acc_paid.enabled is True and acc_paid.disabled_until == 0
          and acc_paid.cooldown_until <= time.time()
          and not acc_paid.model_cooldowns,
          (acc_paid.enabled, acc_paid.cooldown_until - time.time(),
           dict(acc_paid.model_cooldowns)))
    acc_free = run_http403('{"code":"403","message":"account_banned"}',
                           "Qwen3.8-Flash")
    check("HTTP 403（免费模型）-> 自动禁用 2h",
          acc_free.enabled is False and acc_free.auto_disabled_for() > 7000,
          (acc_free.enabled, acc_free.auto_disabled_for()))

    # ---- 模型级冷却要能在面板看到（否则「可用」却打不通）----
    tmp5 = tempfile.mkdtemp(prefix="qd-403-mcd-")
    try:
        acc = A.Account({"uid": "mcd-1", "realm": "intl",
                         "accessToken": "dt-x"})
        acc.model_cooldowns["Qwen3.8-Max"] = time.time() + 7200
        acc.model_cooldowns["Expired-Model"] = time.time() - 10
        view = acc.public()
        check("public() 暴露模型级冷却",
              "Qwen3.8-Max" in (view.get("modelCooldowns") or {}),
              view.get("modelCooldowns"))
        check("已过期的模型冷却不再展示",
              "Expired-Model" not in (view.get("modelCooldowns") or {}),
              view.get("modelCooldowns"))
        check("账号本身仍可用（模型冷却不影响 enabled）",
              view["enabled"] is True and view["autoDisabledFor"] == 0,
              (view["enabled"], view["autoDisabledFor"]))
        check("模型冷却剩余约 7200s",
              (view.get("modelCooldowns") or {}).get("Qwen3.8-Max", 0) > 7100,
              view.get("modelCooldowns"))
    finally:
        shutil.rmtree(tmp5, ignore_errors=True)

    html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    check("面板展示自动禁用状态",
          "autoDisabledFor" in html and "自动禁用" in html)
    check("面板说明会自动恢复", "自动恢复启用" in html)
    check("面板展示模型级冷却", "modelCooldowns" in html and "模型冷却中" in html)

    print("")
    print("SUMMARY: TOTAL %d checks, %d passed, %d failed"
          % (PASS + FAIL, PASS, FAIL))
    print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN",
                                    1 if FAIL else 0))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
