"""Explicit CPU-only permanent deployment. Imports never deploy or call models."""
from hashlib import sha256
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]
REMOTE_ROOT='/opt/istok'
APP_NAME='istok-web-beta'
VOLUME_NAME='istok-web-beta-data'
ORIGIN='https://zhanlaiman--istok-beta.modal.run'
WEB_FILES=('index.html','style.css','app.js','motion.css','motion.js','fonts/InterVariable.woff2',
           'fonts/Inter-LICENSE.txt','fonts/PTSerif-Regular.ttf','fonts/PTSerif-OFL.txt')


def source_manifest(root=ROOT):
    root=Path(root).resolve()
    files=['server.py','cloud/web_beta.py','cloud/modal_web.py',
        *('web/'+name for name in WEB_FILES),
        'data/source-packs/biology-ru-v1.json','data/source-packs/economics-ru-v1.json',
        *(str(path.relative_to(root)) for path in sorted((root/'plagiarism').glob('*.py')))]
    result=[]
    for name in files:
        path=root/name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('Only ordinary files in the deployment allowlist may be uploaded')
        body=path.read_bytes();result.append({'path':name,'sha256':sha256(body).hexdigest(),'bytes':len(body)})
    return result


def stage_source(root=ROOT):
    root=Path(root);manifest=source_manifest(root)
    identity=sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
    directory=root/'.runtime/web-deploy'/identity
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    for item in manifest:
        target=directory/item['path'];target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():shutil.copyfile(root/item['path'],target)
        if sha256(target.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('Staged source differs')
    return directory,manifest


def serve_beta():
    import os,sys
    from threading import Thread
    from pathlib import Path
    import modal
    from cloud.web_beta import create_server
    os.environ.update(ISTOK_PDF_ALTERNATIVE_ENGINE='pdfium',ISTOK_PDFIUM_PYTHON=sys.executable)
    volume=modal.Volume.from_name('istok-web-beta-data')
    service=create_server(Path('/state'),'https://zhanlaiman--istok-beta.modal.run',persist=volume.commit)
    Thread(target=service.serve_forever,name='istok-http',daemon=True).start()


def build_app(directory,key):
    import modal
    volume=modal.Volume.from_name(VOLUME_NAME,create_if_missing=True,version=2)
    image=(modal.Image.debian_slim(python_version='3.14')
        .pip_install('pypdf==7.0.1','pypdfium2==5.14.0')
        .add_local_dir(directory,REMOTE_ROOT,copy=True)
        .env({'PYTHONPATH':REMOTE_ROOT,'ISTOK_SEARCH_PROVIDER':'tavily','ISTOK_MODEL_PROVIDER':'local'}))
    secret=modal.Secret.from_dict({'ISTOK_TAVILY_API_KEY':key})
    app=modal.App(APP_NAME,include_source=False)
    web=app.function(name='web',image=image,secrets=[secret],volumes={'/state':volume},
        cpu=0.25,memory=512,min_containers=0,max_containers=1,buffer_containers=0,
        scaledown_window=360,timeout=600,include_source=False,serialized=True)(
            modal.web_server(8000,label='istok-beta',startup_timeout=60)(serve_beta))
    return app,web


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--deploy',action='store_true')
    args=parser.parse_args();directory,manifest=stage_source()
    if not args.deploy:
        print(json.dumps({'app':APP_NAME,'origin':ORIGIN,'files':manifest,'CPU_only':True,'GPU':None,
            'min_containers':0,'max_containers':1,'private_data_uploaded':False},ensure_ascii=False,indent=2));return
    from plagiarism.search_credentials import key_for
    key=key_for('tavily')
    if not key:raise SystemExit('Tavily is not configured; no deployment')
    import modal
    with modal.enable_output():
        app,web=build_app(directory,key);app.deploy(environment_name='main',strategy='recreate')
    receipt={'app':APP_NAME,'url':web.web_url,'source_manifest':manifest,'CPU_only':True,
             'GPU':None,'private_data_uploaded':False,'volume':VOLUME_NAME}
    path=ROOT/'.runtime/web-deploy/deployment.json';path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)
    print(json.dumps({'app':APP_NAME,'url':web.web_url,'volume':VOLUME_NAME},ensure_ascii=False))


if __name__=='__main__':main()
