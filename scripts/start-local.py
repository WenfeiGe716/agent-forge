"""Start the actual local platform. Config and keys are generated once.
Local execution is the default for new projects; project permissions remain explicit.
Only --with-container-broker provisions the optional Docker/runsc execution broker.
No fixture or silent execution-mode fallback. Existing deployments and credentials survive.
"""
import argparse,base64,json,os,secrets,sys,subprocess,signal,shutil,socket,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def docker_ready():
    if not shutil.which('docker'):return False,'未安装Docker；可先接入模型并进行规划与讨论。'
    try:
        result=subprocess.run(['docker','info','--format','{{json .Runtimes}}'],capture_output=True,text=True,timeout=8)
        if result.returncode:return False,'Docker服务不可访问；未启动代码执行服务。'
        if 'runsc' not in json.loads(result.stdout):return False,'Docker未注册runsc；不降级为不受限执行。'
        return True,''
    except (OSError,ValueError,subprocess.TimeoutExpired):return False,'无法确认Docker/runsc状态。'

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',default=8787,type=int)
    p.add_argument('--no-auto-sandbox',action='store_true',help='Use a separately managed broker; do not provision one.')
    p.add_argument('--with-container-broker',action='store_true',help='Explicitly provision the optional Docker/runsc broker; never required for local execution.')
    p.add_argument('--no-build',action='store_true',help='Do not fetch/build missing execution images.')
    args=p.parse_args()
    if not 1<=args.port<=65535:raise SystemExit('端口必须在1–65535之间')
    # Probe early; never display existing secrets or start brokers if web port is occupied.
    try:
        with socket.socket() as check:
            check.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);check.bind((args.host,args.port))
    except OSError:raise SystemExit('Web端口已占用，请使用原服务或指定其他端口；不会停止已有进程。')
    local=ROOT/'.local';local.mkdir(mode=0o700,exist_ok=True);config=local/'config.json'
    first=not config.exists()
    if first:
        values={'DATABASE_URL':'sqlite:///'+str(local/'forge.db'),'DATA_ROOT':str(local/'data'),'IMPORT_ROOT':str(ROOT/'imports'),
                'ENCRYPTION_KEY':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),'ADMIN_PASSWORD':secrets.token_urlsafe(24),
                'SANDBOX_TOKEN':secrets.token_urlsafe(36),'SANDBOX_URL':'http://127.0.0.1:8091','MAX_ACTIVE_RUNS':'2','MAX_ACTIVE_ACTIVITIES':'4',
                'ALLOWED_ORIGINS':f'http://localhost:{args.port},http://127.0.0.1:{args.port}','SECURE_COOKIE':'false'}
        with os.fdopen(os.open(config,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w') as f:json.dump(values,f,indent=2)
    values=json.loads(config.read_text());Path(values['IMPORT_ROOT']).mkdir(parents=True,exist_ok=True)
    if not (ROOT/'frontend/dist/index.html').is_file():raise SystemExit('缺少正式页面；请运行 node scripts/build-offline.mjs')
    python=ROOT/'.venv/bin/python'
    if not python.is_file():raise SystemExit('运行 bash start.sh 自动安装本平台离线依赖。')
    env={**os.environ,**{k:str(v) for k,v in values.items()},'PYTHONPATH':str(ROOT/'backend'),'ORCHESTRATOR':'local','FORGE_VALIDATION_MODE':'false','DISABLE_DISPATCH':'false','FRONTEND_DIR':str(ROOT/'frontend/dist')}
    # A network-hosted web deployment is not automatically a trusted local client.
    env['FORGE_LOCAL_EXECUTION']='true' if args.host in ('127.0.0.1','localhost','::1') else 'false'
    # Changing the local port does not require hand-editing JSON origins.
    origins=set(env.get('ALLOWED_ORIGINS','').split(','));origins.update({f'http://localhost:{args.port}',f'http://127.0.0.1:{args.port}'})
    env['ALLOWED_ORIGINS']=','.join(sorted(origins))
    children=[];broker=None
    if args.with_container_broker and not args.no_auto_sandbox and values.get('SANDBOX_URL')=='http://127.0.0.1:8091':
        ok,reason=docker_ready()
        if ok:
            image='agent-forge/sandbox:local'
            inspect=subprocess.run(['docker','image','inspect',image],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if inspect.returncode and not args.no_build:
                print('准备默认执行镜像（首次需可信软件源；不改变项目类型支持范围）…',flush=True)
                build=subprocess.run(['docker','build','-f',str(ROOT/'infra/Dockerfile.sandbox'),'-t',image,str(ROOT)],env=env)
                ok=build.returncode==0
                if not ok:reason='默认镜像构建未成功；环境状态页显示缺项，不假装测试通过。'
            elif inspect.returncode:ok=False;reason='缺少执行镜像，已按参数跳过构建。'
            if ok:
                try:
                    with socket.socket() as check:
                        check.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);check.bind(('127.0.0.1',8091))
                except OSError:print('执行服务端口已占用，将由工作台检查其身份、凭据及隔离能力。',flush=True)
                else:
                    benv={**env,'HOST_DATA_ROOT':values['DATA_ROOT'],'SANDBOX_RUNTIME':'runsc','SANDBOX_UID':str(os.getuid()),'SANDBOX_GID':str(os.getgid())}
                    log=open(local/'broker.log','a')
                    broker=subprocess.Popen([str(python),'-m','uvicorn','forge.sandbox:app','--host','127.0.0.1','--port','8091'],env=benv,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
                    children.append(broker)
        if not ok:print(reason,flush=True)
    print(f'Agent Forge v0.14.0 · http://{args.host}:{args.port}\n管理员账号：admin',flush=True)
    if first:print('首次登录密码（仅此终端显示一次）：'+values['ADMIN_PASSWORD'],flush=True)
    else:print('使用原管理员密码；已保存的供应商、团队和Skill不会重置。',flush=True)
    print('登录后可在“供应商与模型”添加多个连接，并为各Agent独立绑定。导入项目后下达需求。',flush=True)
    server=subprocess.Popen([str(python),'-m','uvicorn','forge.api:app','--host',args.host,'--port',str(args.port)],env=env,cwd=ROOT);children.insert(0,server)
    def stop(sig=None,frame=None):
        for proc in children:
            if proc.poll() is None:proc.terminate()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:return server.wait()
    finally:
        stop()
        for proc in children:
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        if broker:log.close()
if __name__=='__main__':sys.exit(main())
