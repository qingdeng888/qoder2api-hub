"""模型连通性测试（/diag/model + probe_model_connectivity）套件。

    python tests/_test_model_diag.py

全部离线：monkeypatch open_upstream / aggregate_stream，验证成功路径、
10605 排队、112 付费墙、限流异常、空模型与面板接线。
"""
import io
import json
import os
import sys
import urllib.error

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


class _Account(object):
    uid = "acct-diag-1"
    nickname = "诊断账号"
    realm = "intl"


class _Resp(object):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(code, body):
    return urllib.error.HTTPError(
        "https://api1.qoder.sh/chat", code, "upstream error", {},
        io.BytesIO(body.encode("utf-8")))


def main():
    print("[model-diag] 模型连通性测试")
    orig_open = P.open_upstream
    orig_agg = P.aggregate_stream
    try:
        calls = {}

        def fake_open(payload, **kwargs):
            calls["payload"] = payload
            calls["kwargs"] = kwargs
            return _Resp(), _Account(), "encoded"

        def fake_agg(resp, model, resp_id=None, holder=None,
                     allowed_names=None):
            return {"choices": [{"message": {"content": "pong"}}],
                    "usage": {"total_tokens": 3}}

        P.open_upstream = fake_open
        P.aggregate_stream = fake_agg
        result = P.probe_model_connectivity(
            "Qwen3.8-Flash", realm="intl", prompt="hi", max_tokens=8)
        check("成功路径 ok=True 且返回 pong",
              result["ok"] and result["reply"] == "pong", result)
        check("诊断请求限制 max_tokens=8",
              calls["payload"]["max_tokens"] == 8, calls["payload"])
        check("诊断请求不等待上游排队（allow_queue_wait=False）",
              calls["kwargs"].get("allow_queue_wait") is False,
              calls["kwargs"])
        check("返回实际账号（uid/昵称）",
              result["account"]["uid"] == "acct-diag-1"
              and result["account"]["nickname"] == "诊断账号",
              result.get("account"))
        check("成功结果带耗时与 realm",
              isinstance(result["elapsed_ms"], int)
              and result["realm"] == "intl", result)

        queue_detail = json.dumps({
            "code": "403",
            "message": json.dumps({
                "code": "10605",
                "message": json.dumps({
                    "isQueued": True, "modelKey": "qfmodel",
                    "queueType": "p3", "retryAfterSeconds": 30,
                    "serviceAvailable": False,
                    "subCode": "RESOURCE_LIMIT",
                }),
                "subCode": "RESOURCE_LIMIT",
            }),
        })

        def raise_queue(*args, **kwargs):
            raise _http_error(403, queue_detail)

        P.open_upstream = raise_queue
        waited = P.probe_model_connectivity("Qwen3.8-Flash", realm="intl")
        check("10605 排队 -> ok=False / status=403",
              not waited["ok"] and waited["status"] == 403, waited)
        check("10605 错误文案提示排队/资源受限",
              "排队" in waited["error"] or "资源" in waited["error"],
              waited.get("error"))
        check("10605 保留上游详情",
              "10605" in (waited.get("detail") or ""), waited.get("detail"))

        pricing_detail = json.dumps({
            "code": "112",
            "message": json.dumps({"pricingUrl": "https://qoder.com/pricing"}),
        })

        def raise_pricing(*args, **kwargs):
            raise _http_error(403, pricing_detail)

        P.open_upstream = raise_pricing
        pricing = P.probe_model_connectivity("Qwen3.8-Max", realm="intl")
        check("112 付费墙 -> 文案提示升级/充值",
              not pricing["ok"] and "付费" in pricing["error"],
              pricing.get("error"))

        def raise_rate(*args, **kwargs):
            raise P.RateLimited(detail="all accounts cooling")

        P.open_upstream = raise_rate
        rate = P.probe_model_connectivity("Qwen3.8-Flash", realm="intl")
        check("RateLimited -> status=429",
              not rate["ok"] and rate["status"] == 429, rate)

        P.open_upstream = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("boom"))
        broken = P.probe_model_connectivity("Qwen3.8-Flash", realm="intl")
        check("未知异常 -> status=502 且不抛出",
              not broken["ok"] and broken["status"] == 502, broken)

        empty = P.probe_model_connectivity("", realm="intl")
        check("空模型 -> 400", not empty["ok"] and empty["status"] == 400,
              empty)

        P.open_upstream = orig_open
        P.aggregate_stream = orig_agg
        check("诊断限流：20 次内放行、第 21 次拒绝",
              all(P.allow_model_diag("diag-test-ip") for _ in range(20))
              and P.allow_model_diag("diag-test-ip") is False)
    finally:
        P.open_upstream = orig_open
        P.aggregate_stream = orig_agg

    html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    for token in ("/diag/model", "testModelConnectivity", "data-model",
                  "连通性"):
        check("面板接线: %s" % token, token in html)

    print("")
    print("SUMMARY: TOTAL %d checks, %d passed, %d failed"
          % (PASS + FAIL, PASS, FAIL))
    print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN",
                                    1 if FAIL else 0))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
