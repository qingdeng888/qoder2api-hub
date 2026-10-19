"""出站代理池套件（qoder_proxypool / 统一出站接入）。

    python tests/_test_proxy_pool.py

覆盖：
  - 配置读写 / 归一化 / 去重 / 脱敏 / 0600 权限
  - rotate / sticky / random / single 选路、exclude、冷却、直连兜底
  - 传输层失败分类、健康检查（真实本地 HTTP 代理 + 目标站）
  - qoder_accounts.http_json 在池启用时确实经代理出站
"""
import http.client
import http.server
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import qoder_proxypool as PP          # noqa: E402
import qoder_accounts as A            # noqa: E402

PASS = FAIL = 0


def check(label, ok, detail=None):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %r" % (label, detail))


class _Target(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                  # noqa: N802
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Proxy(http.server.BaseHTTPRequestHandler):
    """最小 HTTP 正向代理：只实现测试需要的绝对 URI GET。"""

    seen = []

    def do_GET(self):                                  # noqa: N802
        _Proxy.seen.append(self.path)
        parsed = urllib.parse.urlsplit(self.path)
        target_path = urllib.parse.urlunsplit(
            ("", "", parsed.path or "/", parsed.query, ""))
        connection = http.client.HTTPConnection(
            parsed.hostname, parsed.port, timeout=5)
        headers = {name: value for name, value in self.headers.items()
                   if name.lower() not in
                   ("proxy-connection", "connection", "host")}
        try:
            connection.request("GET", target_path, headers=headers)
            response = connection.getresponse()
            body = response.read()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in ("connection", "content-length",
                                        "transfer-encoding"):
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self.send_error(502, str(exc))
        finally:
            connection.close()

    def log_message(self, *args):
        pass


def _serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


# ---------------------------------------------------------------------------
# 最小 SOCKS4a / SOCKS5 代理（仅测试用）
# ---------------------------------------------------------------------------
def _recvn(conn, count):
    chunks = []
    while count > 0:
        chunk = conn.recv(count)
        if not chunk:
            raise OSError("eof")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def _recv_cstr(conn):
    data = b""
    while True:
        char = _recvn(conn, 1)
        if char == b"\x00":
            return data
        data += char


def _pipe_sockets(a, b):
    def copy(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    thread = threading.Thread(target=copy, args=(a, b), daemon=True)
    thread.start()
    copy(b, a)
    thread.join(timeout=2)


def _socks5_handler(conn, require_auth=False):
    try:
        greeting = _recvn(conn, 2)
        methods = _recvn(conn, greeting[1])
        if require_auth:
            if 0x02 not in methods:
                conn.sendall(b"\x05\xff")
                return
            conn.sendall(b"\x05\x02")
            _recvn(conn, 1)
            username = _recvn(conn, _recvn(conn, 1)[0])
            password = _recvn(conn, _recvn(conn, 1)[0])
            if (username, password) != (b"user", b"pass"):
                conn.sendall(b"\x01\x01")
                return
            conn.sendall(b"\x01\x00")
        else:
            conn.sendall(b"\x05\x00")
        request = _recvn(conn, 4)
        atyp = request[3]
        if atyp == 1:
            host = socket.inet_ntoa(_recvn(conn, 4))
        elif atyp == 3:
            host = _recvn(conn, _recvn(conn, 1)[0]).decode()
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, _recvn(conn, 16))
        else:
            conn.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
            return
        port = struct.unpack("!H", _recvn(conn, 2))[0]
        upstream = socket.create_connection((host, port), timeout=5)
        conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        _pipe_sockets(conn, upstream)
        upstream.close()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _socks4_handler(conn):
    try:
        header = _recvn(conn, 8)
        port = struct.unpack("!H", header[2:4])[0]
        ip = header[4:8]
        _recv_cstr(conn)                       # userid
        if ip[:3] == b"\x00\x00\x00" and ip[3] != 0:
            host = _recv_cstr(conn).decode()   # SOCKS4a 域名
        else:
            host = socket.inet_ntoa(ip)
        upstream = socket.create_connection((host, port), timeout=5)
        conn.sendall(b"\x00\x5a" + struct.pack("!H", port)
                     + b"\x00\x00\x00\x00")
        _pipe_sockets(conn, upstream)
        upstream.close()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _serve_socks(handler, *args):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(16)

    def loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,) + args,
                             daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return server, server.getsockname()[1]


