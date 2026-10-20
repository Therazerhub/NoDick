import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.ext import ApplicationHandlerStop

from nodick.telegram import app
from nodick.telegram.welcome import DEFAULT_WELCOME_MEDIA, banner_from_message, decode_banner
from test_growth_reliability import _run_db_script


@pytest.mark.parametrize('extension,kind', [('jpg','photo'), ('png','photo'), ('webp','photo'), ('gif','animation'), ('mp4','video')])
def test_direct_urls(extension, kind):
    url = f'https://example.com/banner.{extension}?v=1'
    assert banner_from_message(NS(text=url)) == dict(type=kind, media=url)


@pytest.mark.parametrize('kind', ['photo', 'animation', 'video'])
def test_uploads(kind):
    item = NS(file_id='telegram-file')
    msg = NS(**{kind: [NS(file_id='small'), item] if kind == 'photo' else item})
    assert banner_from_message(msg) == dict(type=kind, media='telegram-file')


@pytest.mark.parametrize('text', ['https://tenor.com/view/example', 'not a url', 'ftp://example.com/a.jpg', 'https://user:pass@example.com/a.jpg'])
def test_invalid_urls(text):
    with pytest.raises(ValueError):
        banner_from_message(NS(text=text))


def test_default_and_clear():
    assert decode_banner(DEFAULT_WELCOME_MEDIA) == dict(type='photo', media='https://files.catbox.moe/j9r0xf.jpg')
    assert banner_from_message(NS(text='clear')) is None
    assert decode_banner('') is None


@pytest.mark.parametrize('kind', ['photo', 'video', 'animation', None])
@pytest.mark.parametrize('callback', [False, True])
def test_send_banner_and_callback_transitions(monkeypatch, kind, callback):
    raw = json.dumps(dict(type=kind, media='file-id')) if kind else ''
    monkeypatch.setattr(app, 'get_bot_setting', lambda *a: raw)
    bot = NS(**{f'send_{k}': AsyncMock() for k in ['photo','video','animation','message']})
    msg = NS(chat_id=123, delete=AsyncMock())
    update = NS(callback_query=NS(message=msg) if callback else None, message=msg)
    asyncio.run(app._send_welcome(update, NS(bot=bot), 'Welcome', 'keyboard'))
    method = getattr(bot, f'send_{kind or "message"}')
    method.assert_awaited_once()
    assert method.await_args.kwargs['reply_markup'] == 'keyboard'
    if callback:
        msg.delete.assert_awaited_once()
    else:
        msg.delete.assert_not_awaited()


def test_banner_failure_preserves_working_text_menu(monkeypatch):
    monkeypatch.setattr(app, 'get_bot_setting', lambda *a: DEFAULT_WELCOME_MEDIA)
    order = []
    async def send_text(**kwargs):
        order.append('sent')
        return NS(message_id=5)
    async def delete():
        order.append('deleted')
    bot = NS(send_photo=AsyncMock(side_effect=RuntimeError('unavailable')), send_message=AsyncMock(side_effect=send_text))
    update = NS(callback_query=NS(message=NS(chat_id=123, delete=delete)))
    asyncio.run(app._send_welcome(update, NS(bot=bot), 'Welcome', None))
    assert order == ['sent','deleted']


@pytest.mark.parametrize('text,success', [('https://example.com/image.jpg',True), ('clear',True), ('https://tenor.com/view/example',False)])
def test_admin_setting_save_clear_retry(monkeypatch, text, success):
    monkeypatch.setattr(app, '_is_admin', lambda _: True)
    monkeypatch.setattr(app, 'settings_keyboard', lambda: None)
    save = Mock()
    monkeypatch.setattr(app, 'set_bot_setting', save)
    msg = NS(text=text, reply_text=AsyncMock())
    context = NS(user_data={'waiting_for_setting':'welcomegif'}, bot=NS(send_photo=AsyncMock()))
    update = NS(message=msg, effective_chat=NS(id=123))
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(app.welcome_media_input(update, context))
    assert save.called == success
    assert ('waiting_for_setting' in context.user_data) != success
    if success:
        assert save.call_args.args[0] == 'welcome_media'
        if text == 'clear':
            assert save.call_args.args[1] == ''


def test_invalid_telegram_media_does_not_save(monkeypatch):
    monkeypatch.setattr(app, '_is_admin', lambda _: True)
    save = Mock()
    monkeypatch.setattr(app, 'set_bot_setting', save)
    msg = NS(text='https://example.com/image.jpg', reply_text=AsyncMock())
    context = NS(user_data={'waiting_for_setting':'welcomegif'}, bot=NS(send_photo=AsyncMock(side_effect=RuntimeError('invalid media'))))
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(app.welcome_media_input(NS(message=msg,effective_chat=NS(id=123)), context))
    save.assert_not_called()
    assert context.user_data['waiting_for_setting'] == 'welcomegif'


def test_nonadmin_cannot_change_banner(monkeypatch):
    monkeypatch.setattr(app, '_is_admin', lambda _: False)
    save = Mock()
    monkeypatch.setattr(app, 'set_bot_setting', save)
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(app.welcome_media_input(NS(), NS(user_data={'waiting_for_setting':'welcomegif'})))
    save.assert_not_called()


def test_setting_persists_and_clear_survives_restart(tmp_path):
    result = _run_db_script(tmp_path, '''
import json
from nodick import db
from nodick.telegram.welcome import DEFAULT_WELCOME_MEDIA
db.init_db()
initial = db.get_bot_setting('welcome_media', DEFAULT_WELCOME_MEDIA)
db.set_bot_setting('welcome_media', json.dumps({'type':'photo','media':'file-id'}))
db.init_db()
saved = db.get_bot_setting('welcome_media', DEFAULT_WELCOME_MEDIA)
db.set_bot_setting('welcome_media','')
db.init_db()
print(json.dumps({'initial':json.loads(initial), 'saved':json.loads(saved), 'cleared': db.get_bot_setting('welcome_media',DEFAULT_WELCOME_MEDIA)}))
''')
    assert result['initial']['media'] == 'https://files.catbox.moe/j9r0xf.jpg'
    assert result['saved']['media'] == 'file-id'
    assert result['cleared'] == ''
