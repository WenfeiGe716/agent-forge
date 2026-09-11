import os
import tempfile
from pathlib import Path
from cryptography.fernet import Fernet

TEMP = tempfile.TemporaryDirectory(prefix='forge-tests-')
ROOT = Path(TEMP.name)
os.environ.update({'DATABASE_URL':'sqlite:///'+str(ROOT/'test.db'),'DATA_ROOT':str(ROOT/'data'),
                   'IMPORT_ROOT':str(ROOT/'imports'),'ENCRYPTION_KEY':Fernet.generate_key().decode(),
                   'ADMIN_PASSWORD':'test-password-only-12345','SANDBOX_TOKEN':'test-broker-token-not-a-real-secret',
                   'DISABLE_DISPATCH':'true', 'FORGE_BOOTSTRAP_DEFAULT_ORGANIZATION':'false'})
import pytest
from fastapi.testclient import TestClient
from forge.db import Base, engine
from forge.api import app, login_attempts
from forge.config import settings
import shutil


def seat_agents(client, agent_ids=None):
    """Explicitly publish/apply real test desks; never bypass the run gate.

    Tests that add or relocate members call this helper again deliberately.
    Existing departments are preserved, with unassigned fixture members placed
    in one test R&D department. No production API auto-seats new members.
    """
    agents = [a for a in client.get('/api/agents').json()
              if a.get('enabled', True) and (agent_ids is None or a['id'] in agent_ids)]
    departments = {}
    for agent in agents:
        departments.setdefault(agent.get('department') or '研发部', []).append(agent)
    floors, teams, desks, positions = [], [], [], {}
    assert len(departments) <= 10, 'Fixture needs an explicit larger organization design'
    for floor, (department, members) in enumerate(departments.items(), 1):
        assert len(members) <= 21, 'Fixture department exceeds its deliberately configured desks'
        team, zone = f'fixture_team_{floor}', f'fixture_work_{floor}'
        floors.append({'floor': floor, 'name': department + '办公层', 'department': department, 'zones': [
            {'id': zone, 'name': '测试工作区', 'kind': 'work', 'x': 0, 'z': -2.5, 'width': 22, 'depth': 9},
            {'id': f'fixture_meeting_{floor}', 'name': '会议室', 'kind': 'meeting', 'x': -6, 'z': 5.25, 'width': 10, 'depth': 4.5},
            {'id': f'fixture_restroom_{floor}', 'name': '卫生间', 'kind': 'restroom', 'x': 2, 'z': 5.25, 'width': 4, 'depth': 4.5},
            {'id': f'fixture_pantry_{floor}', 'name': '饮水间', 'kind': 'pantry', 'x': 7, 'z': 5.25, 'width': 4, 'depth': 4.5}]})
        teams.append({'id': team, 'name': department + '测试团队', 'floor': floor, 'zone_ids': [zone],
                      'headcount': len(members), 'member_ids': [a['id'] for a in members]})
        for index, agent in enumerate(members):
            position = {'x': -9 + (index % 7) * 3, 'z': -6 + (index // 7) * 3, 'floor': floor, 'zone': zone}
            positions[agent['id']] = position
            desks.append({'id': f'fixture_desk_{floor}_{index}', 'name': agent['name'] + '测试工位',
                          **position, 'team_id': team, 'agent_id': agent['id']})
    config = {'layout_version': 2, 'description': 'Explicit fixture organization for new-run readiness',
              'floors': floors, 'teams': teams, 'desks': desks, 'positions': positions}
    response = client.post('/api/design/assets', json={'kind': 'office', 'name': '测试工位配置', 'config': config})
    assert response.status_code == 201, response.text
    asset = response.json()
    confirmation = {key: asset[key] for key in ('version', 'digest')}
    for action in ('publish', 'apply-office'):
        response = client.post(f'/api/design/assets/{asset["id"]}/{action}', json={**confirmation, 'comment': 'Explicit isolated test desk assignment'})
        assert response.status_code == 200, response.text
    return client.get('/api/organization').json()

@pytest.fixture(autouse=True)
def reset_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    login_attempts.clear()
    for path in [settings.data_root,settings.import_root]:
        shutil.rmtree(path,ignore_errors=True);path.mkdir(parents=True)
    yield

@pytest.fixture
def client():
    with TestClient(app,headers={'X-Forge-Request':'1'}) as c:
        response=c.post('/api/auth/login',json={'username':'admin','password':settings.admin_password})
        assert response.status_code==200,response.text
        yield c

@pytest.fixture
def configured_unseated(client):
    source=settings.import_root/'tiny'
    (source/'tests').mkdir(parents=True)
    (source/'calc.py').write_text('def add(a, b):\n    return a - b\n')
    (source/'tests'/'test_calc.py').write_text('import unittest\nfrom calc import add\nclass TestCalc(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2,3),5)\n')
    (source/'.env').write_text('DO_NOT_IMPORT=top-secret\n')
    provider_ids=[]; model_ids=[]
    for i in range(2):
        p=client.post('/api/providers',json={'name':f'Private {i}','protocol':'openai_chat','base_url':f'http://model-{i}.internal/v1','external':False,'api_key':f'secret-key-{i}'})
        assert p.status_code==201,p.text
        provider_ids.append(p.json()['id'])
        m=client.post('/api/models',json={'name':f'Model {i}','provider_id':p.json()['id'],'model_id':f'configurable-{i}','allowed_parameters':['max_tokens','temperature'],'parameters':{'max_tokens':2048}})
        assert m.status_code==201,m.text
        model_ids.append(m.json()['id'])
    agents=client.get('/api/agents').json()
    for i,a in enumerate(agents):
        body={k:v for k,v in a.items() if k not in ('id','version')}
        body['model_profile_id']=model_ids[i%2]
        r=client.put('/api/agents/'+a['id'],json=body)
        assert r.status_code==200,r.text
    project=client.post('/api/projects',json={'name':'Tiny project','import_path':'tiny','execution_mode':'container','check_commands':['python -m unittest discover -s tests']})
    assert project.status_code==201,project.text
    return {'source':source,'providers':provider_ids,'models':model_ids,'agents':client.get('/api/agents').json(),'project':project.json()}


@pytest.fixture
def configured(client, configured_unseated):
    seat_agents(client)
    return {**configured_unseated, 'agents': client.get('/api/agents').json()}

def run_body(configured,**overrides):
    selected=overrides.get('agent_ids',[a['id'] for a in configured['agents']])
    known=[a for a in configured['agents'] if a['id'] in selected]
    # Explicit owner of the standard four-person test team, not production role
    # inference. Published instances use their own configured reporting leader.
    leader=next((a for a in known if a['role']=='planner'),known[0] if len(known)==1 else None)
    return {'execution_strategy':'autonomous','entry_agent_id':leader['id'] if leader else None,'project_id':configured['project']['id'],'title':'Fix addition','request':'Fix add while preserving its signature. Existing tests must pass.',
            'agent_ids':[a['id'] for a in configured['agents']],**overrides}
