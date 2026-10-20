import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler
from nodick.telegram import app, autopost, keyboards


class Queue:
    def __init__(self):
        self.scheduler=NS(running=True)
        self.next=datetime.now(timezone.utc)+timedelta(hours=1)
        self.pending=[]
        self.run_repeating=Mock()
        self.run_once=Mock(side_effect=lambda *args,**kwargs:self.pending.append(NS(name=kwargs['name'],next_t=self.next,schedule_removal=Mock())))
    def get_jobs_by_name(self,name):
        if name==autopost.JOB_NAME:
            return [NS(next_t=self.next)]
        return [job for job in self.pending if job.name==name]


@pytest.fixture
def store(monkeypatch):
    values={}
    get=lambda key,default='':values.get(key,default)
    for module in (autopost,keyboards):
        monkeypatch.setattr(module,'get_bot_setting',get)
    monkeypatch.setattr(autopost,'set_bot_setting',lambda key,value:values.__setitem__(key,value))
    monkeypatch.setattr(autopost.poster,'_get_channels',lambda:[{'id':'-100123'}])
    monkeypatch.setattr(autopost.poster,'cooldown_seconds_remaining',lambda:0,raising=False)
    monkeypatch.setattr(autopost.poster,'record_post_result',Mock(),raising=False)
    monkeypatch.setattr(app,'_is_admin',lambda update:True)
    monkeypatch.setattr(app,'_replace_with_text',AsyncMock())
    return values


def shapes():
    update=NS(callback_query=NS(data='toggle_autopost',answer=AsyncMock()),effective_user=NS(id=101))
    application=NS(bot_data={})
    context=NS(application=application,job_queue=Queue(),bot=object())
    return update,context


@pytest.mark.parametrize('enabled',['0','1'])
def test_settings_button_reflects_saved_on_off(store,enabled):
    store['auto_post_enabled']=enabled
    buttons=[b for row in keyboards.settings_keyboard().inline_keyboard for b in row]
    button=next(b for b in buttons if b.callback_data=='toggle_autopost')
    assert button.text.endswith('ON' if enabled=='1' else 'OFF')
    assert any(b.callback_data=='autopost_status' for b in buttons)


def test_toggle_readback_and_immediate_cooldown_check(store):
    update,context=shapes()
    asyncio.run(autopost.toggle(update,context))
    assert store['auto_post_enabled']=='1'
    context.job_queue.run_once.assert_called_once_with(autopost.scheduled_check,when=1,name=autopost.KICK_NAME)
    assert 'Auto-Post is now ON.' in app._replace_with_text.await_args.args[2]
    asyncio.run(autopost.toggle(update,context))
    assert store['auto_post_enabled']=='0'
    assert 'Auto-Post is now OFF.' in app._replace_with_text.await_args.args[2]


def test_toggle_failed_persistence_does_not_report_enabled(store,monkeypatch):
    monkeypatch.setattr(autopost,'set_bot_setting',lambda *args:None)
    update,context=shapes()
    asyncio.run(autopost.toggle(update,context))
    assert 'Could not save' in app._replace_with_text.await_args.args[2]
    context.job_queue.run_once.assert_not_called()


def test_status_differentiates_enabled_and_scheduler_health(store):
    store['auto_post_enabled']='1'
    store['auto_post_last_result']=json.dumps(dict(checked_at=datetime.now(timezone.utc).isoformat(),reason='No eligible videos have matched metadata.'))
    update,context=shapes()
    text=autopost.status_text(context)
    assert 'Automatic posting: ON' in text and 'Scheduler: RUNNING' in text
    assert 'Next scheduler check:' in text and 'No eligible videos' in text
    context.job_queue=None
    assert 'UNAVAILABLE' in autopost.status_text(context)


def test_admin_only_for_each_action(store,monkeypatch):
    monkeypatch.setattr(app,'_is_admin',lambda update:False)
    update,context=shapes()
    for action in (autopost.toggle,autopost.show_status,autopost.post_now):
        asyncio.run(action(update,context))
    assert not store
    app._replace_with_text.assert_not_awaited()


