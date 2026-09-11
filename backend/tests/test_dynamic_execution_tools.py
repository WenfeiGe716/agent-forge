import copy
import pytest

from forge import engine,tools
from forge.db import Run,session
from forge.execution_tools import inspect_environment,discovery_command
from conftest import run_body


@pytest.mark.parametrize('names', [['python3;touch'],['$(uname)'],['../tool'],['tool','tool'],[],['x'*81]])
def test_discovery_arguments_cannot_become_commands(names):
    with pytest.raises(ValueError): discovery_command(names,'/workspace')


@pytest.mark.asyncio
async def test_arbitrary_project_specific_tool_names_and_missing_tools_are_reported(client,configured,monkeypatch):
    run=client.post('/api/runs',json=run_body(configured)).json();rid=run['id']
    engine.prepare_run({'run_id':rid})
    aid=next(a['id'] for a in configured['agents'] if a['role']=='developer')
    engine.create_task(rid,'build','develop',aid,'Build',{})
    seen=[]
    async def execute(run_id,key,operation,command,runtime,image):
        seen.append(command)
        assert 'command -v domain-specific-compiler' in command
        assert '--version' not in command
        return {'exit_code':0,'output':'domain-specific-compiler\t/workspace/node_modules/.bin/domain-specific-compiler\nnot-installed\t\n'}
    monkeypatch.setattr(tools,'command',execute)
    result=await inspect_environment(rid,'build','inspect',{'executables':['domain-specific-compiler','not-installed']},{},'fixture')
    assert result['executables'][0]['available'] and not result['executables'][1]['available']
    assert not result['installation_performed'] and not result['executables'][0]['version_verified']
    listed=client.get('/api/execution/tools?run_id='+rid)
    assert listed.status_code==200,listed.text
    assert listed.json()['items'][0]['executables']==result['executables']
    assert len(seen)==1


@pytest.mark.asyncio
async def test_read_only_role_cannot_use_command_backed_inventory(client,configured):
    run=client.post('/api/runs',json=run_body(configured)).json();engine.prepare_run({'run_id':run['id']})
    with pytest.raises(ValueError,match='执行成员'):
        await inspect_environment(run['id'],'plan','inspect',{'executables':['python3']},{},'fixture')
