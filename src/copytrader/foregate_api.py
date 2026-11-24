"""ForeGate REST and public market-data transport."""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


HOST = 'https://openapi.foregate.com'
PUBLIC_MARKET_HOST = 'https://apis.foregate.com/api/userMarket'
PUBLIC_MARKET_WS = 'wss://openapiws.foregate.com/ws/market'


def user_id(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ApiError('ForeGate 用户标识无效。')
    return value


class ApiError(ValueError):
    def __init__(self, message, status=0, uncertain=False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _public_market_data(path, params):
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f'{PUBLIC_MARKET_HOST}{path}?{query}', headers={'Accept': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
            payload = json.loads(response.read(12 * 1024 * 1024))
    except (OSError, ValueError):
        raise ApiError('ForeGate 公开市场目录读取失败。') from None
    if not isinstance(payload, dict) or payload.get('code') != 200 or not isinstance(payload.get('data'), dict):
        raise ApiError('ForeGate 公开市场目录暂时无法返回此分类。')
    return payload['data']


def public_market_catalog(category_id, page, page_size):
    return _public_market_data('/orderBookMarket/list', {'categoryIds': category_id, 'pageNum': page,
                                                        'pageSize': page_size, 'network': 'mainnet'})


def public_market_cycles(category_id, page, page_size):
    return _public_market_data('/cycle/series', {'categoryL2': category_id, 'pageNum': page,
                                                'pageSize': page_size, 'sort': 'DEFAULT'})


def public_book(asset_id):
    from websocket import WebSocketException, create_connection

    socket_timeout = 5
    connection = None
    try:
        connection = create_connection(PUBLIC_MARKET_WS, timeout=socket_timeout, redirect_limit=0, suppress_origin=True)
        connection.send(json.dumps({'assets_ids': [asset_id], 'type': 'market', 'initial_dump': True, 'level': 2}))
        deadline = time.monotonic() + socket_timeout
        while (remaining := deadline - time.monotonic()) > 0:
            connection.settimeout(remaining)
            frame = connection.recv()
            if not frame:
                break
            try:
                message = json.loads(frame)
            except ValueError:
                continue
            for event in message if isinstance(message, list) else [message]:
                if isinstance(event, dict) and event.get('event_type') == 'book' and event.get('asset_id') == asset_id:
                    return event
    except (OSError, WebSocketException):
        raise ApiError('ForeGate 公开行情流连接失败或超时。') from None
    finally:
        if connection is not None:
            connection.shutdown()
    raise ApiError('ForeGate 公开行情流未返回所选盘口的订单簿。')


def session_expired(cookie):
    try:
        jar = SimpleCookie(cookie)
        for name in ('Authorization', 'accessToken'):
            if name not in jar:
                continue
            token = jar[name].value.split('.')
            if len(token) == 3:
                part = token[1]
                expiry = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))).get('exp')
                if isinstance(expiry, (int, float)):
                    return expiry <= datetime.now(timezone.utc).timestamp() + 30
    except (ValueError, CookieError, TypeError):
        pass
    return not cookie