def test_scheduled_off_never_posts_but_manual_still_works(store,monkeypatch):
    send=AsyncMock(return_value=1)
    monkeypatch.setattr(autopost.poster,'send_auto_post',send)
    update,context=shapes()
    asyncio.run(autopost.scheduled_check(context))
    send.assert_not_awaited()
    assert asyncio.run(autopost._run(context,force=True))==1
    send.assert_awaited_once_with(context.bot,force=True)


def test_exception_is_recorded_and_later_check_retries(store,monkeypatch):
    store['auto_post_enabled']='1'
    send=AsyncMock(side_effect=[RuntimeError('failed'),1])
    monkeypatch.setattr(autopost.poster,'send_auto_post',send)
    _,context=shapes()
    assert asyncio.run(autopost._run(context))==0
    assert not context.application.bot_data['autopost_running']
    assert autopost.poster.record_post_result.call_args.args[0]=='error'
    assert asyncio.run(autopost._run(context))==1


def test_manual_and_scheduler_cannot_overlap(store,monkeypatch):
    store['auto_post_enabled']='1'
    async def exercise():
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow(*args,**kwargs):
            entered.set();await release.wait();return 1
        send=AsyncMock(side_effect=slow)
        monkeypatch.setattr(autopost.poster,'send_auto_post',send)
        _,context=shapes()
        task=asyncio.create_task(autopost._run(context))
        await entered.wait()
        assert await autopost._run(context,force=True) is None
        release.set();assert await task==1
        send.assert_awaited_once()
    asyncio.run(exercise())


def test_refresh_not_modified_is_harmless(store,monkeypatch):
    monkeypatch.setattr(app,'_replace_with_text',AsyncMock(side_effect=BadRequest('Message is not modified')))
    update,context=shapes()
    asyncio.run(autopost.show_status(update,context))


def test_real_job_queue_registers_and_executes_callback(store,monkeypatch):
    store['auto_post_enabled']='1'
    async def exercise():
        application=Application.builder().token('123:synthetic-test-token').build()
        done=asyncio.Event()
        async def posted(*args,**kwargs):
            done.set();return 1
        monkeypatch.setattr(autopost.poster,'send_auto_post',AsyncMock(side_effect=posted))
        autopost.setup_jobs(application)
        jobs=application.job_queue.get_jobs_by_name(autopost.JOB_NAME)
        assert len(jobs)==1
        assert jobs[0].callback is autopost.scheduled_check
        await application.job_queue.start()
        try:
            assert jobs[0].next_t is not None
            application.job_queue.run_once(autopost.scheduled_check,when=.01)
            await asyncio.wait_for(done.wait(),2)
        finally:
            await application.job_queue.stop()
    asyncio.run(exercise())


def test_real_application_registers_status_before_catchall(store,monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(app,'init_db',lambda:None)
    monkeypatch.setattr(app,'settings',replace(app.settings,bot_token='123:synthetic-test-token'))
    application=app.build_application()
    callbacks=[h for group in application.handlers.values() for h in group if isinstance(h,CallbackQueryHandler)]
    status_handler=next(h for h in callbacks if h.pattern is not None and h.pattern.match('autopost_status'))
    assert status_handler.callback is autopost.show_status
    assert callbacks.index(status_handler) < next(i for i,h in enumerate(callbacks) if h.pattern is None)
    assert application.job_queue.get_jobs_by_name(autopost.JOB_NAME)[0].callback is autopost.scheduled_check


def test_app_routes_status_and_legacy_controls(store,monkeypatch):
    update,context=shapes()
    toggle=AsyncMock();post=AsyncMock()
    monkeypatch.setattr(autopost,'toggle',toggle)
    monkeypatch.setattr(autopost,'post_now',post)
    asyncio.run(app.toggle_setting(update,context));toggle.assert_awaited_once()
    update.callback_query.data='prompt_autopostnow'
    asyncio.run(app.prompt_setting(update,context));post.assert_awaited_once()
