"""SOCKS4 / SOCKS4a / SOCKS5 / SOCKS5h 出站支持（纯标准库）。

urllib 的 ``ProxyHandler`` 只认识 HTTP/HTTPS 代理，遇到 ``socks5://`` 会直接
报 unknown url type。本模块用标准库 socket 实现 SOCKS 握手，并提供：

  - ``socks_create_connection(proxy, host, port, timeout)``：返回已连到目标的
    普通 socket（调用方自己做 TLS/HTTP）；
  - ``build_socks_opener(url)``：返回一个 urllib opener，HTTP/HTTPS 请求都会
    先经 SOCKS 隧道，再按目标协议收发（HTTPS 在隧道内做 TLS，SNI/证书校验
    仍按目标域名进行）。

支持的代理 URL：

  - ``socks5://host:port`` / ``socks5h://host:port``（域名交给代理解析）
  - ``socks5://user:pass@host:port``（RFC 1929 用户名/密码认证）
  - ``socks4://host:port``（域名本地解析成 IPv4；失败时退回 SOCKS4a）
  - ``socks4a://host:port``（域名交给代理解析）
  - ``socks4://user@host:port``（userid；SOCKS4 没有密码字段）
  - 省略端口时默认 1080。

实现取舍：
  - 域名优先交给代理解析（socks5/socks5h、socks4a），避免本地 DNS 泄漏；
    ``socks4`` 按标准先本地解析 IPv4，解析失败再退回 4a。
  - 仅支持 CONNECT 命令（HTTP/HTTPS 出站都只需要它）。
"""

import http.client
import ipaddress
import socket
import ssl
import struct
import urllib.parse
import urllib.request

DEFAULT_PORTS = {
    "socks4": 1080,
    "socks4a": 1080,
    "socks5": 1080,
    "socks5h": 1080,
}
SCHEMES = tuple(DEFAULT_PORTS)

_SOCKS5_ERRORS = {
    1: "general SOCKS server failure",
    2: "connection not allowed by ruleset",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}
_SOCKS4_ERRORS = {
    91: "request rejected or failed",
    92: "request rejected: proxy cannot connect to identd",
    93: "request rejected: userid mismatch",
}


