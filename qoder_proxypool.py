"""出站代理池（Outbound proxy pool）。

参考 github.com/qingdeng888/opencode2api 的代理池模型（代理列表 + 轮换 +
健康检查 + 冷却/失败切换），按本仓「零第三方依赖」的约束用标准库重写：

- 配置落在 ``<accounts>/proxy_pool.json``（面板「设置 → 出站代理池」可改），
  也可手工编辑；文件权限 0600。
- 每个上游请求按策略从**健康**代理里选一个出口；传输层失败时自动换下一个
  代理重试（同一请求最多 ``max_attempts`` 次），连续失败达到阈值后进入
  冷却，冷却期内不再被选中。
- 后台健康检查线程按间隔并发探测每个代理；探测失败立即冷却，探测成功
  立即恢复。面板可手动测试单个/全部代理。
- 未启用或一个代理都没配时，行为与改动前完全一致：沿用
  ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY`` 环境变量，没设就直连；
  环境变量是 SOCKS 时会自动切到内置 SOCKS 隧道（见 qoder_socks.py）。
- 代理协议：``http://`` / ``https://`` / ``socks4://`` / ``socks4a://`` /
  ``socks5://`` / ``socks5h://``，均支持 ``user:pass@``（SOCKS4 只使用 userid）。
  SOCKS 由 qoder_socks.py 用标准库 socket 实现，仍是零第三方依赖。

配置示例::

    {
      "enabled": true,
      "mode": "rotate",              # rotate | sticky | random | single
      "max_attempts": 3,
      "failure_threshold": 3,
      "cooldown_sec": 60,
      "direct_fallback": true,       # 全部代理不可用时是否允许直连兜底
      "health_check": {
        "enabled": true,
        "interval_sec": 300,
        "timeout_sec": 8,
        "target": "https://api1.qoder.sh/"
      },
      "proxies": [
        {"id": "p1a2b3c4d5e", "label": "HK-01",
         "url": "http://user:pass@1.2.3.4:8080", "enabled": true},
        {"id": "p6f7a8b9c0d", "label": "JP-02",
         "url": "socks5://user:pass@5.6.7.8:1080", "enabled": true},
        {"id": "p0e1d2c3b4a", "label": "US-03",
         "url": "socks4a://9.10.11.12:1080", "enabled": true}
      ]
    }
"""

import copy
import hashlib
import json
import os
import random
import re
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import qoder_socks

CONFIG_NAME = "proxy_pool.json"
MAX_PROXIES = 200
DEFAULT_HEALTH_TARGET = "https://api1.qoder.sh/"
MODES = ("rotate", "sticky", "random", "single")
# 账号级出口模式（对齐 opencode2api 的 upstream key proxy_mode）：
#   ""       跟随代理池全局策略（历史行为）
#   auto     按账号 UID 稳定选一个代理；失败时本请求换下一个
#   bind     固定 proxy_id；代理被删除/停用才回落到默认
#   rotate   每次请求/每次重试都换一个健康代理
#   direct   该账号强制直连（绕过代理池）
ACCOUNT_PROXY_MODES = ("", "auto", "bind", "rotate", "direct")

DEFAULTS = {
    "enabled": False,
    "mode": "rotate",
    "max_attempts": 3,
    "failure_threshold": 3,
    "cooldown_sec": 60,
    "direct_fallback": True,
    "health_check": {
        "enabled": True,
        "interval_sec": 300,
        "timeout_sec": 8,
        "target": DEFAULT_HEALTH_TARGET,
    },
    "proxies": [],
}

