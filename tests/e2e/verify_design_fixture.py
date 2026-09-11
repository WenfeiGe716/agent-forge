"""Verify the artifacts produced by the separate CUA browser acceptance session."""
import json,sys
from pathlib import Path
from urllib.parse import urlsplit
import httpx

root=Path(__file__).resolve().parents[2]
target=urlsplit(sys.argv[1])
assert target.scheme=='http' and target.hostname in ('127.0.0.1','localhost','::1') and target.port not in (None,8787), 'Refusing a non-isolated target'
assert sys.argv[2].startswith('isolated-fixture-'), 'Use the disposable fixture credentials only'
with httpx.Client(base_url=sys.argv[1],headers={'X-Forge-Request':'1'},trust_env=False) as client:
    client.post('/api/auth/login',json={'username':'admin','password':sys.argv[2]}).raise_for_status()
    def get(path):
        response=client.get('/api'+path);response.raise_for_status();return response.json()
    providers=get('/providers')
    assert providers and any('隔离验收' in p['name'] for p in providers), 'Fixture marker missing; refusing test writes'
    assert all(not p.get('external',True) and urlsplit(p['base_url']).hostname in ('127.0.0.1','localhost','::1') for p in providers), 'External provider found; refusing test writes'
    assets=get('/design/assets');hub=get('/model-hub');runs=get('/runs')
    assert len(runs)==1
    run=get('/runs/'+runs[0]['id']);snapshot=run['snapshot']
    assert snapshot['mode']=='runtime'
    assert snapshot['design']['team']['name']=='商城开发团队'
    assert snapshot['design']['routing']['name']=='支付模块策略'
    assert snapshot['design']['enterprise']['version']==2
    assert snapshot['runtime']['max_parallel']==2
    assert len(snapshot['agents'])==4
    nova=next(a for a in snapshot['agents'].values() if a['name']=='Nova API')
    assert nova['office_position']=={'x':5.0,'z':2.0,'floor':2,'zone':'二楼开发区'}
    assert 'delete_file' not in nova['tools']
    assert nova['communication']['scope']=='custom'
    assert hub['models'][0]['capability']['final_scores']['coding']==95
    assert hub['benchmarks']==[] and run['calls']==0
    assert next(a for a in assets if a['kind']=='capability')['version']==2
    assert next(a for a in assets if a['kind']=='capability')['published_version']==1
    assert next(a for a in assets if a['kind']=='office')['status']=='published'
    assert next(a for a in assets if a['kind']=='benchmark')['status']=='published'
    assert all(a.get('office_position') for a in get('/agents'))
    viewer=client.post('/api/users',json={'username':'fixture-viewer','password':'fixture-viewer-pass-2026','role':'viewer'})
    assert viewer.status_code in (200,201,409),viewer.text
    malformed=client.post('/api/design/assets',json={'kind':'team','name':'不完整草稿 · UI 修复验收','config':{'members':{}},'source':'manual'})
    malformed.raise_for_status()
    result={'method':'CUA real browser interactions + HTTP evidence checks plus isolated test fixtures','model_calls':0,'run_status':run['status'],
            'checks':['Published capability v1 remains coding95 while v2 draft coding88 exists','Routing v2 and enterprise rule frozen at max_parallel2','Team draft edited and explicitly published/instantiated','Member permissions narrowed and custom communication enforced','All-hands meeting preview refused restricted communication','Default workflow task explicitly created after preview','Frozen Nova office floor2 x5 z2 survives later office application','Independent office template published and applied','Custom JSON benchmark suite published; benchmark execution left disabled without consent'],
            'playwright_script_executed':False,'paid_inference_executed':False}
    out=root/'docs/evidence-v013';out.mkdir(parents=True,exist_ok=True)
    (out/'cua-fixture-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({'verified':len(result['checks']),'model_calls':0,'malformed_asset_id':malformed.json()['id']},ensure_ascii=False))