def parse_proxy_url(url):
    """解析 socks URL；返回 dict（scheme/host/port/username/password）。"""
    raw = str(url or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise ValueError("SOCKS 代理地址无法解析: %s" % exc)
    scheme = (parsed.scheme or "").lower()
    if scheme not in SCHEMES:
        raise ValueError("not a SOCKS proxy: %s" % scheme)
    host = parsed.hostname
    if not host:
        raise ValueError("SOCKS 代理缺少主机名")
    try:
        port = parsed.port or DEFAULT_PORTS[scheme]
    except ValueError:
        raise ValueError("SOCKS 代理端口不合法")
    return {
        "scheme": scheme,
        "host": host,
        "port": port,
        "username": urllib.parse.unquote(parsed.username or ""),
        "password": urllib.parse.unquote(parsed.password or ""),
        "raw": raw,
    }


def proxy_label(proxy):
    cfg = proxy if isinstance(proxy, dict) else parse_proxy_url(proxy)
    return "%s://%s:%d" % (cfg["scheme"], cfg["host"], cfg["port"])


def _recv_exact(sock, count):
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("SOCKS proxy closed the connection during handshake")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _ip_literal(host):
    try:
        return ipaddress.ip_address(str(host).strip("[]"))
    except ValueError:
        return None


def _socks5_address(host, port, scheme):
    """返回 (atyp, addr_bytes)；域名交给代理解析。"""
    literal = _ip_literal(host)
    if literal is not None:
        if literal.version == 4:
            return 1, literal.packed
        return 4, literal.packed
    name = str(host).encode("idna")
    if not name or len(name) > 255:
        raise OSError("SOCKS5 目标域名不合法: %r" % host)
    return 3, bytes([len(name)]) + name


def _socks5_connect(cfg, host, port, timeout):
    sock = socket.create_connection((cfg["host"], cfg["port"]), timeout)
    try:
        if timeout is not None:
            sock.settimeout(timeout)
        methods = [0x00]
        has_credentials = bool(cfg["username"] or cfg["password"])
        if has_credentials:
            methods.append(0x02)
        sock.sendall(bytes([0x05, len(methods)]) + bytes(methods))
        greeting = _recv_exact(sock, 2)
        if greeting[0] != 0x05:
            raise OSError("not a SOCKS5 proxy (version=%d)" % greeting[0])
        method = greeting[1]
        if method == 0xFF:
            raise OSError("SOCKS5 proxy rejected all offered auth methods")
        if method == 0x02:
            username = cfg["username"].encode("utf-8")
            password = cfg["password"].encode("utf-8")
            if len(username) > 255 or len(password) > 255:
                raise OSError("SOCKS5 用户名/密码超过 255 字节")
            sock.sendall(b"\x01" + bytes([len(username)]) + username
                         + bytes([len(password)]) + password)
            auth = _recv_exact(sock, 2)
            if auth[0] != 0x01 or auth[1] != 0x00:
                raise OSError("SOCKS5 username/password authentication failed")
        elif method != 0x00:
            raise OSError("SOCKS5 proxy selected unsupported auth method 0x%02x"
                          % method)

        atyp, address = _socks5_address(host, port, cfg["scheme"])
        sock.sendall(b"\x05\x01\x00" + bytes([atyp]) + address
                     + struct.pack("!H", int(port)))
        reply = _recv_exact(sock, 4)
        if reply[0] != 0x05:
            raise OSError("invalid SOCKS5 reply version %d" % reply[0])
        if reply[1] != 0x00:
            reason = _SOCKS5_ERRORS.get(reply[1], "unknown error")
            raise OSError("SOCKS5 CONNECT %s:%s rejected: %s (rep=%d)"
                          % (host, port, reason, reply[1]))
        # 吃掉 BND.ADDR / BND.PORT，保持流干净
        if reply[3] == 1:
            _recv_exact(sock, 4 + 2)
        elif reply[3] == 3:
            length = _recv_exact(sock, 1)[0]
            _recv_exact(sock, length + 2)
        elif reply[3] == 4:
            _recv_exact(sock, 16 + 2)
        else:
            raise OSError("invalid SOCKS5 address type %d" % reply[3])
        return sock
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise


def _socks4_connect(cfg, host, port, timeout):
    scheme = cfg["scheme"]
    ipv4 = None
    literal = _ip_literal(host)
    domain = None
    if literal is not None and literal.version == 4:
        ipv4 = literal.packed
    elif literal is not None and literal.version == 6:
        raise OSError("SOCKS4/SOCKS4a 不支持 IPv6 目标；请使用 socks5/socks5h")
    elif scheme == "socks4":
        # 标准 SOCKS4：先在本地解析 IPv4；失败再走 4a（域名交给代理）。
        try:
            infos = socket.getaddrinfo(host, port, socket.AF_INET,
                                       socket.SOCK_STREAM)
            if infos:
                ipv4 = socket.inet_aton(infos[0][4][0])
        except OSError:
            ipv4 = None
        if ipv4 is None:
            domain = str(host).encode("idna")
    else:
        domain = str(host).encode("idna")

    userid = cfg["username"].encode("utf-8")
    if b"\x00" in userid:
        raise OSError("SOCKS4 userid 不能包含 NUL")
    sock = socket.create_connection((cfg["host"], cfg["port"]), timeout)
    try:
        if timeout is not None:
            sock.settimeout(timeout)
        if ipv4 is not None:
            request = (b"\x04\x01" + struct.pack("!H", int(port)) + ipv4
                       + userid + b"\x00")
        else:
            if not domain:
                raise OSError("SOCKS4 目标地址为空")
            request = (b"\x04\x01" + struct.pack("!H", int(port))
                       + b"\x00\x00\x00\x01" + userid + b"\x00"
                       + domain + b"\x00")
        sock.sendall(request)
        reply = _recv_exact(sock, 8)
        if reply[0] not in (0x00, 0x04):
            raise OSError("invalid SOCKS4 reply version %d" % reply[0])
        if reply[1] != 0x5A:
            reason = _SOCKS4_ERRORS.get(reply[1], "unknown error")
            raise OSError("SOCKS4 CONNECT %s:%s rejected: %s (code=%d)"
                          % (host, port, reason, reply[1]))
        return sock
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise


def socks_create_connection(proxy, host, port, timeout=None):
    """与目标建立 SOCKS 隧道，返回已连接的 socket。"""
    cfg = proxy if isinstance(proxy, dict) else parse_proxy_url(proxy)
    if cfg["scheme"] in ("socks5", "socks5h"):
        return _socks5_connect(cfg, host, port, timeout)
    return _socks4_connect(cfg, host, port, timeout)


def _socks_http_connection_class(proxy_url):
    cfg = parse_proxy_url(proxy_url)

    class SocksHTTPConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socks_create_connection(
                cfg, self.host, self.port, self.timeout)

    return SocksHTTPConnection


def _socks_https_connection_class(proxy_url):
    cfg = parse_proxy_url(proxy_url)

    class SocksHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            sock = socks_create_connection(
                cfg, self.host, self.port, self.timeout)
            if self._tunnel_host:          # SOCKS 场景通常为 None，保留兼容
                self.sock = sock
                self._tunnel()
                sock = self.sock
            context = self._context or ssl.create_default_context()
            self.sock = context.wrap_socket(sock,
                                            server_hostname=self.host)

    return SocksHTTPSConnection


class SocksProxyHandler(urllib.request.HTTPHandler,
                        urllib.request.HTTPSHandler):
    """让 urllib 的 HTTP/HTTPS 请求都经过 SOCKS 隧道。"""

    def __init__(self, proxy_url):
        urllib.request.HTTPSHandler.__init__(self)
        self.proxy_url = proxy_url
        self._http_class = _socks_http_connection_class(proxy_url)
        self._https_class = _socks_https_connection_class(proxy_url)

    def http_open(self, req):
        return self.do_open(self._http_class, req)

    def https_open(self, req):
        return self.do_open(self._https_class, req, context=self._context)


def build_socks_opener(proxy_url):
    """构造只走该 SOCKS 代理的 opener（显式 ProxyHandler({}) 隔离 env 代理）。"""
    handler = SocksProxyHandler(proxy_url)
    return urllib.request.build_opener(handler,
                                       urllib.request.ProxyHandler({}))