_LOCK = threading.RLock()
_ACCOUNTS_DIR = (os.environ.get("ACCOUNTS_DIR") or "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "accounts")
_CFG = None
_CFG_MTIME = None
_CFG_CHECKED = 0.0
_STATS = {}
_CURSOR = 0
_THREAD = None
_WAKE = threading.Event()
_STOP = threading.Event()

_USERINFO_RE = re.compile(r"//[^/@\s]+@")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
def configure(accounts_dir, start_health=False):
    """切换数据目录（网关启动时用 --accounts-dir / 默认目录调用一次）。"""
    global _ACCOUNTS_DIR, _CFG, _CFG_MTIME, _CFG_CHECKED
    path = str(accounts_dir or "").strip()
    with _LOCK:
        if path:
            _ACCOUNTS_DIR = os.path.abspath(path)
        _CFG = None
        _CFG_MTIME = None
        _CFG_CHECKED = 0.0
        _STATS.clear()
    if start_health:
        start_health_thread()


def config_path():
    return os.path.join(_ACCOUNTS_DIR, CONFIG_NAME)


def _coerce_int(value, default, minimum, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = int(default)
    return max(minimum, min(maximum, number))


def _coerce_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


PROXY_SCHEMES = ("http", "https") + qoder_socks.SCHEMES


def normalize_proxy_url(raw):
    """校验并归一化代理地址；非法时抛 ValueError（消息直接给面板看）。"""
    value = str(raw or "").strip()
    if not value:
        raise ValueError("代理地址不能为空")
    if "://" not in value:
        value = "http://" + value
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise ValueError("代理地址无法解析: %s" % exc)
    scheme = (parsed.scheme or "").lower()
    if scheme not in PROXY_SCHEMES:
        raise ValueError("只支持 http/https/socks4/socks4a/socks5/socks5h 代理")
    if not parsed.hostname:
        raise ValueError("代理地址缺少主机名")
    try:
        parsed.port  # noqa: B018 —— 触发端口合法性校验
    except ValueError:
        raise ValueError("代理端口不合法")
    if parsed.query or parsed.fragment:
        raise ValueError("代理地址不要带 query / fragment")
    return urllib.parse.urlunsplit((scheme, parsed.netloc, "", "", ""))


def build_proxy_opener(url):
    """按代理协议构造 opener：HTTP(S) 用 ProxyHandler，SOCKS 用自建隧道。"""
    scheme = (urllib.parse.urlsplit(str(url or "")).scheme or "").lower()
    if scheme in qoder_socks.SCHEMES:
        return qoder_socks.build_socks_opener(url)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url}))


def env_socks_url():
    """环境变量里的 SOCKS 代理（HTTPS_PROXY 优先，其次 ALL_PROXY/HTTP_PROXY）。

    代理池未启用时，``HTTPS_PROXY=socks5://...`` 也能直接用——否则 urllib 的
    ProxyHandler 会把它当 HTTP 代理并报 unknown url type。
    """
    keys = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
            "HTTP_PROXY", "http_proxy")
    for key in keys:
        value = (os.environ.get(key) or "").strip()
        if "://" not in value:
            continue
        if value.split("://", 1)[0].lower() in qoder_socks.SCHEMES:
            return value
    return ""


def mask_url(url):
    """面板/日志展示用：隐藏用户名与密码，只留 scheme://***@host:port。"""
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = "[%s]" % host
        port = (":%d" % parsed.port) if parsed.port else ""
        credentials = "***@" if (parsed.username is not None
                                 or parsed.password is not None) else ""
        return "%s://%s%s%s" % (parsed.scheme or "http", credentials, host, port)
    except Exception:
        return "***"


def _scrub(text):
    return _USERINFO_RE.sub("//***@", str(text or ""))[:200]


def _proxy_id(url):
    return "p" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def _normalize_entry(item, strict=False, index=0):
    if isinstance(item, str):
        raw_url, label, enabled = item, "", True
        entry_id = ""
    elif isinstance(item, dict):
        raw_url = item.get("url") or item.get("proxy") or ""
        label = str(item.get("label") or item.get("name") or "").strip()
        enabled = _coerce_bool(item.get("enabled", True), True)
        entry_id = str(item.get("id") or "").strip()
    else:
        if strict:
            raise ValueError("代理条目必须是字符串或对象")
        return None
    try:
        url = normalize_proxy_url(raw_url)
    except ValueError:
        if strict:
            raise
        return None
    if not _ID_RE.match(entry_id):
        entry_id = _proxy_id(url)
    return {
        "id": entry_id,
        "label": label[:60],
        "url": url,
        "enabled": enabled,
    }


def _normalize_health(raw):
    source = raw if isinstance(raw, dict) else {}
    target = str(source.get("target") or DEFAULT_HEALTH_TARGET).strip()
    try:
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError
    except Exception:
        target = DEFAULT_HEALTH_TARGET
    return {
        "enabled": _coerce_bool(source.get("enabled", True), True),
        "interval_sec": _coerce_int(source.get("interval_sec"), 300, 30, 86400),
        "timeout_sec": _coerce_int(source.get("timeout_sec"), 8, 2, 120),
        "target": target,
    }


