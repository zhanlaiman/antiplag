"""Explicit CPU-only permanent deployment. Imports never deploy or call models."""
from hashlib import sha256
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]
REMOTE_ROOT='/opt/istok'
APP_NAME='istok-web-beta'
VOLUME_NAME='istok-web-beta-data'
ORIGIN='https://zhanlaiman--istok-beta.us-east.modal.run'
WEB_FILES=('favicon.ico','favicon.png','favicon.svg','apple-touch-icon.png','safari-pinned-tab.svg',
           'robots.txt','sitemap.xml','privacy.html','api-guide.html','dictionary-license.txt','rtf-license.txt','owner.html','owner.js','index.html','style.css','app.js','motion.css','motion.js','fonts/InterVariable.woff2',
           'fonts/Inter-LICENSE.txt','fonts/PTSerif-Regular.ttf','fonts/PTSerif-OFL.txt',
           'dashboard.html','dashboard.js','dashboard.css','pool.html','pool.js','pool.css','account.html','account.js','account.css')


def source_manifest(root=ROOT):
    root=Path(root).resolve()
    files=['server.py','cloud/web_beta.py','cloud/modal_web.py','cloud/dashboard.py','cloud/user_auth.py','cloud/pool_runtime.py',
        *('web/'+name for name in WEB_FILES),
        'data/source-packs/biology-ru-v1.json','data/source-packs/economics-ru-v1.json',
        *('data/dictionaries/ru_RU/'+name for name in ('ru_RU.aff','ru_RU.dic','README_ru_RU.txt','manifest.json')),
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
    integrated_value=os.environ.get('ISTOK_INTEGRATED_RELEASE','0')
    if integrated_value not in ('0','1'):raise ValueError('Invalid integrated role flag')
    integrated=integrated_value=='1'
    options={}
    if integrated:
        for name in ('ISTOK_REGISTRATION_ENABLED','ISTOK_DASHBOARD_WRITES_ENABLED'):
            if os.environ.get(name) not in ('0','1'):raise ValueError('Invalid integrated role flag')
        if os.environ.get('ISTOK_AUTOMATIC_MONTHLY_LIMIT') not in ('180','900'):
            raise ValueError('Invalid automatic monthly cap')
        from plagiarism.search_credentials import key_for
        options=dict(integrated=True,
            registration_enabled=os.environ.get('ISTOK_REGISTRATION_ENABLED')=='1',
            business_writes_enabled=os.environ.get('ISTOK_DASHBOARD_WRITES_ENABLED')=='1',
            automatic_monthly_limit=int(os.environ.get('ISTOK_AUTOMATIC_MONTHLY_LIMIT','180')),
            pool_bootstrap=json.loads(os.environ['ISTOK_POOL_BOOTSTRAP']),
            pool_grants=json.loads(os.environ['ISTOK_SEARCH_POOL_GRANTS']),
            pool_keys={p:key_for(p) for p in ('serpapi','tavily','serper','exa')})
    volume=modal.Volume.from_name(VOLUME_NAME)
    # Volume v2 exposes this trusted deployment mount through a symlink.
    state_root=Path('/state').resolve(strict=True)
    service=create_server(state_root,ORIGIN,persist=volume.commit,**options)
    Thread(target=service.serve_forever,name='istok-http',daemon=True).start()


def build_app(directory,key,*,google_key=None,default_search_provider='tavily',
              automatic_search=False,serper_key=None,exa_key=None,pool_grants=None,
              integrated=False,registration_enabled=False,business_writes_enabled=False,
              automatic_monthly_limit=180,dashboard_config=None,pool_bootstrap=None):
    if any(type(flag) is not bool for flag in (integrated,registration_enabled,business_writes_enabled,automatic_search)):
        raise ValueError('Reviewed role flags must be booleans')
    if type(automatic_monthly_limit) is not int or automatic_monthly_limit not in (180,900):
        raise ValueError('Reviewed automatic cap required')
    if not integrated and (registration_enabled or business_writes_enabled or automatic_monthly_limit!=180 or dashboard_config is not None or pool_bootstrap is not None):
        raise ValueError('Integrated settings require integrated mode')
    if integrated:
        from cloud.dashboard import DashboardConfig
        if not automatic_search or not isinstance(dashboard_config,dict) or set(dashboard_config)!={'username','salt','digest'}:
            raise ValueError('Integrated release requires reviewed admin and pool configuration')
        DashboardConfig(**dashboard_config)
        if not isinstance(pool_bootstrap,dict) or len(json.dumps(pool_bootstrap,allow_nan=False))>65536:
            raise ValueError('Bounded bootstrap packet required')
        from cloud.pool_runtime import validate_bootstrap
        validate_bootstrap(pool_bootstrap,{'serpapi':google_key,'tavily':key,'serper':serper_key,'exa':exa_key})
    if default_search_provider not in ('tavily','serpapi','serpapi-yandex') or default_search_provider in ('serpapi','serpapi-yandex') and not google_key:
        raise ValueError('A reviewed search provider and its credential are required')
    if automatic_search:
        from plagiarism.search_pool import validate_grant_policy
        credentials={'serpapi':google_key,'serper':serper_key,'exa':exa_key,'tavily':key}
        for provider,value in credentials.items():
            if not isinstance(value,str) or not value or len(value)>1024 or any(not 33<=ord(c)<=126 for c in value):
                raise ValueError('All four search credentials are required')
        if not isinstance(pool_grants,dict) or set(pool_grants)!={'serper','exa'}:
            raise ValueError('Both bounded free grants are required')
        for provider in ('serper','exa'):
            validate_grant_policy(pool_grants[provider],provider,credentials[provider])
        encoded_grants=json.dumps(pool_grants,sort_keys=True,allow_nan=False)
    import modal
    volume=modal.Volume.from_name(VOLUME_NAME,create_if_missing=True,version=2)
    image=(modal.Image.debian_slim(python_version='3.14')
        .apt_install('hunspell','antiword')
        .pip_install('pypdf==7.0.1','pypdfium2==5.14.0')
        .add_local_dir(directory,REMOTE_ROOT,copy=True)
        .env({'PYTHONPATH':REMOTE_ROOT,'ISTOK_SEARCH_PROVIDER':default_search_provider,'ISTOK_MODEL_PROVIDER':'local'}))
    secret_values={'ISTOK_TAVILY_API_KEY':key}
    if automatic_search:
        secret_values.update(ISTOK_AUTOMATIC_SEARCH='1',ISTOK_SERPER_API_KEY=serper_key,
                             ISTOK_EXA_API_KEY=exa_key,
                              ISTOK_SEARCH_POOL_GRANTS=encoded_grants)
    if integrated:
        secret_values.update(ISTOK_INTEGRATED_RELEASE='1',
            ISTOK_REGISTRATION_ENABLED='1' if registration_enabled else '0',
            ISTOK_DASHBOARD_WRITES_ENABLED='1' if business_writes_enabled else '0',
            ISTOK_AUTOMATIC_MONTHLY_LIMIT=str(automatic_monthly_limit),
            ISTOK_POOL_BOOTSTRAP=json.dumps(pool_bootstrap,sort_keys=True,allow_nan=False),
            **{'ISTOK_DASHBOARD_'+name.upper():value for name,value in dashboard_config.items()})
    if google_key:
        if not isinstance(google_key,str) or len(google_key)>1024 or any(not 33<=ord(c)<=126 for c in google_key):
            raise ValueError('Invalid Google search credential')
        secret_values['ISTOK_SERPAPI_API_KEY']=google_key
    corrector_path=ROOT/'.runtime/corrector-deployment.json'
    if corrector_path.exists():
        corrector=json.loads(corrector_path.read_text())
        if corrector.get('ready_for_public') is True:
            from plagiarism.correction_jobs import ModalCorrector
            ModalCorrector(corrector['deployment_id'],corrector['model_digest'])
            if (corrector.get('app')!='istok-corrector' or corrector.get('class_name')!='QwenCorrector'
                    or corrector.get('max_containers')!=1 or corrector.get('persistent_weights') is not False):
                raise ValueError('Corrector must match the reviewed private deployment')
            secret_values.update(ISTOK_CORRECTOR_DEPLOYMENT=corrector['deployment_id'],
                                 ISTOK_CORRECTOR_MODEL_DIGEST=corrector['model_digest'])
    owner_path=ROOT/'.runtime/owner-credentials.json'
    if owner_path.exists():
        owner=json.loads(owner_path.read_text())
        secret_values.update(ISTOK_OWNER_USERNAME=owner['username'],ISTOK_OWNER_SALT=owner['salt'],ISTOK_OWNER_DIGEST=owner['digest'])
    secret=modal.Secret.from_dict(secret_values)
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
    url=web.get_web_url()
    receipt={'app':APP_NAME,'url':url,'source_manifest':manifest,'CPU_only':True,
             'GPU':None,'private_data_uploaded':False,'volume':VOLUME_NAME}
    path=ROOT/'.runtime/web-deploy/deployment.json';path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)
    print(json.dumps({'app':APP_NAME,'url':url,'volume':VOLUME_NAME},ensure_ascii=False))


if __name__=='__main__':main()
