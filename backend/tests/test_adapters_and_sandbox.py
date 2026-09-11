import asyncio
import json
import socket
import httpx
import pytest
from forge.providers import complete,ProviderError,anthropic_messages,validate_private_target
from forge.sandbox import Command,docker_args
from forge.config import settings

MODEL={'model_id':'user-selected-model','allowed_parameters':['max_tokens'],'parameters':{'max_tokens':100}}

def test_openai_compatible_wire_format_and_tool_calls():
    def handler(request):
        assert str(request.url)=='https://provider.example/v1/chat/completions'
        assert request.headers['authorization']=='Bearer secret-test'
        body=json.loads(request.content)
        assert body['model']=='user-selected-model' and body['max_tokens']==100
        assert body['tools'][0]['function']['name']=='read_file'
        assert body['messages'][1]['tool_calls'][0]['function']['arguments']=='{"path": "a.py"}'
        assert 'anthropic_native' not in str(body)
        return httpx.Response(200,json={'choices':[{'message':{'content':'reading','tool_calls':[{'id':'id2','function':{'name':'read_file','arguments':'{"path":"b.py"}'}}]}}],'usage':{'prompt_tokens':11,'completion_tokens':7}})
    messages=[{'role':'user','content':'read code'},{'role':'assistant','content':'','calls':[{'id':'id1','name':'read_file','arguments':{'path':'a.py'}}]}, {'role':'tool','call_id':'id1','content':'value'}]
    result=asyncio.run(complete({'protocol':'openai_chat','base_url':'https://provider.example/v1','external':True},MODEL,messages,[{'name':'read_file','description':'read','parameters':{'type':'object'}}],'secret-test',10,httpx.MockTransport(handler)))
    assert result.calls[0]['arguments']['path']=='b.py' and result.input_tokens==11 and result.output_tokens==7

def test_anthropic_native_blocks_and_tool_results_preserved():
    native=[{'type':'thinking','thinking':'synthetic test fixture','signature':'test-signature'},{'type':'tool_use','id':'t1','name':'read_file','input':{'path':'x'}}]
    messages=[{'role':'system','content':'System instruction'},{'role':'user','content':'Task'},{'role':'assistant','content':'','calls':[{'id':'t1','name':'read_file','arguments':{'path':'x'}}],'anthropic_native':native},{'role':'tool','call_id':'t1','content':'file content'}]
    def handler(request):
        body=json.loads(request.content)
        assert str(request.url)=='https://provider.example/v1/messages'
        assert request.headers['x-api-key']=='anthropic-test-secret'
        assert body['system']=='System instruction'
        assert body['messages'][1]['content']==native
        assert body['messages'][2]['content'][0]['tool_use_id']=='t1'
        assert all(m['role']!='system' for m in body['messages'])
        return httpx.Response(200,json={'content':[{'type':'text','text':'Done'}],'usage':{'input_tokens':12,'output_tokens':2}})
    result=asyncio.run(complete({'protocol':'anthropic_messages','base_url':'https://provider.example/v1','external':True},MODEL,messages,[],'anthropic-test-secret',10,httpx.MockTransport(handler)))
    assert result.text=='Done' and result.native==[{'type':'text','text':'Done'}]

def test_error_body_cannot_leak_key():
    handler=lambda request:httpx.Response(401,json={'error':'echo SECRET-PASSWORD'})
    with pytest.raises(ProviderError) as error:
        asyncio.run(complete({'protocol':'openai_chat','base_url':'https://provider.example/v1','external':True},MODEL,[{'role':'user','content':'test'}],[],'SECRET-PASSWORD',10,httpx.MockTransport(handler)))
    assert 'SECRET-PASSWORD' not in str(error.value) and not error.value.retryable

def test_rate_limit_is_eligible_for_explicit_fallback():
    with pytest.raises(ProviderError) as error:
        asyncio.run(complete({'protocol':'openai_chat','base_url':'https://provider.example/v1','external':True},MODEL,[{'role':'user','content':'test'}],[],'k',10,httpx.MockTransport(lambda request:httpx.Response(429))))
    assert error.value.retryable

def test_timeout_is_not_blindly_retried():
    def handler(request):raise httpx.ReadTimeout('uncertain',request=request)
    with pytest.raises(ProviderError) as error:
        asyncio.run(complete({'protocol':'openai_chat','base_url':'https://provider.example/v1','external':True},MODEL,[{'role':'user','content':'test'}],[],'k',10,httpx.MockTransport(handler)))
    assert not error.value.retryable

@pytest.mark.parametrize('address',['8.8.8.8','169.254.169.254','0.0.0.0','224.0.0.1'])
def test_private_connection_rejects_public_metadata_unspecified_and_multicast(monkeypatch,address):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *args:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,80))])
    with pytest.raises(ProviderError):asyncio.run(validate_private_target('http://anything/v1'))

def test_sandbox_command_has_fixed_security_flags_and_derived_mount():
    run='a'*32;key='task1'
    root=settings.data_root/'runs'/run/'tasks'/key
    (root/'.git').mkdir(parents=True)
    c=Command(run_id=run,task_key=key,operation_id='b'*64,image='agent-forge/sandbox:local',command='python -m unittest',cpu=2,memory_mb=1024)
    args=docker_args(c)
    for flag in ['--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=128','--runtime=runsc','--pull=never']:
        assert flag in args
    assert '--privileged' not in args and 'docker.sock' not in ' '.join(args)
    assert any('target=/workspace/.git,readonly' in a for a in args)
    with pytest.raises(ValueError):docker_args(c.model_copy(update={'image':'unapproved/image'}))
