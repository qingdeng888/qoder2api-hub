"""API 连通性测试（Playground：本地 Key -> 网关 -> 上游）套件。

    python tests/_test_api_connectivity.py

用本地假网关验证 run_api_connectivity_test 的真实 HTTP 链路、错误透传、
本地 Key 选择、限流与前端接线；不访问公网。
"""
import http.server
import json
import os
import sys
import threading
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


class _FakeGateway(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        _FakeGateway.seen.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "diag": self.headers.get("X-Qoder-Diag"),
            "body": body,
        })
        if body.get("model") == "paid-model":
            payload = {"error": {"message": "该模型需要付费套餐",
                                 "type": "upstream_error", "code": 403}}
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        payload = {
            "choices": [{"message": {"content": "你好，我在"}}],
            "usage": {"total_tokens": 6},
        }
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Qoder-Realm", "intl")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def main():
    print("[api-connectivity] API 连通性测试")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeGateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    orig_key = P._local_api_test_key
    try:
        P._local_api_test_key = lambda model: "local-test-key"
        _FakeGateway.seen[:] = []
        result = P.run_api_connectivity_test(
            "Qwen3.8-Flash", "你是谁，出来干活了",
            max_tokens=32, port=port)
        check("成功：ok=True 且解析回复",
              result["ok"] and result["reply"] == "你好，我在", result)
        check("成功：状态码/耗时/realm",
              result["status"] == 200
              and isinstance(result["elapsed_ms"], int)
              and result["realm"] == "intl", result)
        seen = _FakeGateway.seen[-1] if _FakeGateway.seen else {}
        check("真实发到本网关 /v1/chat/completions",
              seen.get("path") == "/v1/chat/completions", seen.get("path"))
        check("携带本地 API Key",
              seen.get("auth") == "Bearer local-test-key",
              seen.get("auth"))
        check("携带诊断头（不等待长排队）",
              seen.get("diag") == "1", seen.get("diag"))
        check("模型/消息/max_tokens 正确传入",
              seen.get("body", {}).get("model") == "Qwen3.8-Flash"
              and seen.get("body", {}).get("messages", [{}])[0].get("content")
              == "你是谁，出来干活了"
              and seen.get("body", {}).get("max_tokens") == 32,
              seen.get("body"))

        failed = P.run_api_connectivity_test(
            "paid-model", "test", max_tokens=16, port=port)
        check("上游 403 -> ok=False / status=403",
              not failed["ok"] and failed["status"] == 403, failed)
        # 诊断跑完后，真实上游 403 必须留下可观测的冷却痕迹，否则「为什么没冷却」
        # 永远只能靠翻日志猜。这里只断言 403 原样透出，冷却由 403 套件覆盖。
        check("上游错误消息透传", "付费" in (failed.get("error") or ""),
              failed.get("error"))

        P._local_api_test_key = lambda model: ""
        no_key = P.run_api_connectivity_test(
            "Qwen3.8-Flash", "test", max_tokens=16, port=port)
        check("没有本地 Key -> 明确提示",
              not no_key["ok"] and "API Key" in (no_key.get("error") or ""),
              no_key)

        P._local_api_test_key = lambda model: "local-test-key"
        # 空模型 -> 用免费默认模型（Qwen3.8-Flash），而不是 400
        empty = P.run_api_connectivity_test(
            "", "test", max_tokens=16, port=port)
        check("空模型 -> 自动使用免费默认模型",
              empty.get("model") == P.DIAG_DEFAULT_MODEL, empty.get("model"))
        check("空模型 -> 真的发出请求并成功",
              empty["ok"] is True, empty)
        seen = _FakeGateway.seen[-1] if _FakeGateway.seen else {}
        check("空模型时下游请求体用默认模型",
              seen.get("body", {}).get("model") == P.DIAG_DEFAULT_MODEL,
              seen.get("body", {}).get("model"))
        check("默认诊断模型是免费模型 Qwen3.8-Flash",
              P.DIAG_DEFAULT_MODEL == "Qwen3.8-Flash", P.DIAG_DEFAULT_MODEL)

        explicit = P.run_api_connectivity_test(
            "Qwen3.8-Max", "test", max_tokens=16, port=port)
        check("显式指定模型时不被默认值覆盖",
              explicit.get("model") == "Qwen3.8-Max", explicit.get("model"))

        closed = P.run_api_connectivity_test(
            "Qwen3.8-Flash", "test", max_tokens=16, port=1)
        check("网关不可达 -> status=0 且给出错误",
              not closed["ok"] and closed["status"] == 0
              and "无法连接" in (closed.get("error") or ""), closed)

        check("限流：10 次内放行、第 11 次拒绝",
              all(P.allow_api_connectivity_test("api-diag-ip")
                  for _ in range(10))
              and P.allow_api_connectivity_test("api-diag-ip") is False)

        # 诊断请求不应为了 10605 在信封层干等（thread-local 标记）
        queue_detail = ('{"code":"403","message":"{\\"code\\":\\"10605\\",'
                        '\\"retryAfterSeconds\\":30}"}')
        fake_queue = P.UpstreamStatus(403, queue_detail)
        P.set_diag_request(False)
        normal_retry = P.should_retry_envelope(fake_queue, False, 0)
        P.set_diag_request(True)
        diag_retry = P.should_retry_envelope(fake_queue, False, 0)
        P.set_diag_request(False)
        check("10605 在正常请求里会重试（等待队列）",
              normal_retry is True, normal_retry)
        check("诊断请求不重试 10605（立即返回真实状态）",
              diag_retry is False, diag_retry)
        check("标记可重置（keep-alive 复用不会串到下一个请求）",
              P.is_diag_request() is False)
    finally:
        P._local_api_test_key = orig_key
        server.shutdown()

    html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    for token in ("sectionApiConnectivity", "apiTestModel", "apiTestMessage",
                  "runApiConnectivityTest", "/diag/connectivity",
                  "发送测试请求"):
        check("面板接线: %s" % token, token in html)

    print("")
    print("SUMMARY: TOTAL %d checks, %d passed, %d failed"
          % (PASS + FAIL, PASS, FAIL))
    print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN",
                                    1 if FAIL else 0))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
