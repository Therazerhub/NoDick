import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from nodick.telegram import app


@pytest.mark.parametrize('premium',[False,True])
def test_sponsored_ads_respect_premium(monkeypatch,premium):
    monkeypatch.setattr(app,'is_user_premium',lambda uid:premium)
    monkeypatch.setattr(app,'_get_ad_frequency',lambda:1)
    monkeypatch.setattr(app,'_get_ad_config',lambda:dict(text='Example ad',button_text='Open',button_url='https://example.com'))
    update=NS(effective_user=NS(id=123),effective_chat=NS(id=456))
    context=NS(user_data={},bot=NS(send_message=AsyncMock()))
    asyncio.run(app._maybe_send_ad(update,context))
    assert context.bot.send_message.await_count == (0 if premium else 1)
    assert context.user_data.get('video_count',0) == (0 if premium else 1)
