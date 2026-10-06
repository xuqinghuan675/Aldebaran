"""网络请求默认设置 + 东财 push2 多源健壮请求。

设计要点（v3）：
1. 给 requests 默认 Session 注入浏览器 UA（避免裸 python-requests UA 被东财断连）。
2. push2_get：东财 push2 健壮请求函数
   - 子域名白名单（社区/akshare/efinance 验证可用的 12 个数字子域）
   - 每次调用按健康度排序、尝试 K=3 个子域，单子域失败累计 3 次进黑名单 5 分钟
   - 全局熔断：连续失败累计 -> 30s / 60s / 120s / 300s 自适应冷却
   - 主 push2 全失败后尝试 push2delay 延时行情域作为最后兜底
   - 区分限流（403/429 直接拉黑）与网络错误（仅累计）
   - 成功立即清空全局熔断 + 减少该子域失败计数 + 记忆 trust_env 模式

注意：必须在任何 `import requests` / `import akshare` 之前调用 setup_requests_defaults。
"""
from __future__ import annotations
import logging
import os
import random
import time
from collections import defaultdict


logger = logging.getLogger(__name__)


_BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)
_PUSH2_HEADERS = {
    'User-Agent': _BROWSER_UA,
    'Referer': 'https://quote.eastmoney.com/',
    'Accept': 'application/json,text/plain,*/*',
}

# 社区验证可用的 push2 数字子域名（避免从 1-99 随机命中失效的子域）
_PUSH2_SUBDOMAINS = ['1', '7', '17', '21', '22', '38', '48', '50', '64', '76', '82', '99']

# 延时行情子域（push2delay.eastmoney.com）：~15 分钟延迟但限流宽松，最后兜底
# 接口路径与 push2 完全一致，只是主机名不同
_PUSH2DELAY_HOSTS = [
    '82.push2delay.eastmoney.com',
    '1.push2delay.eastmoney.com',
    'push2delay.eastmoney.com',
]

# 子域名失败计数 & 黑名单到期时间戳
_PUSH2_SUB_FAIL = defaultdict(int)
_PUSH2_SUB_BLACKLIST: dict[str, float] = {}
_REQUESTS_DEFAULTS_ENV = 'ALDEBARAN_REQUESTS_DEFAULTS'


def _requests_defaults_enabled() -> bool:
    value = os.environ.get(_REQUESTS_DEFAULTS_ENV, '').strip().lower()
    return value not in {'0', 'false', 'no', 'off', 'disable', 'disabled'}

# 全局熔断状态
_PUSH2_FAIL_TS = 0.0
_PUSH2_FAIL_COUNT = 0  # 连续失败次数
_PUSH2_OK_MODE = None  # 上次成功的 trust_env 值（True=用系统代理 / False=直连）
_DEFAULT_REQUEST_TIMEOUT = 10


def _push2_cooldown_for(count: int) -> int:
    """连续失败次数 -> 熔断冷却时长（秒）。"""
    if count <= 1:
        return 30
    if count == 2:
        return 60
    if count == 3:
        return 120
    return 300


def _push2_pick_subs(k: int = 3) -> list[str]:
    """挑选 k 个候选子域：优先未黑名单 + 失败次数最少 + 同 tier 内随机。"""
    now = time.time()
    healthy = [s for s in _PUSH2_SUBDOMAINS if _PUSH2_SUB_BLACKLIST.get(s, 0) < now]
    if not healthy:
        # 全部进黑名单：强制重置（说明上轮全跪了，给所有子域一次复活机会）
        _PUSH2_SUB_BLACKLIST.clear()
        _PUSH2_SUB_FAIL.clear()
        healthy = list(_PUSH2_SUBDOMAINS)
    healthy.sort(key=lambda s: (_PUSH2_SUB_FAIL[s], random.random()))
    return healthy[:k]


def push2_reset_breaker():
    """手动重置 push2 熔断器（调试用）。"""
    global _PUSH2_FAIL_TS, _PUSH2_FAIL_COUNT
    _PUSH2_FAIL_TS = 0.0
    _PUSH2_FAIL_COUNT = 0
    _PUSH2_SUB_BLACKLIST.clear()
    _PUSH2_SUB_FAIL.clear()


