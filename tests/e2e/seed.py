import json,os
from pathlib import Path
import httpx
STATE=Path(os.getenv('FORGE_E2E_STATE','/tmp/agent-forge-e2e'));env=json.loads((STATE/'environment.json').read_text())
c=httpx.Client(base_url='http://127.0.0.1:8787',headers={'X-Forge-Request':'1'},trust_env=False,timeout=30)
c.post('/api/auth/login',json={'username':'admin','password':env['ADMIN_PASSWORD']}).raise_for_status()
models=[]
for name,protocol,path in [('验证连接A / OpenAI协议','openai_chat','openai'),('验证连接B / Anthropic协议','anthropic_messages','anthropic')]:
    r=c.post('/api/providers',json={'name':name,'protocol':protocol,'base_url':f'http://127.0.0.1:8099/{path}/v1','external':False,'api_key':'fixture-no-real-secret'});r.raise_for_status();p=r.json()
    r=c.post('/api/models',json={'name':'脚本化验收模型 / '+path,'provider_id':p['id'],'model_id':'fixture-'+path,'allowed_parameters':['max_tokens'],'parameters':{'max_tokens':2048}});r.raise_for_status();models.append(r.json())
    r=c.post('/api/models/'+models[-1]['id']+'/test');r.raise_for_status();assert r.json()['text']=='SCRIPTED FIXTURE OK'
agents=c.get('/api/agents').json()
for a in agents:
    body={k:v for k,v in a.items() if k not in ('id','version')};body['model_profile_id']=models[0 if a['role'] in ['planner','developer'] else 1]['id'];body['runtime']={'max_steps':12}
    r=c.put('/api/agents/'+a['id'],json=body);r.raise_for_status()
r=c.post('/api/projects',json={'name':'协作验收 / 加法修复','import_path':'fixture-project','check_commands':['python -m unittest discover -s tests'],'runtime':{'max_parallel':2,'max_agent_messages':20}});r.raise_for_status();project=r.json()
setup={'project':project,'agents':c.get('/api/agents').json(),'models':models};(STATE/'setup.json').write_text(json.dumps(setup,ensure_ascii=False,indent=2));print('seeded',project['id'])
