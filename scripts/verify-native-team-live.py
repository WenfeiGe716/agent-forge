"""Small opt-in REAL model/tool validation, against the user's local platform.

Never auto-approve generated plans or unknown tools. Creating a run may incur
model fees. Existing agents, bindings, provider settings and source files remain
unchanged. Secrets are read locally and never included in the printed report.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'retry', 'inspect', 'approve-plan', 'approve-tool', 'accept'])
    parser.add_argument('--run-id', default='')
    parser.add_argument('--installation-id', default='')
    args = parser.parse_args()
    config = json.loads((ROOT / '.local/config.json').read_text())
    with httpx.Client(base_url='http://127.0.0.1:8787/api', headers={'X-Forge-Request': '1'}, timeout=30, trust_env=False) as client:
        def request(method, path, body=None):
            response = client.request(method, path, json=body)
            if response.status_code >= 300:
                raise RuntimeError(f'{method} {path}: HTTP {response.status_code}: {response.text[:500]}')
            return response.json()
        request('POST', '/auth/login', {'username': 'admin', 'password': config['ADMIN_PASSWORD']})
        rid=args.run_id
        if args.action == 'create':
            models=request('GET','/models')
            model=next((m for m in models if m['enabled'] and m['supports_tools'] and m['model_id']=='bailian/glm-5.2'), None)
            if not model: raise RuntimeError('指定的启用模型 bailian/glm-5.2 不可用；不会自动换供应商或型号')
            provider=next(p for p in request('GET','/providers') if p['id']==model['provider_id'])
            # A legacy profile may retain Chat Completions parameters after a
            # provider moved to Responses. Keep that user-owned profile intact.
            if provider['protocol']=='openai_responses' and 'max_tokens' in model['parameters']:
                model=request('POST','/models',{'name':'本机验收 · bailian/glm-5.2 · Responses','provider_id':provider['id'],
                    'model_id':model['model_id'],'supports_tools':True,'allowed_parameters':['max_output_tokens'],
                    'parameters':{'max_output_tokens':4096}})
            source=Path(tempfile.mkdtemp(prefix='native-team-check-',dir=config['IMPORT_ROOT']))
            (source/'tests').mkdir()
            (source/'normalize.py').write_text('def normalize_title(text):\n    return text\n')
            (source/'tests/test_contract.py').write_text('''import unittest
from normalize import normalize_title
class Contract(unittest.TestCase):
    def test_normalize(self): self.assertEqual(normalize_title("  hello\\t world \\n"), "hello world")
    def test_empty(self): self.assertEqual(normalize_title("\\t\\n  "), "")
    def test_unicode(self): self.assertEqual(normalize_title("  需求   文档 "), "需求 文档")
    def test_type(self):
        with self.assertRaises(TypeError): normalize_title(123)
if __name__ == '__main__': unittest.main()
''')
            (source/'README.md').write_text('# 本机执行验证项目\n\n仅包含公开的字符串规范化需求，无私人数据。\n')
            members=[]
            for name,role,job,instructions in [
                ('验证负责人','planner','任务协调员','只规划两个依赖子任务：先需求文档，后代码实现；不要重复平台的验收或审查阶段。保持任务精简。'),
                ('验证需求师','developer','需求分析师','负责真实需求文档。完成后必须send_message通知实现工程师关键约束。仅写docs/requirements.md，不修改代码。申请并使用固定离线forge-text-tools@1.0.0统计文档，等待审批后再安装调用；不要联网安装。'),
                ('验证工程师','developer','代码研发工程师','必须先读需求文档和已收到的成员消息，再开发normalize.py，运行现有测试。完成后send_message通知验收审查员代码路径和真实测试结果。'),
                ('验证审查员','reviewer','成果验收审查员','独立检查需求文档、源代码、成员消息和平台真实验收结果。仅审查本次字符串规范化目标，不增加无关要求。'),
            ]:
                members.append(request('POST','/agents',{'name':name+' '+source.name[-6:],'role':role,'job_title':job,'department':'本机执行验证',
                    'model_profile_id':model['id'],'instructions':instructions,'runtime':{'max_steps':16,'call_timeout_s':120}}))
            project=request('POST','/projects',{'name':'本机执行闭环验证 '+source.name[-6:],'import_path':source.name,
                'execution_mode':'local','permission_profile':'ask','allow_external_models':True,
                'check_commands':['python3 -m unittest discover -s tests -v'],
                'instructions':'这是一次明确授权的最小本机执行验收。保留既有tests/test_contract.py，不删除或弱化验收。仅处理字符串规范化，不使用真实业务数据。',
                'runtime':{'max_model_calls':45,'max_tasks':2,'max_parallel':1,'command_timeout_s':20,'max_rework_rounds':1}})
            body={'project_id':project['id'],'title':'需求文档→代码→测试→成员消息闭环',
                'request':'实现 normalize_title(text)：参数必须是字符串，否则抛出TypeError；去掉首尾空白，将连续任意空白合成一个ASCII空格，保留中文。先由需求分析师写真实docs/requirements.md并用send_message交接给代码研发工程师，再实现normalize.py并运行python3 -m unittest discover -s tests -v，完成后用send_message通知审查员真实结果。需求师需申请、经人工审批安装并调用内置固定离线forge-text-tools@1.0.0统计文档。这些都是实际工具操作，不能仅在文本描述完成。两个依赖子任务即可；平台负责额外自动验收和最终审查。',
                'agent_ids':[a['id'] for a in members]}
            preflight=request('POST','/execution/preflight',body)
            if not preflight['ready']:
                print(json.dumps({'created_project_id':project['id'],'created_agents':[a['id'] for a in members],'preflight':preflight},ensure_ascii=False,indent=2))
                raise RuntimeError('执行预检未通过，未创建运行，保留验证配置供检查')
            result=request('POST','/runs',body);rid=result['id']
            print(json.dumps({'run_id':rid,'project_id':project['id'],'agent_ids':[a['id'] for a in members],'model':model['model_id'],'permission_profile':'ask','source':str(source)},ensure_ascii=False,indent=2))
            return
        if not rid: raise RuntimeError('必须提供明确的 --run-id')
        run=request('GET','/runs/'+rid)
        if not run['title'].startswith('需求文档→代码→测试→成员消息闭环'): raise RuntimeError('目标不是此验证任务')
        if args.action == 'retry':
            if run['status']!='needs_attention': raise RuntimeError('只为已停止且需要处理的验证任务创建新尝试')
            body={'project_id':run['project_id'],'title':'需求文档→代码→测试→成员消息闭环 · 重试',
                'request':run['request'],'agent_ids':list(run['snapshot']['agents'])}
            preflight=request('POST','/execution/preflight',body)
            if not preflight['ready']:
                print(json.dumps({'ready':False,'runner':preflight.get('runner'),'errors':[item for item in preflight.get('checks',[]) if item.get('status')=='error']},ensure_ascii=False,indent=2))
                raise RuntimeError('配置预检失败，未创建重试运行')
            result=request('POST','/runs',body)
            print(json.dumps({'run_id':result['id'],'previous_run_id':rid,'project_id':run['project_id']},ensure_ascii=False))
            return
        if args.action == 'approve-plan':
            if run['status']!='awaiting_approval': raise RuntimeError('运行不在计划审批阶段')
            print(json.dumps(request('POST',f'/runs/{rid}/decision/plan',{'approved':True,'digest':run['plan_digest'],'note':'已人工核对两个依赖子任务，仅限公开字符串规范化示例。'}),ensure_ascii=False))
            return
        if args.action == 'approve-tool':
            items=request('GET','/tool-installations?run_id='+rid)['items']
            item=next(x for x in items if x['id']==args.installation_id)
            trusted=request('GET','/tool-packages')['items'][0]
            if item['manifest']!=trusted or trusted['id']!='forge-text-tools' or trusted['version']!='1.0.0':
                raise RuntimeError('申请不是预期的固定内置来源，不能批准')
            print(json.dumps(request('POST','/tool-installations/'+item['id']+'/review',{'approved':True,'digest':item['digest'],'comment':'已核对内置源码和固定摘要，仅批准当前验证任务的离线安装。'}),ensure_ascii=False))
            return
        if args.action == 'accept':
            if run['status']!='awaiting_acceptance': raise RuntimeError('运行不在验收阶段')
            print(json.dumps(request('POST',f'/runs/{rid}/decision/acceptance',{'approved':True,'digest':run['artifact_digest'],'note':'已核对真实文档、代码、原生命令和成员消息证据。'}),ensure_ascii=False))
            return
        events=request('GET',f'/runs/{rid}/events?limit=1000')['events']
        counts={kind:sum(e['kind']==kind for e in events) for kind in sorted({e['kind'] for e in events})}
        installs=request('GET','/tool-installations?run_id='+rid)['items']
        folder=Path(config['DATA_ROOT'])/'runs'/rid/'integration'
        artifacts={}
        for relative in ('docs/requirements.md','normalize.py','tests/test_contract.py'):
            file=folder/relative
            if file.is_file(): artifacts[relative]={'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'content':file.read_text()[:5000]}
        print(json.dumps({'run_id':rid,'status':run['status'],'error':run.get('error'),'calls':run.get('calls'),
            'plan':run.get('plan'),'event_counts':counts,'tasks':[{k:t.get(k) for k in ('key','kind','status','title','result')} for t in run.get('tasks',[])],
            'installations':[{k:x[k] for k in ('id','status','digest','reason')} for x in installs],
            'evidence_events':[e for e in events if e['kind'] in ('execution.native.completed','check.completed','agent.message','agent.message.delivered','tool.installed.invoked')],
            'workspace':str(folder),'artifacts':artifacts},ensure_ascii=False,indent=2))


if __name__=='__main__': main()
