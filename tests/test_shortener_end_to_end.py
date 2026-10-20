"""Real SQLite + real HTTPX request encoding, with an in-process fake provider."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_shortener_return_flow_with_real_storage(tmp_path):
    env=os.environ.copy();env.pop('DATABASE_URL',None);env['DB_PATH']=str(tmp_path/'flow.db')
    code='''
import asyncio,json
from dataclasses import replace
from urllib.parse import urlsplit,parse_qs
from unittest.mock import AsyncMock
import httpx
from nodick import db
from nodick.services import shortener

db.init_db()
shortener.settings=replace(shortener.settings,shortener_domain='links.example.com',shortener_api_key='synthetic-provider-credential',shortener_enabled=True)
shortener._check_dns=AsyncMock()
real_client=httpx.AsyncClient
requests=[]
async def provider(request):
    requests.append(request)
    return httpx.Response(200,json={'status':'success','shortenedUrl':'https://links.example.com/synthetic'})
shortener.httpx.AsyncClient=lambda **kwargs:real_client(transport=httpx.MockTransport(provider),**kwargs)
async def flow():
    ready=await shortener.prepare_premium_pass(101,'TestBot')
    assert ready['status']=='ready' and 'token' not in ready
    again=await shortener.prepare_premium_pass(101,'TestBot')
    assert again==ready and len(requests)==1
    destination=requests[0].url.params['url']
    payload=parse_qs(urlsplit(destination).query)['start'][0]
    token=payload.removeprefix('pass_')
    assert (await shortener.redeem_premium_pass(102,token))['status']=='invalid'
    activated=await shortener.redeem_premium_pass(101,token)
    assert activated['status']=='activated'
    assert db.is_user_premium(101)
    assert db.get_user_quota(101)['is_premium']
    assert (await shortener.redeem_premium_pass(101,token))['status']=='invalid'
    assert (await shortener.prepare_premium_pass(101,'TestBot'))['status']=='active'
    assert len(requests)==1
    print(json.dumps({'flow':'passed','provider_requests':1,'premium':True,'wrong_user_and_replay_rejected':True}))
asyncio.run(flow())
'''
    result=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[1],env=env,capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)=={'flow':'passed','provider_requests':1,'premium':True,'wrong_user_and_replay_rejected':True}