class ForeGateAPI:
    def __init__(self, credentials, account, opener=None):
        keys = ('appKey', 'appSecret', 'apiKey')
        if not isinstance(credentials, dict) or not all(isinstance(credentials.get(key), str) and
                re.fullmatch(r'[!-~]{1,1024}', credentials[key]) for key in keys):
            raise ApiError('请填写有效的 APP Key、APP Secret 和 API Key。')
        self.credentials = {key: credentials[key] for key in keys}
        registration = account.get('registration')
        if not isinstance(registration, dict) or not registration.get('marchantUserId'):
            raise ApiError('请填写 ForeGate 商户用户标识。')
        self.registration = {key: registration[key] for key in ('marchantUserId', 'merchantId', 'userEmail') if key in registration}
        self.account_id = user_id(account['profile_user_id']) if account.get('profile_user_id') else None
        self.registered_id = user_id(account['user_id']) if account.get('user_id') else self.account_id
        self.known_ids = {self.account_id, self.registered_id} - {None}
        self.cookie = ''
        self.verified = False
        self.lock = threading.Lock()
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    @classmethod
    def enroll(cls, credentials, registration, expected_id):
        client = cls(credentials, {'registration': registration, 'profile_user_id': expected_id})
        client._register(enrolling=True)
        return client

    def signed_request(self, path, method, query, body, cookie):
        pairs = sorted((key, str(value)) for key, value in (query or {}).items())
        resource = path + ('?' + '&'.join(key + '=' + value for key, value in pairs) if pairs else '')
        url = HOST + path + ('?' + urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote) if pairs else '')
        post = method == 'POST'
        raw = json.dumps(body, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode() if body is not None else None
        content_type = 'application/json' if post else ''
        digest = base64.b64encode(hashlib.md5(raw or b'').digest()).decode() if post else ''
        date = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        headers = {'accept': 'application/json', 'date': date, 'x-ca-key': self.credentials['appKey'],
                   'x-api-key': self.credentials['apiKey'], 'x-nonce': str(uuid.uuid4())}
        if cookie:
            headers['cookie'] = cookie
        if post:
            headers.update({'content-type': content_type, 'content-md5': digest})
        signed = ('cookie', 'x-api-key', 'x-nonce') if path == '/ordering/query-orders' else ('x-api-key',)
        canonical = '\n'.join([method, headers['accept'], digest, content_type, date,
                              *(k + ':' + headers[k] for k in signed), resource])
        headers['x-ca-signature-headers'] = ','.join(signed)
        headers['x-ca-signature'] = base64.b64encode(hmac.new(self.credentials['appSecret'].encode(), canonical.encode(), hashlib.sha256).digest()).decode()
        return urllib.request.Request(url, data=raw if raw is not None else (b'' if post else None), headers=headers, method=method)

    def _send(self, path, method='GET', query=None, body=None, cookie='', mutation=False):
        request = self.signed_request(path, method, query, body, cookie)
        try:
            response = self.opener.open(request, timeout=12)
        except urllib.error.HTTPError as exc:
            response = exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.gaierror):
                raise ApiError('当前网络无法解析 ForeGate API 域名。') from None
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise ApiError('ForeGate API 的 HTTPS 证书验证失败。') from None
            raise ApiError('ForeGate 连接失败或超时。', uncertain=mutation) from None
        except (OSError, ValueError):
            raise ApiError('ForeGate 连接失败或超时。', uncertain=mutation) from None
        with response:
            status = response.code
            try:
                payload = json.loads(response.read(12 * 1024 * 1024))
            except (OSError, ValueError):
                payload = None
            if status == 200 and isinstance(payload, dict) and payload.get('code') == 0:
                rotated = ''
                if path == '/user/register':
                    jar = SimpleCookie()
                    try:
                        for value in response.headers.get_all('Set-Cookie') or ():
                            jar.load(value)
                        rotated = '; '.join(jar[name].OutputString(attrs=[]) for name in ('Authorization', 'accessToken', 'acw_tc') if name in jar)
                    except CookieError:
                        pass
                return payload, rotated
        code = payload.get('code') if isinstance(payload, dict) else None
        code = code if type(code) is int else None
        if status == 401:
            raise ApiError('ForeGate 鉴权失败，会话或商户权限需核验。', status)
        # Only a structured rejection establishes that the gateway processed the request.
        uncertain = mutation and (status not in {200, 400, 403, 404, 422} or code is None)
        suffix = f'，业务码 {code}' if code is not None else ''
        step = {
            '/user/mfa/status': '读取账户验证方式',
            '/user/mfa/setup/totp/init': '获取验证器绑定密钥',
            '/user/mfa/setup/totp/complete': '确认验证器绑定',
            '/user/mfa/verification/create': '开启本次提现验证',
            '/user/mfa/verification/send-email-otp': '发送邮箱验证码',
            '/user/mfa/verification/verify': '校验本次验证码',
        }.get(path, '请求')
        raise ApiError(f'ForeGate {step}失败（HTTP {status}{suffix}）。', status, uncertain)

    def _verify(self, cookie):
        profile = self._send('/user/profile', cookie=cookie)[0].get('data')
        if not isinstance(profile, dict) or self.account_id and str(profile.get('userId')) != self.account_id:
            raise ApiError('会话归属与预期的 ForeGate API 账户不一致，账户操作已停止。')
        if profile.get('isActive') is False:
            raise ApiError('ForeGate API 账户当前未激活。')
        return profile

    def _register(self, *, enrolling=False):
        response, candidate = self._send('/user/register', 'POST', body=self.registration)
        registered = response.get('data')
        if not candidate or not isinstance(registered, dict):
            raise ApiError('ForeGate 未返回完整的账户会话。')
        registered_id = user_id(registered.get('userId'))
        if not enrolling and registered_id not in self.known_ids:
            raise ApiError('会话更新未验证为原有的 ForeGate API 账户。')
        profile_id = user_id(self._verify(candidate).get('userId'))
        self.account_id, self.registered_id = profile_id, registered_id
        self.known_ids = {profile_id, registered_id}
        self.cookie, self.verified = candidate, True

    def ensure_session(self, force=False):
        with self.lock:
            if self.verified and not force and not session_expired(self.cookie):
                return self.cookie
            if not self.account_id:
                raise ApiError('请先连接 ForeGate API 账户。')
            self.verified = False
            self.cookie = ''
            self._register()
            return self.cookie

    def call(self, path, query=None, body=None, *, post=False, public=False, mutation=False, envelope=False):
        method = 'POST' if post or body is not None else 'GET'
        cookie = '' if public else self.ensure_session()
        try:
            result = self._send(path, method, query, body, cookie, mutation)[0]
        except ApiError as exc:
            if exc.status != 401 or public or mutation:
                raise
            cookie = self.ensure_session(force=True)
            result = self._send(path, method, query, body, cookie)[0]
        return result if envelope else result.get('data')
