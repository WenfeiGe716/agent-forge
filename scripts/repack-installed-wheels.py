"""Maintainer-only: reconstruct wheels from installed distributions for air-gapped verification.
Not upstream wheel artifacts. Every generated wheel has a new SHA256 and explicit provenance.
Never collects credentials, fonts, unrelated packages, or files outside site-packages.
"""
import base64, csv, hashlib, importlib.metadata as md, io, json, re, sys, zipfile
from pathlib import Path
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'offline'/'wheelhouse';OUT.mkdir(parents=True,exist_ok=True)
roots=['fastapi','uvicorn','sqlalchemy','pydantic','httpx','cryptography','pytest','pytest-asyncio']
seen={}; pending=list(roots)
while pending:
    name=pending.pop();key=canonicalize_name(name)
    if key in seen:continue
    dist=md.distribution(name);seen[key]=dist
    for raw in dist.requires or []:
        req=Requirement(raw)
        if not req.marker or req.marker.evaluate({'extra':''}):
            installed=md.version(req.name)
            if req.specifier and installed not in req.specifier:raise RuntimeError(f'Installed dependency mismatch: {raw}: {installed}')
            pending.append(req.name)
manifest=[];lock=[]
for name,dist in sorted(seen.items()):
    files=dist.files or []
    wheel_path=next(f for f in files if str(f).endswith('.dist-info/WHEEL'))
    distinfo=str(wheel_path).rsplit('/',1)[0]
    wheel_text=dist.locate_file(wheel_path).read_text()
    tag=next(line.split(': ',1)[1] for line in wheel_text.splitlines() if line.startswith('Tag: '))
    filename=f'{name.replace("-","_")}-{dist.version}-{tag}.whl'
    target=OUT/filename
    records=[]
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for f in sorted(files,key=str):
            path=Path(str(f))
            if path.is_absolute() or '..' in path.parts or '__pycache__' in path.parts or path.suffix=='.pyc':continue
            if path.name in ('RECORD','direct_url.json','INSTALLER','REQUESTED'):continue
            source=Path(dist.locate_file(f))
            if not source.is_file() or source.is_symlink():continue
            data=source.read_bytes();z.writestr(str(f),data)
            digest=base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
            records.append((str(f),'sha256='+digest,str(len(data))))
        info={'source':'reconstructed from preinstalled distribution files; NOT upstream wheel bytes','name':dist.metadata['Name'],'version':dist.version,'license':dist.metadata.get('License-Expression') or dist.metadata.get('License',''),'original_WHEEL':wheel_text}
        data=json.dumps(info,ensure_ascii=False,indent=2).encode();rp=distinfo+'/FORGE_REPACK_PROVENANCE.json';z.writestr(rp,data)
        records.append((rp,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode(),str(len(data))))
        record=distinfo+'/RECORD';records.append((record,'',''));buf=io.StringIO();csv.writer(buf,lineterminator='\n').writerows(records);z.writestr(record,buf.getvalue())
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    manifest.append({'name':name,'version':dist.version,'file':filename,'sha256':digest,'files':len(records),'source':'installed distribution repack; upstream download unavailable'})
    lock.append(f'{name}=={dist.version} --hash=sha256:{digest}')
(ROOT/'offline'/'requirements-linux-cp313.lock').write_text('\n'.join(lock)+'\n')
(ROOT/'offline'/'MANIFEST.json').write_text(json.dumps({'platform':sys.platform,'python':sys.version,'provenance':'Repacked installed distributions; not downloaded upstream artifacts. Licenses and package metadata are retained. No Temporal, psycopg, Docker or model weights included.','packages':manifest},ensure_ascii=False,indent=2))
print(json.dumps({'wheels':len(manifest),'bytes':sum(p.stat().st_size for p in OUT.iterdir())},indent=2))