def main():
    print("[proxy-pool] 出站代理池")

    # 本地代理/目标站（健康检查与真实出站都用它们，不依赖公网）
    target_server, target_port = _serve(_Target)
    proxy_server, proxy_port = _serve(_Proxy)
    target_url = "http://127.0.0.1:%d/data" % target_port
    proxy_url = "http://127.0.0.1:%d" % proxy_port

    temp_dir = tempfile.mkdtemp(prefix="qd-proxy-pool-")
    old_env = {key: os.environ.get(key) for key in
               ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")}
    for key in old_env:
        os.environ.pop(key, None)
    old_validate = A.validate_public_http_url
    A.validate_public_http_url = lambda url: None

    try:
        PP.configure(temp_dir)
        view = PP.status_view()
        check("默认关闭 + 没有代理", view["enabled"] is False
              and view["proxies"] == [], view)
        opener, entry, kind = PP.acquire()
        check("未启用时 acquire -> env（沿用原行为）",
              opener is None and entry is None and kind == "env", kind)

        result = PP.add_proxies([
            "香港-01|http://user:pass@127.0.0.1:%d" % proxy_port,
            "本地-02|http://127.0.0.1:%d" % proxy_port,
            "http://127.0.0.1:%d" % proxy_port,          # 同一地址 -> 跳过
            "# 这是注释",
            "socks6://127.0.0.1:1080",                   # 未知协议
            "127.0.0.1:%d" % proxy_port,                 # 补 scheme 后仍重复
        ])
        check("批量添加：2 条入库、2 条重复跳过、1 条非法拒绝",
              result["added"] == 2 and len(result["skipped"]) == 2
              and len(result["rejected"]) == 1, result)
        check("无 scheme 的地址补成 http://",
              PP.normalize_proxy_url("127.0.0.1:9002")
              == "http://127.0.0.1:9002")
        if os.name == "nt":
            print("  [SKIP] 配置文件权限 0600（Windows 不适用）")
        else:
            mode = oct(os.stat(PP.config_path()).st_mode & 0o777)
            check("配置文件权限 0600", mode == "0o600", mode)

        PP.update_config({"enabled": True, "mode": "rotate",
                          "health_check": {"enabled": False,
                                           "target": target_url,
                                           "timeout_sec": 5},
                          "direct_fallback": True})
        view = PP.status_view()
        check("启用后 status_view 生效", view["enabled"] is True
              and view["summary"]["total"] == 2, view["summary"])

        first = PP.acquire()[1]["id"]
        second = PP.acquire()[1]["id"]
        third = PP.acquire()[1]["id"]
        check("rotate：两次不同、第三次回到第一个",
              first != second and third == first, (first, second, third))

        PP.update_config({"mode": "sticky"})
        sticky_a = set(PP.acquire(affinity="account-a")[1]["id"]
                       for _ in range(6))
        sticky_b = set(PP.acquire(affinity="account-b")[1]["id"]
                       for _ in range(6))
        check("sticky：同一账号固定同一代理（各 6 次只有 1 个）",
              len(sticky_a) == 1 and len(sticky_b) == 1,
              (sticky_a, sticky_b))

        PP.update_config({"mode": "rotate"})
        only = PP.acquire(exclude={first})[1]["id"]
        check("exclude：跳过已试过的代理", only == second, only)

        # 冷却：连续 3 次传输失败 -> cooling；冷却期内不再选中
        cfg = PP.get_config(force=True)
        victim = cfg["proxies"][0]
        for _ in range(3):
            PP.report_failure(victim, urllib.error.URLError("boom"))
        view = PP.status_view()
        row = [item for item in view["proxies"]
               if item["id"] == victim["id"]][0]
        check("连续失败达到阈值 -> cooling", row["state"] == "cooling", row)
        picked = set(PP.acquire()[1]["id"] for _ in range(6))
        check("冷却中的代理不再被选中", victim["id"] not in picked, picked)
        PP.report_success(victim)
        check("成功一次 -> 立即恢复 healthy",
              PP.status_view()["proxies"][0]["state"] != "cooling",
              PP.status_view()["proxies"])

        PP.update_config({"direct_fallback": False})
        cfg = PP.get_config(force=True)
        for entry in cfg["proxies"]:
            for _ in range(3):
                PP.report_failure(entry, urllib.error.URLError("down"))
        _, entry, kind = PP.acquire()
        check("全部冷却 + 关闭直连兜底 -> 仍选最早冷却的代理（不直接失败）",
              kind == "proxy" and entry is not None, kind)
        for entry in cfg["proxies"]:
            PP.set_proxies_enabled([entry["id"]], False)
        _, _, kind = PP.acquire()
        check("全部停用 + 关闭直连兜底 -> exhausted", kind == "exhausted", kind)
        for entry in cfg["proxies"]:
            PP.set_proxies_enabled([entry["id"]], True)
        PP.update_config({"direct_fallback": True})
        _, _, kind = PP.acquire()
        check("全部冷却 + 开启直连兜底 -> direct", kind == "direct", kind)

        check("HTTP 500 不算代理失败",
              PP.is_transport_error(urllib.error.HTTPError(
                  target_url, 500, "err", {}, None)) is False)
        check("HTTP 407（代理认证失败）算代理失败",
              PP.is_transport_error(urllib.error.HTTPError(
                  target_url, 407, "auth", {}, None)) is True)
        check("URLError 算传输失败",
              PP.is_transport_error(urllib.error.URLError("refused")) is True)

        masked = [row["address"] for row in PP.status_view()["proxies"]]
        check("状态视图隐藏代理密码",
              all("user:pass" not in address for address in masked), masked)

        # 真实链路：健康检查经本地代理访问本地目标站
        PP.reset_stats()
        PP.update_config({"enabled": True, "direct_fallback": True,
                          "mode": "rotate"})
        results = PP.test_proxies()
        ok_count = sum(1 for item in results if item["ok"])
        check("健康检查经代理可达（2 条都通）", ok_count == 2, results)
        check("测试结果地址已脱敏",
              all("user:pass" not in item["address"] for item in results),
              results)

        # 真实链路：qoder_accounts.http_json 走代理池
        PP.update_config({"mode": "rotate"})
        _Proxy.seen[:] = []
        payload = A.http_json(target_url, timeout=5, retries=1)
        check("http_json 经代理出站成功", payload == {"ok": True}, payload)
        check("代理确实收到该请求", any(target_url in item
                                        for item in _Proxy.seen), _Proxy.seen)

        # 真实链路：qoder_proxy 的推理/模型/更新统一出口 _open_upstream
        import qoder_proxy as QP                    # noqa: E402
        _Proxy.seen[:] = []
        upstream = QP._open_upstream(
            urllib.request.Request(target_url), 5, affinity="upstream-test")
        try:
            body = upstream.read()
            status = upstream.status
        finally:
            upstream.close()
        check("qoder_proxy._open_upstream 经代理出站",
              status == 200 and body == b'{"ok": true}', (status, body))
        check("_open_upstream 确实经过代理",
              any(target_url in item for item in _Proxy.seen), _Proxy.seen)

        # ---- SOCKS5 / SOCKS4a 真实链路（本地最小 SOCKS 服务）----
        def use_single(proxy_value):
            current = PP.get_config(force=True)["proxies"]
            if current:
                PP.delete_proxies([entry["id"] for entry in current])
            PP.add_proxies([proxy_value])
            PP.update_config({
                "enabled": True, "mode": "single", "direct_fallback": False,
                "health_check": {"enabled": False, "target": target_url,
                                 "timeout_sec": 5},
            })
            PP.reset_stats()
            return PP.get_config(force=True)["proxies"][0]["id"]

        socks5_server, socks5_port = _serve_socks(_socks5_handler)
        use_single("socks5://127.0.0.1:%d" % socks5_port)
        payload = A.http_json(target_url, timeout=5, retries=1)
        check("SOCKS5 无认证：http_json 经隧道出站", payload == {"ok": True},
              payload)
        results = PP.test_proxies()
        check("SOCKS5 健康检查可达", bool(results) and results[0]["ok"],
              results)
        check("SOCKS5 地址展示保留 scheme",
              PP.status_view()["proxies"][0]["address"].startswith("socks5://"),
              PP.status_view()["proxies"][0]["address"])

        socks5a_server, socks5a_port = _serve_socks(_socks5_handler, True)
        use_single("socks5://user:pass@127.0.0.1:%d" % socks5a_port)
        payload = A.http_json(target_url, timeout=5, retries=1)
        check("SOCKS5 用户名/密码认证出站", payload == {"ok": True}, payload)
        check("SOCKS5 认证信息在状态视图脱敏",
              "user:pass" not in PP.status_view()["proxies"][0]["address"],
              PP.status_view()["proxies"][0]["address"])

        socks4_server, socks4_port = _serve_socks(_socks4_handler)
        use_single("socks4a://user@127.0.0.1:%d" % socks4_port)
        payload = A.http_json(target_url, timeout=5, retries=1)
        check("SOCKS4a 域名交给代理：http_json 出站",
              payload == {"ok": True}, payload)

        use_single("socks4://127.0.0.1:%d" % socks4_port)
        localhost_url = "http://localhost:%d/data" % target_port
        payload = A.http_json(localhost_url, timeout=5, retries=1)
        check("SOCKS4 本地解析域名：http_json 出站",
              payload == {"ok": True}, payload)

        check("socks5 URL 归一化",
              PP.normalize_proxy_url("socks5://u:p@1.2.3.4:1080")
              == "socks5://u:p@1.2.3.4:1080")
        try:
            PP.normalize_proxy_url("socks6://1.2.3.4:1080")
            rejected = False
        except ValueError:
            rejected = True
        check("未知 socks6 scheme 被拒绝", rejected)

        # 环境变量 SOCKS：池关闭时也生效
        current = PP.get_config(force=True)["proxies"]
        if current:
            PP.delete_proxies([entry["id"] for entry in current])
        PP.update_config({"enabled": False})
        os.environ["HTTPS_PROXY"] = "socks5://127.0.0.1:%d" % socks5_port
        payload = A.http_json(target_url, timeout=5, retries=1)
        check("环境变量 HTTPS_PROXY=socks5:// 出站", payload == {"ok": True},
              payload)
        os.environ.pop("HTTPS_PROXY", None)

        for server in (socks5_server, socks5a_server, socks4_server):
            server.close()

        # 恢复成两条 HTTP 测试代理，供后面的持久化/删除断言使用
        PP.add_proxies([
            "香港-01|http://user:pass@127.0.0.1:%d" % proxy_port,
            "本地-02|http://127.0.0.1:%d" % proxy_port,
        ])
        PP.update_config({"enabled": True, "mode": "rotate",
                          "direct_fallback": True})

        # 配置持久化：重新从磁盘加载仍是 2 条
        PP.configure(temp_dir)
        reloaded = PP.reload()
        check("重新加载配置后代理仍在", len(reloaded["proxies"]) == 2,
              reloaded["proxies"])

        # 删除 / 启停
        first_id = reloaded["proxies"][0]["id"]
        PP.set_proxies_enabled([first_id], False)
        check("停用单个代理",
              PP.status_view()["proxies"][0]["enabled"] is False)
        PP.delete_proxies([first_id])
        check("删除单个代理后只剩 1 条",
              len(PP.get_config(force=True)["proxies"]) == 1)

        # 多选批量操作（面板勾选多行 -> 批量启用/停用/测试/删除/清冷却）
        PP.add_proxies([
            "BULK-1|http://bulk1:pass@127.0.0.1:%d" % proxy_port,
            "BULK-2|http://bulk2:pass@127.0.0.1:%d" % proxy_port,
            "BULK-3|http://bulk3:pass@127.0.0.1:%d" % proxy_port,
        ])
        bulk_ids = [entry["id"] for entry in PP.get_config(force=True)["proxies"]
                    if entry.get("label", "").startswith("BULK-")]
        check("批量添加 3 条用于多选操作", len(bulk_ids) == 3, bulk_ids)

        result = PP.set_proxies_enabled(bulk_ids[:2], False)
        view = {entry["id"]: entry for entry in PP.status_view()["proxies"]}
        check("批量停用 2 条",
              set(result["changed"]) == set(bulk_ids[:2])
              and all(view[item]["enabled"] is False for item in bulk_ids[:2])
              and view[bulk_ids[2]]["enabled"] is True, result)
        result = PP.set_proxies_enabled(bulk_ids[:2], True)
        check("批量启用 2 条", set(result["changed"]) == set(bulk_ids[:2]),
              result)

        results = PP.test_proxies(bulk_ids[:2])
        check("批量测试只返回选中的 2 条",
              len(results) == 2 and all(item["ok"] for item in results),
              results)
        results = PP.test_proxies([bulk_ids[0]])
        check("多选接口支持单条", len(results) == 1 and results[0]["ok"],
              results)

        cfg = PP.get_config(force=True)
        target_entry = [entry for entry in cfg["proxies"]
                        if entry["id"] == bulk_ids[0]][0]
        for _ in range(3):
            PP.report_failure(target_entry, urllib.error.URLError("bulk"))
        result = PP.reset_stats([bulk_ids[0]])
        row = [entry for entry in PP.status_view()["proxies"]
               if entry["id"] == bulk_ids[0]][0]
        check("清除选中代理的冷却",
              result.get("reset") == 1 and row["state"] != "cooling",
              (result, row["state"]))

        result = PP.delete_proxies(bulk_ids[:2])
        check("批量删除 2 条", result["removed"] == 2
              and len(PP.get_config(force=True)["proxies"]) == 2, result)
        result = PP.delete_proxies([bulk_ids[2]])
        check("再删除最后 1 条", result["removed"] == 1
              and len(PP.get_config(force=True)["proxies"]) == 1, result)

        # ---- 账号级出口绑定（Qoder 账号 ≈ 参考项目上游 Key）----
        PP.add_proxies(["SECOND|http://second:pass@127.0.0.1:%d" % proxy_port])
        cfg = PP.get_config(force=True)
        first, second = cfg["proxies"][0], cfg["proxies"][1]
        PP.update_config({"enabled": True, "mode": "rotate",
                          "direct_fallback": False,
                          "health_check": {"enabled": False,
                                           "target": target_url}})
        PP.reset_stats()

        bind = {"mode": "bind", "proxy_id": first["id"], "key": "acct-1"}
        _, entry, kind = PP.acquire(binding=bind)
        check("账号 bind：固定到指定代理",
              kind == "proxy" and entry["id"] == first["id"], (kind, entry))
        for _ in range(3):
            PP.report_failure(first, urllib.error.URLError("bound-down"))
        _, entry, kind = PP.acquire(binding=bind)
        check("账号 bind：代理冷却时仍固定（不偷偷换出口）",
              kind == "proxy" and entry["id"] == first["id"], (kind, entry))
        _, _, kind = PP.acquire(binding=bind, exclude={first["id"]})
        check("账号 bind：本次已试过后明确失败", kind == "exhausted", kind)
        PP.reset_stats()

        _, entry, kind = PP.acquire(binding={"mode": "bind",
                                             "proxy_id": "does-not-exist",
                                             "key": "acct-1"})
        check("账号 bind：代理不存在时回落默认策略",
              kind == "proxy" and entry["id"] in (first["id"], second["id"]),
              (kind, entry))

        _, _, kind = PP.acquire(binding={"mode": "direct", "key": "acct-1"})
        check("账号 direct：强制直连", kind == "direct", kind)

        PP.update_config({"mode": "rotate"})
        auto_ids = set()
        for _ in range(6):
            _, entry, _ = PP.acquire(binding={"mode": "auto", "key": "acct-auto"})
            auto_ids.add(entry["id"])
        check("账号 auto：同一账号固定同一代理", len(auto_ids) == 1, auto_ids)

        rotate_ids = set()
        for _ in range(4):
            _, entry, _ = PP.acquire(binding={"mode": "rotate",
                                              "key": "acct-rotate"})
            rotate_ids.add(entry["id"])
        check("账号 rotate：每次请求轮换", len(rotate_ids) >= 2, rotate_ids)

        account_dir = os.path.join(temp_dir, "accounts")
        acct = A.Account({
            "uid": "acct-bind-1", "nickname": "绑定测试", "realm": "cn",
            "accessToken": "dt-test", "proxyMode": "bind",
            "proxyId": first["id"],
        })
        acct.save(account_dir)
        pool2 = A.AccountPool(account_dir)
        pool2.load()
        persisted = pool2.get("acct-bind-1")
        check("账号 JSON 持久化 proxyMode / proxyId",
              persisted is not None and persisted.proxy_mode == "bind"
              and persisted.proxy_id == first["id"],
              persisted.public() if persisted else None)
        pool2.set_proxy_binding("acct-bind-1", "rotate", "")
        pool3 = A.AccountPool(account_dir)
        pool3.load()
        updated = pool3.get("acct-bind-1")
        check("AccountPool.set_proxy_binding 落盘",
              updated.proxy_mode == "rotate" and updated.proxy_id == "",
              (updated.proxy_mode, updated.proxy_id))
        pool2.set_proxy_binding("acct-bind-1", "bind", first["id"])
        bound_account = pool2.get("acct-bind-1")
        _Proxy.seen[:] = []
        payload = A.http_json(target_url, timeout=5, retries=1,
                              account=bound_account)
        check("http_json(account=) 按账号 bind 出站",
              payload == {"ok": True}
              and any(target_url in item for item in _Proxy.seen),
              (payload, _Proxy.seen))

        _Proxy.seen[:] = []
        upstream = QP._open_upstream(
            urllib.request.Request(target_url), 5, affinity="acct-bind-1",
            binding=bound_account.proxy_binding())
        try:
            body = upstream.read()
            status = upstream.status
        finally:
            upstream.close()
        check("qoder_proxy._open_upstream 按账号绑定出站",
              status == 200 and body == b'{"ok": true}'
              and any(target_url in item for item in _Proxy.seen),
              (status, body, _Proxy.seen))

        dashboard = open(os.path.join(ROOT, "dashboard.html"),
                         encoding="utf-8").read()
        for token in ("ppSelectAll", "pp-row-check", "bulkToggleProxies",
                      "bulkTestProxies", "bulkResetProxies",
                      "bulkDeleteProxies", "selectedProxyIds",
                      "accountProxyList", "renderAccountProxyBindings",
                      "saveAccountProxyBinding", "syncAccountProxyMode",
                      "/accounts/proxy-binding"):
            check("面板多选批量控件接线: %s" % token, token in dashboard)
    finally:
        PP.stop_health_thread()
        PP.configure(ROOT + "/accounts")
        A.validate_public_http_url = old_validate
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        proxy_server.shutdown()
        target_server.shutdown()
        shutil.rmtree(temp_dir, ignore_errors=True)

    print("")
    print("SUMMARY: TOTAL %d checks, %d passed, %d failed"
          % (PASS + FAIL, PASS, FAIL))
    print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN",
                                    1 if FAIL else 0))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