def _normalize_config(raw, strict=False):
    source = raw if isinstance(raw, dict) else {}
    mode = str(source.get("mode") or "rotate").strip().lower()
    if mode not in MODES:
        if strict:
            raise ValueError("mode 必须是 rotate / sticky / random / single")
        mode = "rotate"
    cfg = {
        "enabled": _coerce_bool(source.get("enabled", False), False),
        "mode": mode,
        "max_attempts": _coerce_int(source.get("max_attempts"), 3, 1, 10),
        "failure_threshold": _coerce_int(
            source.get("failure_threshold"), 3, 1, 20),
        "cooldown_sec": _coerce_int(source.get("cooldown_sec"), 60, 5, 86400),
        "direct_fallback": _coerce_bool(
            source.get("direct_fallback", True), True),
        "health_check": _normalize_health(source.get("health_check")),
        "proxies": [],
    }
    proxies = source.get("proxies")
    if not isinstance(proxies, list):
        proxies = []
    seen_ids = set()
    seen_urls = set()
    for index, item in enumerate(proxies[:MAX_PROXIES]):
        entry = _normalize_entry(item, strict=strict, index=index)
        if not entry:
            continue
        if entry["id"] in seen_ids:
            if strict:
                raise ValueError("代理 id 重复: %s" % entry["id"])
            continue
        if entry["url"] in seen_urls:
            continue
        seen_ids.add(entry["id"])
        seen_urls.add(entry["url"])
        cfg["proxies"].append(entry)
    return cfg


def _read_config_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # 文件被手工改坏时不要让网关起不来：退回默认（关闭）配置。
        return {}


def get_config(force=False):
    """读取配置（带 mtime 缓存：最多每 2 秒 stat 一次，热路径开销可忽略）。"""
    global _CFG, _CFG_MTIME, _CFG_CHECKED
    with _LOCK:
        now = time.time()
        path = config_path()
        if force or _CFG is None or now - _CFG_CHECKED > 2.0:
            _CFG_CHECKED = now
            try:
                mtime = os.stat(path).st_mtime_ns
            except OSError:
                mtime = None
            if _CFG is None or mtime != _CFG_MTIME:
                _CFG = _normalize_config(_read_config_file(path))
                _CFG_MTIME = mtime
        return _CFG


