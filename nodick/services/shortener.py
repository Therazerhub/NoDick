"""Configurable common shortener API and account-bound return-token rewards.

This validates possession of a return token, NOT provider-confirmed ad views.
Providers with other protocols require an adapter; eligibility is operator-owned.
"""
import asyncio
from collections import OrderedDict
import time
import ipaddress
import logging
import re
import socket
from urllib.parse import quote, urlsplit

import httpx
from nodick.config import settings
from nodick.db import get_bot_setting, is_user_premium

log = logging.getLogger(__name__)
_locks = [asyncio.Lock() for _ in range(32)]
_api_gate = asyncio.Semaphore(4)
_attempts = OrderedDict()
PROVIDER_DEADLINE = 15
TOKEN_PATTERN = re.compile(r'[A-Za-z0-9_-]{32}\Z')


class _RedactProviderQueries(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        # urllib3 DEBUG records otherwise print GET query credentials.
        key = settings.shortener_api_key
        if key:
            message = message.replace(key, '[REDACTED]').replace(quote(key, safe=''), '[REDACTED]')
        message = re.sub(r'\?[^\s\"\']+', '?[REDACTED]', message)
        message = re.sub(r'pass_[A-Za-z0-9_-]{32}', 'pass_[REDACTED]', message)
        record.msg, record.args = message, ()
        return True


for _logger in ('urllib3.connectionpool', 'requests.packages.urllib3.connectionpool', 'httpx', 'httpcore.http11'):
    logging.getLogger(_logger).addFilter(_RedactProviderQueries())


class ShortenerError(Exception):
    """Intentionally contains no request URLs, credentials, or provider payloads."""


def _public_hostname(host):
    if not host or len(host) > 253:
        raise ShortenerError('Invalid provider hostname')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if '.' not in host or not re.fullmatch(r'[A-Za-z0-9.-]+', host) or host.endswith('.'):
            raise ShortenerError('Invalid provider hostname')
        if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in host.split('.')):
            raise ShortenerError('Invalid provider hostname')
        if host.lower().endswith(('.local', '.localhost', '.internal', '.test', '.invalid')):
            raise ShortenerError('Private provider hostname')
    else:
        if not ip.is_global:
            raise ShortenerError('Private provider address')
    return host.lower()


def provider_endpoint():
    domain = settings.shortener_domain
    if not domain:
        raise ShortenerError('Provider not configured')
    url = urlsplit(domain if '://' in domain else 'https://' + domain)
    if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443) or url.query or url.fragment or url.path not in ('', '/'):
        raise ShortenerError('Configure an HTTPS domain without a path')
    _public_hostname(url.hostname)
    path = settings.shortener_api_path
    if not re.fullmatch(r'/[A-Za-z0-9_./-]*', path) or path.startswith('//') or '..' in path:
        raise ShortenerError('Invalid provider API path')
    for key in (settings.shortener_api_param, settings.shortener_url_param):
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,40}', key):
            raise ShortenerError('Invalid provider parameter')
    if settings.shortener_api_param == settings.shortener_url_param:
        raise ShortenerError('Provider parameter collision')
    return f'https://{url.netloc}{path}'


def configured():
    if not settings.shortener_enabled or not settings.shortener_api_key:
        return False
    try:
        provider_endpoint()
        return get_bot_setting('shortener_enabled', '1') == '1'
    except Exception:
        return False


async def _check_dns(host):
    addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ShortenerError('Provider resolved to a non-public address')


def _validate_short_url(value, destination):
    if not isinstance(value, str) or len(value) > 2048 or value != value.strip():
        raise ShortenerError('Invalid shortened link')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment:
        raise ShortenerError('Invalid shortened link')
    host = _public_hostname(parsed.hostname)
    configured_hosts = settings.shortener_link_domains
    allowed = {_public_hostname(h.strip()) for h in configured_hosts.split(',') if h.strip()} if configured_hosts else {urlsplit(provider_endpoint()).hostname.lower()}
    if host not in allowed or value == destination or settings.shortener_api_key in value:
        raise ShortenerError('Unexpected shortened-link host or content')
    if 'pass_' in value or (destination and quote(destination, safe='') in value):
        raise ShortenerError('Provider returned a destination instead of a short link')
    return value


async def shorten_destination(destination):
    """Cancellable 15-second deadline includes gate, DNS and full response."""
    try:
        async with asyncio.timeout(PROVIDER_DEADLINE):
            async with _api_gate:
                endpoint = provider_endpoint()
                await _check_dns(urlsplit(endpoint).hostname)
                params = {settings.shortener_api_param: settings.shortener_api_key, settings.shortener_url_param: destination}
                async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3), follow_redirects=False) as client:
                    async with client.stream('GET', endpoint, params=params) as response:
                        if response.status_code != 200:
                            raise ShortenerError('Provider request failed')
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 65536:
                                raise ShortenerError('Provider response too large')
                        import json
                        payload = json.loads(body)
                if not isinstance(payload, dict) or payload.get('status') in ('error', 'fail', 'failed', False):
                    raise ShortenerError('Provider rejected the request')
                value = payload
                for component in settings.shortener_url_field.split('.'):
                    value = value[component]
                return _validate_short_url(value, destination)
    except Exception:
        # Exceptions can contain query credentials and raw destination tokens.
        # Cancellation unwinds both stream and client context managers.
        raise ShortenerError('Unable to create a short link') from None


async def prepare_premium_pass(user_id, bot_username):
    if not configured():
        return {'status': 'disabled'}
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', bot_username or ''):
        return {'status': 'error'}
    from nodick import premium_passes as passes
    async with _locks[user_id % len(_locks)]:
        if await asyncio.to_thread(is_user_premium, user_id):
            return {'status': 'active', 'premium_until': await asyncio.to_thread(passes.active_pass_until, user_id)}
        pending = await asyncio.to_thread(passes.pending_pass, user_id)
        if pending:
            # Revalidate host after provider/domain changes; never return an old
            # provider's link if configuration now points elsewhere.
            try:
                _validate_short_url(pending['shortened_url'], '')
            except Exception:
                pass
            else:
                return {'status': 'ready', **pending}
        now = time.monotonic()
        if now - _attempts.get(user_id, -float('inf')) < 20:
            return {'status': 'error'}
        _attempts[user_id] = now
        _attempts.move_to_end(user_id)
        while len(_attempts) > 4096:
            _attempts.popitem(last=False)
        issued = await asyncio.to_thread(passes.issue_pass, user_id, 1800)
        if issued['status'] != 'issued':
            return issued
        token = issued['token']
        destination = f'https://t.me/{bot_username}?start=pass_{token}'
        try:
            shortened = await shorten_destination(destination)
            ready = await asyncio.to_thread(passes.mark_pass_ready, user_id, token, shortened)
            if not ready:
                raise ShortenerError('Pass was superseded or expired')
            stored = await asyncio.to_thread(passes.pending_pass, user_id)
            if not stored or stored['shortened_url'] != shortened:
                raise ShortenerError('Pass readback failed')
            return {'status': 'ready', 'shortened_url': shortened, 'expires_at': issued['expires_at']}
        except Exception:
            await asyncio.to_thread(passes.cancel_pass, user_id, token)
            log.warning('Sponsored pass link creation failed')
            return {'status': 'error'}


async def redeem_premium_pass(user_id, token):
    if not configured():
        return {'status': 'disabled'}
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        return {'status': 'invalid'}
    from nodick import premium_passes as passes
    return await asyncio.to_thread(passes.redeem_pass, user_id, token)
