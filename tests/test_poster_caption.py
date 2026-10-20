import asyncio
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nodick.services import poster
from nodick.telegram import app


class CaptionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = ""

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "blockquote"}
        assert not attrs
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag

    def handle_data(self, data):
        self.text += data


def test_draft_one_exact_structure_and_escaping():
    caption = poster.build_auto_post_caption({
        "stashdb_title": "Actual <Title>", "title": "Fallback",
        "stashdb_performer": "A & B", "stashdb_studio": 'Studio "One"',
        "category": "Behind-the Scenes!", "description": "Do not invent this",
    }, "actual_bot")
    assert caption == (
        '<b>Actual &lt;Title&gt;</b>\n\n'
        '➜ Featuring: <b>A &amp; B</b>\n'
        '➜ Studio: <b>Studio &quot;One&quot;</b>\n'
        '➜ Category: <b>Behind-the Scenes!</b>\n\n'
        '<blockquote>#Behind_the_Scenes\nOpen the complete video in @actual_bot.</blockquote>'
    )


@pytest.mark.parametrize("placeholder", [None, "", "Unknown", "N/A", "Video_123", "file", "123", "Not set"])
def test_missing_metadata_omitted(placeholder):
    result = poster.build_auto_post_caption(dict.fromkeys(
        ["title", "stashdb_title", "stashdb_performer", "stashdb_studio", "category"], placeholder), "real_bot")
    assert result == '<blockquote>Open the complete video in @real_bot.</blockquote>'


def test_placeholder_stash_title_falls_back_to_actual_title():
    assert poster.build_auto_post_caption({"stashdb_title": "Unknown", "title": "Actual title"}, "bot").startswith('<b>Actual title</b>')


@pytest.mark.parametrize("character", ["😀", "&", "<", "界"])
def test_caption_limit_and_balanced_entities(character):
    video = dict.fromkeys(["stashdb_title", "stashdb_performer", "stashdb_studio", "category"], character * 3000)
    caption = poster.build_auto_post_caption(video, '<bot&' * 100)
    parser = CaptionParser()
    parser.feed(caption)
    parser.close()
    assert not parser.stack
    assert len(parser.text.encode('utf-16-le')) // 2 <= 1024
    assert '…' in parser.text
    assert '&lt;bot&amp;' in caption


def test_autopost_html_buttons_and_actual_bot(monkeypatch):
    video = {"id": 123, "title": "Real title", "category": "Test"}
    monkeypatch.setattr(poster, '_cooldown_active', lambda: False)
    monkeypatch.setattr(poster, '_get_channels', lambda: [{"id": "-1001"}])
    monkeypatch.setattr(poster, '_select_video', lambda: video)
    monkeypatch.setattr(poster, '_fetch_teaser_source', AsyncMock(return_value=(b'source', 'telegram')))
    monkeypatch.setattr(poster, 'create_blurred_thumbnail', lambda _: b'rendered')
    monkeypatch.setattr(poster, '_mark_posted', lambda _: None)
    bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username='actual_bot')), send_photo=AsyncMock())
    assert asyncio.run(poster.send_auto_post(bot, force=True)) == 1
    sent = bot.send_photo.call_args.kwargs
    assert sent['parse_mode'] == 'HTML'
    assert '@actual_bot.' in sent['caption']
    rows = sent['reply_markup'].inline_keyboard
    assert [[b.text for b in row] for row in rows] == [['Premium', 'How to Watch'], ['Open Video']]
    assert [[b.url for b in row] for row in rows] == [
        ['https://t.me/actual_bot?start=premium', 'https://t.me/actual_bot?start=how_to_watch'],
        ['https://t.me/actual_bot?start=vid_123'],
    ]


@pytest.mark.parametrize('payload', ['premium', 'how_to_watch'])
@pytest.mark.parametrize('gated', [False, True])
def test_cta_start_routes_and_preserves_join_action(monkeypatch, payload, gated):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42), callback_query=None)
    context = SimpleNamespace(args=[payload], user_data={})
    monkeypatch.setattr(app, 'user_exists', lambda _: True)
    monkeypatch.setattr(app, 'ensure_user_exists', lambda _: None)
    monkeypatch.setattr(app, 'record_growth_event', lambda *a, **kw: None)
    gate = AsyncMock(return_value=gated)
    show = AsyncMock()
    monkeypatch.setattr(app, '_check_force_join', gate)
    monkeypatch.setattr(app, '_show_autopost_action', show)
    asyncio.run(app.start(update, context))
    gate.assert_awaited_once_with(update, context, pending_action=payload)
    assert show.await_count == (0 if gated else 1)


@pytest.mark.parametrize('payload', ['premium', 'how_to_watch'])
def test_cta_resumes_after_join(monkeypatch, payload):
    query = SimpleNamespace(answer=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42), callback_query=query)
    context = SimpleNamespace(user_data={'pending_after_join': payload}, bot=SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status='member'))))
    monkeypatch.setattr(app, '_force_join_config', lambda: {'enabled': True, 'valid': True, 'channels': [{'id': 1}]})
    monkeypatch.setattr(app, 'record_growth_event', lambda *a, **kw: None)
    show = AsyncMock()
    monkeypatch.setattr(app, '_show_autopost_action', show)
    asyncio.run(app.verify_join_callback(update, context))
    show.assert_awaited_once_with(update, context, payload)
    assert 'pending_after_join' not in context.user_data


@pytest.mark.parametrize('callback', [False, True])
@pytest.mark.parametrize('payload', ['premium', 'how_to_watch'])
def test_cta_destinations_support_command_and_callback(monkeypatch, callback, payload):
    update = SimpleNamespace(callback_query=SimpleNamespace() if callback else None, message=SimpleNamespace(reply_text=AsyncMock()))
    premium = AsyncMock()
    replace = AsyncMock()
    monkeypatch.setattr(app, 'get_premium_callback', premium)
    monkeypatch.setattr(app, '_replace_with_text', replace)
    asyncio.run(app._show_autopost_action(update, SimpleNamespace(), payload))
    if payload == 'premium':
        premium.assert_awaited_once()
    elif callback:
        replace.assert_awaited_once()
    else:
        assert 'How to Watch' in update.message.reply_text.call_args.args[0]