def _atomic_write(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=CONFIG_NAME + ".", suffix=".tmp",
                               dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def save_config(raw):
    """校验并落盘；返回规范化后的配置。非法输入抛 ValueError。"""
    global _CFG, _CFG_MTIME
    cfg = _normalize_config(raw, strict=True)
    path = config_path()
    _atomic_write(path, cfg)
    with _LOCK:
        _CFG = cfg
        try:
            _CFG_MTIME = os.stat(path).st_mtime_ns
        except OSError:
            _CFG_MTIME = None
        _CFG_CHECKED = time.time()
        live_ids = set(entry["id"] for entry in cfg["proxies"])
        for entry_id in list(_STATS):
            if entry_id not in live_ids:
                _STATS.pop(entry_id, None)
    if cfg["enabled"]:
        start_health_thread()
    _WAKE.set()
    return copy.deepcopy(cfg)


def reload():
    return copy.deepcopy(get_config(force=True))


def update_config(patch):
    """局部更新开关/策略/健康检查等（代理列表请用 add/delete/toggle）。"""
    if not isinstance(patch, dict):
        raise ValueError("config 必须是对象")
    cfg = copy.deepcopy(get_config(force=True))
    for key in ("enabled", "mode", "max_attempts", "failure_threshold",
                "cooldown_sec", "direct_fallback"):
        if key in patch:
            cfg[key] = patch[key]
    if isinstance(patch.get("health_check"), dict):
        merged = dict(cfg.get("health_check") or {})
        merged.update(patch["health_check"])
        cfg["health_check"] = merged
    return save_config(cfg)


# ---------------------------------------------------------------------------
# 代理列表维护
# ---------------------------------------------------------------------------
def _split_label(raw):
    """支持 ``备注|http://...``；没有分隔符时整行都是地址。"""
    text = str(raw or "").strip()
    if "|" in text:
        left, right = text.split("|", 1)
        if left.strip() and right.strip():
            return left.strip(), right.strip()
    return "", text


def add_proxies(values, label=""):
    """批量添加。返回 added / skipped / rejected 与最新配置。"""
    cfg = copy.deepcopy(get_config(force=True))
    existing_urls = set(entry["url"] for entry in cfg["proxies"])
    existing_ids = set(entry["id"] for entry in cfg["proxies"])
    added, skipped, rejected = [], [], []
    default_label = str(label or "").strip()[:60]
    for raw in values or []:
        if isinstance(raw, dict):
            raw_label, raw_url = _split_label(raw.get("url") or "")
            item_label = str(raw.get("label") or raw_label or default_label)
            try:
                url = normalize_proxy_url(raw_url)
            except ValueError as exc:
                rejected.append({"input": _scrub(raw_url), "reason": str(exc)})
                continue
        else:
            text = str(raw or "").strip()
            if not text or text.startswith("#"):
                continue
            raw_label, raw_url = _split_label(text)
            item_label = raw_label or default_label
            try:
                url = normalize_proxy_url(raw_url)
            except ValueError as exc:
                rejected.append({"input": _scrub(text), "reason": str(exc)})
                continue
        if url in existing_urls:
            skipped.append(mask_url(url))
            continue
        if len(cfg["proxies"]) >= MAX_PROXIES:
            rejected.append({"input": mask_url(url), "reason": "代理数量已达上限 %d" % MAX_PROXIES})
            continue
        entry_id = _proxy_id(url)
        suffix = 0
        while entry_id in existing_ids:
            suffix += 1
            entry_id = "%s-%d" % (_proxy_id(url), suffix)
        entry = {"id": entry_id, "label": item_label[:60], "url": url,
                 "enabled": True}
        cfg["proxies"].append(entry)
        existing_urls.add(url)
        existing_ids.add(entry_id)
        added.append(mask_url(url))
    saved = save_config(cfg)
    return {"added": len(added), "skipped": skipped, "rejected": rejected,
            "proxies": saved["proxies"]}


def delete_proxies(ids):
    wanted = set(str(item or "").strip() for item in (ids or []))
    wanted.discard("")
    if not wanted:
        raise ValueError("ids 不能为空")
    cfg = copy.deepcopy(get_config(force=True))
    kept, removed_ids = [], set()
    for entry in cfg["proxies"]:
        if entry["id"] in wanted:
            removed_ids.add(entry["id"])
        else:
            kept.append(entry)
    cfg["proxies"] = kept
    save_config(cfg)
    with _LOCK:
        for entry_id in removed_ids:
            _STATS.pop(entry_id, None)
    return {"removed": len(removed_ids), "skipped": sorted(wanted - removed_ids)}


def set_proxies_enabled(ids, enabled):
    wanted = set(str(item or "").strip() for item in (ids or []))
    wanted.discard("")
    if not wanted:
        raise ValueError("ids 不能为空")
    cfg = copy.deepcopy(get_config(force=True))
    changed = []
    for entry in cfg["proxies"]:
        if entry["id"] in wanted:
            entry["enabled"] = bool(enabled)
            changed.append(entry["id"])
    save_config(cfg)
    return {"changed": changed, "enabled": bool(enabled)}


def reset_stats(ids=None):
    wanted = set(str(item or "").strip() for item in (ids or []))
    wanted.discard("")
    with _LOCK:
        if wanted:
            for entry_id in wanted:
                _STATS.pop(entry_id, None)
        else:
            _STATS.clear()
    return {"reset": len(wanted) if wanted else "all"}


# ---------------------------------------------------------------------------
# 运行时选择与健康统计
# ---------------------------------------------------------------------------
def _new_stats():
    return {
        "failures": 0,
        "failures_total": 0,
        "successes": 0,
        "checks": 0,
        "cooldown_until": 0.0,
        "last_ok": 0.0,
        "last_fail_at": 0.0,
        "last_error": "",
        "latency_ms": None,
        "last_check": 0.0,
        "checking": False,
    }


def _stats_for(entry):
    entry_id = (entry or {}).get("id") or ""
    stats = _STATS.get(entry_id)
    if stats is None:
        stats = _new_stats()
        _STATS[entry_id] = stats
    return stats


def is_transport_error(exc):
    """传输层失败才算「代理坏了」；HTTPError 只把 407（代理认证失败）算进去。"""
    if isinstance(exc, urllib.error.HTTPError):
        return int(getattr(exc, "code", 0) or 0) == 407
    if isinstance(exc, urllib.error.URLError):
        return True
    return isinstance(exc, (socket.timeout, TimeoutError, ConnectionError,
                            ssl.SSLError, OSError))


def affinity_from_headers(headers):
    """sticky 模式的亲和键：优先 Authorization，其次 Cookie（均为账号级）。"""
    for name, value in (headers or {}).items():
        if str(name).lower() in ("authorization", "cookie") and value:
            return hashlib.sha1(str(value).encode("utf-8")).hexdigest()
    return None


def _choose_index(mode, affinity, count):
    global _CURSOR
    if count <= 1:
        return 0
    if mode == "single":
        return 0
    if mode == "random":
        return random.randrange(count)
    if mode == "sticky" and affinity:
        digest = hashlib.sha1(str(affinity).encode("utf-8")).hexdigest()
        return int(digest[:16], 16) % count
    with _LOCK:
        index = _CURSOR % count
        _CURSOR += 1
    return index


def _pick(cfg, affinity, exclude, mode=None):
    """返回 (entry, kind)。kind: proxy / direct / exhausted。

    mode 非空时覆盖代理池全局策略（账号级 auto/rotate 用）。
    """
    now = time.time()
    candidates = [
        entry for entry in cfg["proxies"]
        if entry.get("enabled", True)
        and entry["id"] not in exclude
        and _stats_for(entry)["cooldown_until"] <= now
    ]
    if candidates:
        index = _choose_index(mode or cfg.get("mode") or "rotate", affinity,
                              len(candidates))
        return candidates[index], "proxy"
    if exclude:
        # 本请求已经试过的代理不再重复；没有直连兜底就明确失败。
        return (None, "direct") if cfg.get("direct_fallback", True) \
            else (None, "exhausted")
    if cfg.get("direct_fallback", True):
        return None, "direct"
    leftovers = [entry for entry in cfg["proxies"]
                 if entry.get("enabled", True)]
    if leftovers:
        return min(leftovers,
                   key=lambda entry: _stats_for(entry)["cooldown_until"]), "proxy"
    return None, "exhausted"


def _find_proxy(cfg, proxy_id):
    wanted = str(proxy_id or "").strip()
    if not wanted:
        return None
    for entry in cfg.get("proxies") or []:
        if entry.get("id") == wanted:
            return entry
    return None


def find_proxy(proxy_id):
    """按 id 查代理（供账号绑定校验）；不存在返回 None。"""
    return _find_proxy(get_config(force=True), proxy_id)


def _direct_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def acquire(affinity=None, exclude=None, binding=None):
    """给一次出站请求选代理。

    ``binding`` 是账号级绑定：``{"mode": "", "proxy_id": "", "key": uid}``
    （见 ACCOUNT_PROXY_MODES）。没有绑定或 mode 为空时沿用代理池全局策略。

    返回 ``(opener, entry, kind)``：

    - ``kind == "env"``：代理池未启用，调用方沿用原来的 env 代理/urllib 行为；
    - ``kind == "proxy"``：``opener`` 已绑定该代理，``entry`` 用于回报成败；
    - ``kind == "direct"``：池已启用但当前只能直连（兜底 / 账号 direct）；
    - ``kind == "exhausted"``：没有可用代理且禁止直连。
    """
    cfg = get_config()
    if not cfg.get("enabled"):
        return None, None, "env"
    tried = set(exclude or ())
    if affinity is None and binding:
        affinity = binding.get("key")
    mode = str((binding or {}).get("mode") or "").strip().lower()
    if mode not in ACCOUNT_PROXY_MODES:
        mode = ""

    if mode == "direct":
        return _direct_opener(), None, "direct"

    if mode == "bind":
        entry = _find_proxy(cfg, (binding or {}).get("proxy_id"))
        if entry is not None and entry.get("enabled", True):
            # 绑定优先：即使该代理正在冷却也照用（与参考项目 bind 语义一致）；
            # 本请求已经试过它则明确失败，不再偷偷换别的出口。
            if entry["id"] in tried:
                return None, None, "exhausted"
            return build_proxy_opener(entry["url"]), entry, "proxy"
        # 绑定的代理被删除/停用 -> 回落全局默认策略，保证账号仍可用
        entry, kind = _pick(cfg, affinity, tried)
    elif mode == "auto":
        entry, kind = _pick(cfg, affinity, tried, mode="sticky")
    elif mode == "rotate":
        entry, kind = _pick(cfg, affinity, tried, mode="rotate")
    else:
        entry, kind = _pick(cfg, affinity, tried)

    if kind == "proxy" and entry:
        return build_proxy_opener(entry["url"]), entry, "proxy"
    if kind == "direct":
        return _direct_opener(), None, "direct"
    return None, None, "exhausted"


def report_success(entry):
    if not entry:
        return
    with _LOCK:
        stats = _stats_for(entry)
        stats["failures"] = 0
        stats["cooldown_until"] = 0.0
        stats["last_ok"] = time.time()
        stats["successes"] += 1
        stats["last_error"] = ""


def report_failure(entry, exc):
    if not entry or not is_transport_error(exc):
        return
    cfg = get_config()
    now = time.time()
    with _LOCK:
        stats = _stats_for(entry)
        stats["failures"] += 1
        stats["failures_total"] += 1
        stats["last_fail_at"] = now
        stats["last_error"] = _scrub(exc)
        threshold = max(1, int(cfg.get("failure_threshold") or 3))
        if stats["failures"] >= threshold:
            exponent = min(stats["failures"] - threshold, 3)
            stats["cooldown_until"] = now + (
                max(5, int(cfg.get("cooldown_sec") or 60)) * (2 ** exponent))


def report_transport_failure(entry, exc):
    """兼容别名：调用方不确定异常类型时也可以直接调用。"""
    report_failure(entry, exc)


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
def _probe_entry(entry, target, timeout):
    started = time.time()
    opener = build_proxy_opener(entry["url"])
    request = urllib.request.Request(target, headers={
        "User-Agent": "qoder-proxy-pool-health",
        "Accept": "*/*",
        "Connection": "close",
    })
    try:
        with opener.open(request, timeout=timeout) as response:
            try:
                response.read(64)
            except Exception:
                pass
            status = getattr(response, "status", 200)
        return True, status, int((time.time() - started) * 1000), ""
    except urllib.error.HTTPError as exc:
        if int(getattr(exc, "code", 0) or 0) == 407:
            return False, exc.code, int((time.time() - started) * 1000), \
                "proxy authentication failed (407)"
        # 4xx/5xx 也证明代理链路通；只有连不上/超时/认证失败算坏。
        return True, exc.code, int((time.time() - started) * 1000), ""
    except Exception as exc:
        return False, None, int((time.time() - started) * 1000), _scrub(exc)


def _record_probe(entry, ok, status, latency_ms, error):
    cfg = get_config()
    now = time.time()
    with _LOCK:
        stats = _stats_for(entry)
        stats["checks"] += 1
        stats["last_check"] = now
        stats["latency_ms"] = latency_ms
        stats["last_status"] = status
        if ok:
            stats["failures"] = 0
            stats["cooldown_until"] = 0.0
            stats["last_ok"] = now
            stats["successes"] += 1
            stats["last_error"] = ""
        else:
            stats["failures"] += 1
            stats["failures_total"] += 1
            stats["last_fail_at"] = now
            stats["last_error"] = _scrub(error)
            # 健康检查是权威判定：探测失败立即冷却，下一次探测成功即恢复。
            stats["cooldown_until"] = now + max(
                5, int(cfg.get("cooldown_sec") or 60))


def _probe_many(entries, target, timeout, workers=8):
    results = {}
    if not entries:
        return results
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(entries)))) \
            as pool:
        futures = {
            pool.submit(_probe_entry, entry, target, timeout): entry
            for entry in entries
        }
        for future in as_completed(futures):
            entry = futures[future]
            try:
                ok, status, latency, error = future.result()
            except Exception as exc:
                ok, status, latency, error = False, None, 0, _scrub(exc)
            _record_probe(entry, ok, status, latency, error)
            results[entry["id"]] = {
                "id": entry["id"],
                "label": entry.get("label") or "",
                "address": mask_url(entry["url"]),
                "ok": ok,
                "status": status,
                "latency_ms": latency,
                "error": error,
            }
    return results


