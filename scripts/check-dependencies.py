"""Read-only dependency diagnostics. No DNS, proxy, browser policy, or TLS changes."""
import importlib.metadata as md,json,platform,shutil,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
result={'python':sys.version,'platform':platform.platform(),'tools':{x:shutil.which(x) for x in ['node','npm','docker','runsc','chromium']},'packages':{},'network':{},'offline':{}}
for name in ['fastapi','uvicorn','sqlalchemy','pydantic','httpx','cryptography','temporalio','psycopg']:
    try:result['packages'][name]=md.version(name)
    except md.PackageNotFoundError:result['packages'][name]=None
for name,url in [('npm','https://registry.npmjs.org/react'),('pypi','https://pypi.org/simple/temporalio/')]:
    try:
        r=subprocess.run(['curl','-I','-sS','--connect-timeout','3','--max-time','5',url],capture_output=True,text=True,timeout=7)
        result['network'][name]={'returncode':r.returncode,'stdout':r.stdout[-800:],'stderr':r.stderr[-800:]}
    except Exception as e:result['network'][name]={'error':str(e)}
result['offline']={'wheel_count':len(list((ROOT/'offline/wheelhouse').glob('*.whl'))),'frontend_built':(ROOT/'frontend/dist/index.html').is_file(),'temporal_and_docker_included':False}
print(json.dumps(result,ensure_ascii=False,indent=2))