def setup_requests_defaults():
    """给 requests 注入默认 UA。部分东财接口对裸 python-requests UA 会直接关闭连接。"""
    if not _requests_defaults_enabled():
        logger.info('requests defaults patch disabled by %s', _REQUESTS_DEFAULTS_ENV)
        return
    try:
        import requests.sessions as _rs
        if getattr(_rs.Session, '_aldebaran_defaults_patched', False):
            logger.debug('requests defaults patch already active')
            return
        _orig_session_init = _rs.Session.__init__
        _orig_session_request = _rs.Session.request

        def _patched_session_init(self, *args, **kwargs):
            _orig_session_init(self, *args, **kwargs)
            try:
                if 'python-requests' in str(self.headers.get('User-Agent', '')):
                    self.headers['User-Agent'] = _BROWSER_UA
            except Exception:
                pass

        def _patched_session_request(self, method, url, **kwargs):
            if kwargs.get('timeout') is None:
                kwargs['timeout'] = _DEFAULT_REQUEST_TIMEOUT
            return _orig_session_request(self, method, url, **kwargs)

        _rs.Session.__init__ = _patched_session_init
        _rs.Session.request = _patched_session_request
        _rs.Session._aldebaran_defaults_patched = True
        logger.info('requests defaults patch enabled: timeout=%ss, user-agent=browser', _DEFAULT_REQUEST_TIMEOUT)
    except Exception as exc:
        logger.debug('requests defaults patch setup failed: %s', exc)


def push2_get(path='', **kwargs):
    """GET 东财 push2，自适应熔断 + 多子域容错。

    成功：清空全局熔断、缩短子域失败计数、记忆 trust_env
    失败（HTTP 403/429）：判定为限流 -> 该子域立即拉黑 5 分钟
    失败（网络错误）：累计子域失败，达到 3 次进黑名单 5 分钟
    全部候选失败：全局熔断进入 30/60/120/300s 自适应冷却
    """
    import requests

    global _PUSH2_FAIL_TS, _PUSH2_OK_MODE, _PUSH2_FAIL_COUNT

    now = time.time()
    cd = _push2_cooldown_for(_PUSH2_FAIL_COUNT)
    if now - _PUSH2_FAIL_TS < cd:
        remain = int(cd - (now - _PUSH2_FAIL_TS))
        raise ConnectionError(
            f'push2 熔断中（剩余 {remain}s，连续失败 {_PUSH2_FAIL_COUNT} 次），使用缓存数据'
        )

    headers = dict(_PUSH2_HEADERS)
    headers.update(kwargs.pop('headers', {}) or {})
    timeout = kwargs.pop('timeout', 6)

    modes = [_PUSH2_OK_MODE] if _PUSH2_OK_MODE is not None else []
    modes.extend(m for m in (True, False) if m not in modes)

    subs = _push2_pick_subs(k=3)
    last_exc: Exception | None = None

    for sub in subs:
        # 每个 sub 内：仅试一种 mode（已记忆的成功模式或第一个候选）
        # 若该 sub 在所有 mode 下均失败再换下一个 sub
        sub_failed = False
        for trust_env in modes:
            url = f'https://{sub}.push2.eastmoney.com{path}'
            s = requests.Session()
            s.trust_env = trust_env
            try:
                resp = s.get(url, headers=headers, timeout=timeout, **kwargs)
                resp.raise_for_status()
                # 成功：清状态
                _PUSH2_OK_MODE = trust_env
                _PUSH2_FAIL_TS = 0.0
                _PUSH2_FAIL_COUNT = 0
                if _PUSH2_SUB_FAIL[sub] > 0:
                    _PUSH2_SUB_FAIL[sub] -= 1
                return resp
            except Exception as e:
                last_exc = e
                # HTTP 403/429 -> 直接拉黑该子域
                status_code = getattr(getattr(e, 'response', None), 'status_code', None)
                if status_code in (403, 429):
                    _PUSH2_SUB_BLACKLIST[sub] = time.time() + 300
                    _PUSH2_SUB_FAIL[sub] += 2
                    sub_failed = True
                    break
                # 其他失败：尝试下一个 mode
        if not sub_failed:
            _PUSH2_SUB_FAIL[sub] += 1
            if _PUSH2_SUB_FAIL[sub] >= 3:
                _PUSH2_SUB_BLACKLIST[sub] = time.time() + 300

    # 主 push2 全失败 -> 兜底尝试 push2delay（延时行情，限流宽松）
    # 仅尝试前 2 个 delay 主机，避免拖慢整体响应
    for host in _PUSH2DELAY_HOSTS[:2]:
        for trust_env in modes:
            url = f'https://{host}{path}'
            s = requests.Session()
            s.trust_env = trust_env
            try:
                resp = s.get(url, headers=headers, timeout=timeout, **kwargs)
                resp.raise_for_status()
                # 延时行情成功：清全局熔断（说明网络通），但不重置 OK_MODE
                # （delay 主机网段路由可能与 push2 主域不同）
                _PUSH2_FAIL_TS = 0.0
                _PUSH2_FAIL_COUNT = 0
                return resp
            except Exception as e:
                last_exc = e

    # 主域 + 延时域全失败：触发全局熔断
    _PUSH2_FAIL_TS = time.time()
    _PUSH2_FAIL_COUNT += 1
    next_cd = _push2_cooldown_for(_PUSH2_FAIL_COUNT)
    raise ConnectionError(
        f'push2 不可达（已试 {len(subs)} 主子域 + push2delay，进入 {next_cd}s 熔断）：{last_exc}'
    )