def test_proxies(ids=None):
    """手动测试（同步返回）。ids 为空 = 全部。"""
    cfg = get_config(force=True)
    wanted = set(str(item or "").strip() for item in (ids or []))
    wanted.discard("")
    entries = [entry for entry in cfg["proxies"]
               if not wanted or entry["id"] in wanted]
    health = cfg.get("health_check") or {}
    results = _probe_many(entries, health.get("target") or DEFAULT_HEALTH_TARGET,
                          int(health.get("timeout_sec") or 8))
    ordered = [results[entry["id"]] for entry in entries
               if entry["id"] in results]
    return ordered


def _run_scheduled_checks():
    cfg = get_config(force=True)
    health = cfg.get("health_check") or {}
    if not cfg.get("enabled") or not health.get("enabled"):
        return
    entries = [entry for entry in cfg["proxies"]
               if entry.get("enabled", True)]
    _probe_many(entries, health.get("target") or DEFAULT_HEALTH_TARGET,
                int(health.get("timeout_sec") or 8))


def _health_loop():
    while not _STOP.is_set():
        try:
            cfg = get_config(force=True)
            health = cfg.get("health_check") or {}
            if cfg.get("enabled") and health.get("enabled"):
                _run_scheduled_checks()
                delay = int(health.get("interval_sec") or 300)
            else:
                delay = 300
        except Exception:
            delay = 300
        _WAKE.wait(timeout=max(15, delay))
        _WAKE.clear()


