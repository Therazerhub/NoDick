import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock, AsyncMock
import httpx
_REAL_CLIENT = httpx.AsyncClient
import pytest
import nodick
from nodick.services import shortener


@pytest.fixture
def config(monkeypatch):
    values = NS(shortener_domain='links.example.com', shortener_api_key='test-only-provider-key',
                shortener_enabled=True, shortener_api_path='/api', shortener_api_param='api',
                shortener_url_param='url', shortener_url_field='shortenedUrl', shortener_link_domains='')
    monkeypatch.setattr(shortener,'settings',values)
    monkeypatch.setattr(shortener,'get_bot_setting',lambda key,default='':default)
    monkeypatch.setattr(shortener,'_check_dns',AsyncMock())
    shortener._attempts.clear()
    return values


@pytest.mark.parametrize('domain',['http://links.example.com','localhost','127.0.0.1','https://links.example.com/api','https://u:p@links.example.com','https://links.example.com:444','https://links.example.com?q=x','private.local','private.internal'])
def test_unsafe_provider_config_disabled(config,domain):
    config.shortener_domain=domain
    assert not shortener.configured()


def test_two_env_configuration_and_admin_disable(config,monkeypatch):
    assert shortener.provider_endpoint()=='https://links.example.com/api'
    assert shortener.configured()
    monkeypatch.setattr(shortener,'get_bot_setting',lambda *args:'0')
    assert not shortener.configured()
    config.shortener_api_key=''
    assert not shortener.configured()


def install_transport(monkeypatch, payload, status=200):
    calls = []
    async def handle(request):
        calls.append(request)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return httpx.Response(status, content=data)
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(shortener.httpx, 'AsyncClient', lambda **kwargs: _REAL_CLIENT(transport=transport, **kwargs))
    return calls


def test_common_api_params_are_encoded_and_response_checked(config,monkeypatch):
    calls=install_transport(monkeypatch,{'status':'success','shortenedUrl':'https://links.example.com/abc'})
    target='https://t.me/TestBot?start=pass_'+'a'*32
    assert asyncio.run(shortener.shorten_destination(target))=='https://links.example.com/abc'
    assert calls[0].url.params['api']==config.shortener_api_key
    assert calls[0].url.params['url']==target


@pytest.mark.parametrize('payload,status',[
    ({'status':'error','message':'sensitive provider error'},200),
    ({'status':'success','shortenedUrl':'http://links.example.com/abc'},200),
    ({'status':'success','shortenedUrl':'https://evil.example.com/abc'},200),
    ({'status':'success','shortenedUrl':'https://links.example.com/pass_abc'},200),
    ({'status':'success','shortenedUrl':'https://links.example.com/'+ 'test-only-provider-key'},200),
    (b'not-json',200), (b'x'*65537,200), ({},302), ({},500),
])
def test_provider_errors_are_sanitized(config,monkeypatch,payload,status):
    install_transport(monkeypatch,payload,status)
    with pytest.raises(shortener.ShortenerError) as error:
        asyncio.run(shortener.shorten_destination('https://t.me/TestBot?start=pass_'+'a'*32))
    assert str(error.value)=='Unable to create a short link'
    assert config.shortener_api_key not in str(error.value)


def test_alternate_fields_and_link_domain(config,monkeypatch):
    config.shortener_url_field='data.url'
    config.shortener_api_param='key'
    config.shortener_url_param='long_url'
    config.shortener_link_domains='go.example.com'
    install_transport(monkeypatch,{'data':{'url':'https://go.example.com/a'}})
    assert asyncio.run(shortener.shorten_destination('https://t.me/TestBot?start=pass_'+'a'*32))=='https://go.example.com/a'


def fake_passes(monkeypatch):
    module=NS(active_pass_until=Mock(return_value=0), pending_pass=Mock(return_value=None),
              issue_pass=Mock(return_value={'status':'issued','token':'a'*32,'expires_at':12345}),
              mark_pass_ready=Mock(return_value=True),cancel_pass=Mock(),redeem_pass=Mock(return_value={'status':'activated','premium_until':99999}))
    monkeypatch.setattr(nodick,'premium_passes',module,raising=False)
    monkeypatch.setattr(shortener,'is_user_premium',lambda uid:False)
    return module


def test_prepare_reuses_saved_link_without_provider_call(config,monkeypatch):
    passes=fake_passes(monkeypatch)
    passes.pending_pass.return_value={'shortened_url':'https://links.example.com/abc','expires_at':12345}
    shorten=AsyncMock()
    monkeypatch.setattr(shortener,'shorten_destination',shorten)
    result=asyncio.run(shortener.prepare_premium_pass(101,'TestBot'))
    assert result['status']=='ready'
    shorten.assert_not_called()
    passes.issue_pass.assert_not_called()


def test_prepare_only_exposes_short_link_and_verifies_persistence(config,monkeypatch):
    passes=fake_passes(monkeypatch)
    passes.pending_pass.side_effect=[None,{'shortened_url':'https://links.example.com/abc','expires_at':12345}]
    shorten=AsyncMock(return_value='https://links.example.com/abc')
    monkeypatch.setattr(shortener,'shorten_destination',shorten)
    result=asyncio.run(shortener.prepare_premium_pass(101,'TestBot'))
    assert result=={'status':'ready','shortened_url':'https://links.example.com/abc','expires_at':12345}
    assert 'token' not in result
    assert shorten.call_args.args[0]=='https://t.me/TestBot?start=pass_'+'a'*32


def test_failed_provider_cancels_pending_and_retry_is_throttled(config,monkeypatch,caplog):
    passes=fake_passes(monkeypatch)
    shorten=AsyncMock(side_effect=RuntimeError(config.shortener_api_key))
    monkeypatch.setattr(shortener,'shorten_destination',shorten)
    assert asyncio.run(shortener.prepare_premium_pass(101,'TestBot'))=={'status':'error'}
    passes.cancel_pass.assert_called_once_with(101,'a'*32)
    assert asyncio.run(shortener.prepare_premium_pass(101,'TestBot'))=={'status':'error'}
    assert shorten.call_count==1
    assert config.shortener_api_key not in caplog.text


def test_existing_premium_skips_provider(config,monkeypatch):
    passes=fake_passes(monkeypatch)
    monkeypatch.setattr(shortener,'is_user_premium',lambda uid:True)
    assert asyncio.run(shortener.prepare_premium_pass(101,'TestBot'))['status']=='active'
    passes.issue_pass.assert_not_called()


def test_debug_transport_logs_redact_credentials_and_tokens(config, caplog):
    import logging
    logger = logging.getLogger('urllib3.connectionpool')
    with caplog.at_level(logging.DEBUG, logger='urllib3.connectionpool'):
        logger.debug('GET /api?api=%s&url=https%%3A%%2F%%2Ft.me%%2Ftest%%3Fstart%%3Dpass_%s HTTP/1.1', config.shortener_api_key, 'a'*32)
    assert config.shortener_api_key not in caplog.text
    assert 'a'*32 not in caplog.text
    assert '[REDACTED]' in caplog.text


@pytest.mark.parametrize('token',['','a'*31,'a'*33,'a'*31+'!','a'*32+'\n'])
def test_malformed_return_never_reaches_db(config,monkeypatch,token):
    passes=fake_passes(monkeypatch)
    assert asyncio.run(shortener.redeem_premium_pass(101,token))=={'status':'invalid'}
    passes.redeem_pass.assert_not_called()