def start_health_thread():
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            _WAKE.set()
            return
        _STOP.clear()
        _THREAD = threading.Thread(target=_health_loop,
                                   name="qoder-proxy-pool-health",
                                   daemon=True)
        _THREAD.start()


def stop_health_thread():
    _STOP.set()
    _WAKE.set()


# ---------------------------------------------------------------------------
# 面板视图
# ---------------------------------------------------------------------------
def status_view():
    cfg = get_config()
    now = time.time()
    rows = []
    healthy = cooling = disabled = unknown = 0
    for entry in cfg["proxies"]:
        with _LOCK:
            stats = dict(_stats_for(entry))
        cooling_left = max(0, int(stats.get("cooldown_until", 0) - now))
        if not entry.get("enabled", True):
            state = "disabled"
            disabled += 1
        elif cooling_left > 0:
            state = "cooling"
            cooling += 1
        elif stats.get("checks") == 0 and stats.get("successes") == 0:
            state = "unknown"
            unknown += 1
        else:
            state = "healthy"
            healthy += 1
        rows.append({
            "id": entry["id"],
            "label": entry.get("label") or "",
            "address": mask_url(entry["url"]),
            "enabled": bool(entry.get("enabled", True)),
            "state": state,
            "cooling_left": cooling_left,
            "failures": stats.get("failures", 0),
            "failures_total": stats.get("failures_total", 0),
            "successes": stats.get("successes", 0),
            "checks": stats.get("checks", 0),
            "latency_ms": stats.get("latency_ms"),
            "last_status": stats.get("last_status"),
            "last_ok": int(stats.get("last_ok") or 0),
            "last_error": stats.get("last_error") or "",
        })
    return {
        "enabled": bool(cfg.get("enabled")),
        "mode": cfg.get("mode") or "rotate",
        "max_attempts": cfg.get("max_attempts", 3),
        "failure_threshold": cfg.get("failure_threshold", 3),
        "cooldown_sec": cfg.get("cooldown_sec", 60),
        "direct_fallback": bool(cfg.get("direct_fallback", True)),
        "health_check": dict(cfg.get("health_check") or {}),
        "proxies": rows,
        "summary": {
            "total": len(rows),
            "healthy": healthy,
            "cooling": cooling,
            "disabled": disabled,
            "unknown": unknown,
        },
        "config_file": config_path(),
    }


def max_attempts():
    cfg = get_config()
    if not cfg.get("enabled"):
        return 1
    return max(1, int(cfg.get("max_attempts") or 1))
