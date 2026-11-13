"""Bounded anonymous web beta; reuses the matcher and isolated tenant stores."""
from contextlib import closing
from datetime import datetime,timezone
from hashlib import sha256,pbkdf2_hmac
import hmac
from http.cookies import SimpleCookie,CookieError
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError
from email.utils import parsedate_to_datetime
from urllib.request import Request,build_opener

import server
from plagiarism import access,auth_provider,saas,web_search
from plagiarism.search_credentials import key_for
from plagiarism.search_mediawiki import MediaWikiSearch
from plagiarism.search_tavily import TavilySearch
from plagiarism.tenants import TenantRegistry

COOKIE='istok_beta'
MAX_CHARACTERS=10000
MONTHLY_SEARCHES=180
DAILY_CHECKS=6


class SearchNotStarted(web_search.WebSearchError):
    """Only the built-in preflight may certify that no search was attempted."""


class DurableTenants(TenantRegistry):
    def __init__(self,root,persist):
        super().__init__(root,max_contexts=20,max_active=3)
        self.persist=persist

    def _create(self,identifier):
        context=super()._create(identifier)
        save=context.store.save
        def durable_save(*args,**kwargs):
            value=save(*args,**kwargs);self.persist();return value
        context.store.save=durable_save
        return context


class BetaRuntime:
    def __init__(self,root,origin,persist=lambda:None,*,allow_http=False,runner=None):
        parsed=urlsplit(origin)
        if (parsed.scheme not in ({'https','http'} if allow_http else {'https'}) or not parsed.netloc
                or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError('An exact public HTTPS origin is required')
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.origin=origin;self.host=parsed.netloc;self.secure=parsed.scheme=='https';self.persist=persist
        self.runtime=self.root/'runtime';self.runtime.mkdir(exist_ok=True,mode=0o700)
        (self.runtime/'local-inference-disabled').touch(mode=0o600,exist_ok=True)
        path=self.runtime/'guest-signing.key'
        try:
            with path.open('xb') as stream:
                stream.write(secrets.token_bytes(32));stream.flush();os.fsync(stream.fileno())
            path.chmod(0o600)
        except FileExistsError:pass
        self.key=path.read_bytes()
        if len(self.key)!=32:raise ValueError('Invalid guest signing key')
        self.database=self.root/'beta.sqlite3'
        with closing(self.connect()) as db:
            db.executescript('CREATE TABLE IF NOT EXISTS guests(id TEXT PRIMARY KEY,month TEXT NOT NULL);'
                'CREATE TABLE IF NOT EXISTS rates(guest TEXT,day TEXT,count INTEGER NOT NULL,PRIMARY KEY(guest,day));'
                'CREATE TABLE IF NOT EXISTS checks(guest TEXT,id TEXT,fingerprint TEXT,day TEXT,PRIMARY KEY(guest,id));'
                'CREATE TABLE IF NOT EXISTS unstarted_checks(guest TEXT,id TEXT,PRIMARY KEY(guest,id));'
                'CREATE TABLE IF NOT EXISTS quota_guard(id INTEGER PRIMARY KEY CHECK(id=1),retry_at REAL NOT NULL);')
        self.tenants=DurableTenants(self.root/'tenants',persist)
        self.runner=runner or self.run_web
        self.persist()

    def connect(self):return sqlite3.connect(self.database,timeout=15)

    def identity(self,headers):
        try:
            cookies=SimpleCookie();cookies.load(headers.get('Cookie',''));value=cookies[COOKIE].value
            identifier,signature=value.split('.')
            expected=hmac.new(self.key,identifier.encode(),sha256).hexdigest()
            if re.fullmatch(r'[a-f0-9]{64}',identifier) and hmac.compare_digest(expected,signature):return identifier,None
        except (CookieError,KeyError,ValueError):pass
        month=datetime.now(timezone.utc).strftime('%Y-%m');identifier=secrets.token_hex(32)
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM guests WHERE month=?',(month,)).fetchone()[0]>=100:
                raise access.AccessError('На этот месяц места в бесплатной бете закончились.','beta_capacity',429)
            db.execute('INSERT INTO guests VALUES (?,?)',(identifier,month))
        signature=hmac.new(self.key,identifier.encode(),sha256).hexdigest();cookie=SimpleCookie()
        cookie[COOKIE]=identifier+'.'+signature;cookie[COOKIE]['path']='/';cookie[COOKIE]['httponly']=True
        cookie[COOKIE]['samesite']='Lax';cookie[COOKIE]['max-age']=90*86400
        if self.secure:cookie[COOKIE]['secure']=True
        self.persist()
        return identifier,cookie.output(header='').strip()

    def owner_identity(self,headers):
        try:
            cookie=SimpleCookie();cookie.load(headers.get('Cookie',''))
            value=cookie['istok_owner'].value;guest,expiry,nonce,signature=value.split('.')
            message='owner.'+'.'.join((guest,expiry,nonce))
            expected=hmac.new(self.key,message.encode(),sha256).hexdigest()
            if (re.fullmatch(r'[a-f0-9]{64}',guest) and int(expiry)>time.time()
                    and hmac.compare_digest(expected,signature)):return guest
        except (KeyError,ValueError,CookieError):pass
        return None

    def owner_login(self,body,guest):
        username=os.environ.get('ISTOK_OWNER_USERNAME','');salt=os.environ.get('ISTOK_OWNER_SALT','')
        digest=os.environ.get('ISTOK_OWNER_DIGEST','')
        if not username or not salt or not digest:raise access.AccessError('Вход владельца ещё не настроен.','owner_unconfigured',503)
        password=body.get('password','');login=body.get('username','')
        if not isinstance(password,str) or len(password)>256 or not isinstance(login,str):raise ValueError('Некорректный вход')
        with closing(self.connect()) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS owner_attempts(at REAL)')
            db.execute('DELETE FROM owner_attempts WHERE at<?',(time.time()-900,))
            if db.execute('SELECT count(*) FROM owner_attempts').fetchone()[0]>=20:
                raise access.AccessError('Слишком много попыток. Повторите через 15 минут.','owner_rate_limit',429)
            db.execute('INSERT INTO owner_attempts VALUES (?)',(time.time(),))
        self.persist()
        candidate=pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex()
        if not (hmac.compare_digest(login.encode(),username.encode()) and hmac.compare_digest(candidate,digest)):
            raise access.AccessError('Неверный логин или пароль.','owner_login_failed',401)
        with closing(self.connect()) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS owner_account(id INTEGER PRIMARY KEY CHECK(id=1),guest TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO owner_account VALUES (1,?)',(guest,))
            guest=db.execute('SELECT guest FROM owner_account WHERE id=1').fetchone()[0]
            db.execute('DELETE FROM owner_attempts')
        expiry=str(int(time.time())+7*86400);nonce=secrets.token_hex(16)
        value='.'.join((guest,expiry,nonce))
        signature=hmac.new(self.key,('owner.'+value).encode(),sha256).hexdigest()
        self.persist()
        return 'istok_owner='+value+'.'+signature+'; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800'+('; Secure' if self.secure else '')

    def trusted(self,headers,method):
        host=headers.get('Host','')
        forwarded=(host in {'127.0.0.1:8000','localhost:8000'} and headers.get('X-Forwarded-Host')==self.host)
        if host!=self.host and not forwarded:return False
        origin=headers.get('Origin')
        return origin==self.origin if method not in {'GET','HEAD'} else origin in (None,self.origin)

    def mutation(self,guest):
        day=datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT count FROM rates WHERE guest=? AND day=?',(guest,day)).fetchone()
            if row and row[0]>=40:raise access.AccessError('Лимит действий беты на сегодня исчерпан.','beta_rate_limit',429)
            db.execute('INSERT INTO rates VALUES (?,?,1) ON CONFLICT(guest,day) DO UPDATE SET count=count+1',(guest,day))
        # Bound all beta files well below the free storage allowance.
        used=0
        for path in self.root.rglob('*.sqlite3*'):
            try:
                if path.is_file():used+=path.stat().st_size
            except FileNotFoundError:pass
        if used>256*1024*1024:raise access.AccessError('Хранилище бесплатной беты заполнено.','beta_storage_limit',429)

    def admit_check(self,guest,identifier,text,*,owner=False,rearm=False):
        if not isinstance(identifier,str) or not re.fullmatch(r'[a-f0-9]{32}',identifier):
            raise ValueError('Нужен идентификатор запроса из 32 шестнадцатеричных знаков.')
        fingerprint=sha256(text.encode()).hexdigest()
        day=datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE');old=db.execute('SELECT fingerprint,NOT EXISTS(SELECT 1 FROM unstarted_checks u WHERE u.guest=checks.guest AND u.id=checks.id) FROM checks WHERE guest=? AND id=?',(guest,identifier)).fetchone()
            if old:
                if old[0]!=fingerprint:raise access.AccessError('Идентификатор уже использован для другого текста.','request_conflict',409)
                if old[1] or not rearm:return
            if not owner and db.execute('SELECT count(*) FROM checks WHERE guest=? AND day=? AND NOT EXISTS(SELECT 1 FROM unstarted_checks u WHERE u.guest=checks.guest AND u.id=checks.id)',(guest,day)).fetchone()[0]>=DAILY_CHECKS:
                raise access.AccessError(f'В бете доступны {DAILY_CHECKS} интернет-проверок в сутки для этого браузера. Общая бесплатная квота ограничена.','beta_check_limit',429)
            db.execute('INSERT INTO checks(guest,id,fingerprint,day) VALUES (?,?,?,?) ON CONFLICT(guest,id) DO UPDATE SET day=excluded.day',(guest,identifier,fingerprint,day))
            db.execute('DELETE FROM unstarted_checks WHERE guest=? AND id=?',(guest,identifier))
        self.persist()

    def release_unstarted_check(self,guest,identifier):
        # Keep identity/fingerprint forever: a duplicate still resolves the old job.
        with closing(self.connect()) as db,db:
            db.execute('INSERT OR IGNORE INTO unstarted_checks SELECT guest,id FROM checks WHERE guest=? AND id=?',(guest,identifier))
        self.persist()

    def quota_cooldown(self):
        with closing(self.connect()) as db:
            row=db.execute('SELECT retry_at FROM quota_guard WHERE id=1').fetchone()
        return max(0,int(row[0]-time.time()+1)) if row else 0

    def rate_limited(self,headers):
        delay=600
        value=headers.get('Retry-After','') if headers else ''
        try:
            delay=int(value) if value.isascii() and value.isdecimal() else int(parsedate_to_datetime(value).timestamp()-time.time())
        except (ValueError,TypeError,OverflowError):pass
        with closing(self.connect()) as db,db:
            db.execute('INSERT INTO quota_guard VALUES (1,?) ON CONFLICT(id) DO UPDATE SET retry_at=max(retry_at,excluded.retry_at)',
                       (time.time()+min(86400,max(1,delay)),))
        self.persist()

    def remaining(self,guest,owner=False):
        day=datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with closing(self.connect()) as db:
            used=db.execute('SELECT count(*) FROM checks WHERE guest=? AND day=? AND NOT EXISTS(SELECT 1 FROM unstarted_checks u WHERE u.guest=checks.guest AND u.id=checks.id)',(guest,day)).fetchone()[0]
        budget=self.root/'search-budget.sqlite3';monthly=0
        if budget.exists():
            with closing(sqlite3.connect('file:'+str(budget.resolve())+'?mode=ro',uri=True)) as db:
                monthly=db.execute('SELECT count(*) FROM calls WHERE month=?',(day[:7],)).fetchone()[0]
        return {'search_retry_after_seconds':self.quota_cooldown(),'daily_used':used,'daily_remaining':None if owner else max(0,DAILY_CHECKS-used),
                'daily_reset':'00:00 UTC (05:00 Алматы)','monthly_used':monthly,
                'monthly_remaining':max(0,MONTHLY_SEARCHES-monthly)}

    def owner_statistics(self):
        """Aggregate SQL counts only; never load another tenant's text or IDs."""
        counts={'spaces':0,'reports':0,'sources':0,'trashed':0,'database_bytes':0,'unavailable_spaces':0}
        for path in (self.root/'tenants').glob('*/istok.sqlite3'):
            if not re.fullmatch(r'[a-f0-9]{64}',path.parent.name):continue
            counts['spaces']+=1
            try:
                counts['database_bytes']+=path.stat().st_size
                with closing(sqlite3.connect('file:'+str(path.resolve())+'?mode=ro',uri=True,timeout=2)) as db:
                    counts['reports']+=db.execute("SELECT count(*) FROM records WHERE kind='report'").fetchone()[0]
                    counts['sources']+=db.execute('SELECT count(*) FROM sources').fetchone()[0]
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trash'").fetchone():
                        counts['trashed']+=db.execute('SELECT count(*) FROM trash').fetchone()[0]
            except (OSError,sqlite3.Error):counts['unavailable_spaces']+=1
        day=datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with closing(self.connect()) as db:
            counts['checks_today']=db.execute('SELECT count(*) FROM checks WHERE day=? AND NOT EXISTS(SELECT 1 FROM unstarted_checks u WHERE u.guest=checks.guest AND u.id=checks.id)',(day,)).fetchone()[0]
            counts['guests_this_month']=db.execute('SELECT count(*) FROM guests WHERE month=?',(day[:7],)).fetchone()[0]
        return {'counts':counts,'quota':self.remaining('',owner=True),'monthly_limit':MONTHLY_SEARCHES,
                'storage_limit_bytes':256*1024*1024,'as_of':datetime.now(timezone.utc).isoformat()}

    def run_web(self,text,request_id,progress=None):
        # No paid fallback: confirm the included allowance before any search.
        cooldown=self.quota_cooldown()
        if cooldown:
            raise SearchNotStarted(f'Сервис проверки квоты временно ограничил обращения. Попробуйте новую проверку через {cooldown} сек. Поиск не отправлен, дневная попытка сохранена.','free_quota_rate_limited')
        if progress:progress('Проверяем бесплатную квоту поиска',0,0,phase='preflight')
        request=Request('https://api.tavily.com/usage',headers={'Authorization':'Bearer '+key_for('tavily')})
        try:
            # Retry only the read-only quota endpoint; never duplicate a search.
            for attempt in range(3):
                try:
                    with build_opener(auth_provider.NoRedirect()).open(request,timeout=12) as response:account=json.load(response)['account']
                    break
                except HTTPError as error:
                    try:
                        if error.code==429:self.rate_limited(error.headers)
                    finally:error.close()
                    raise
                except (TimeoutError,OSError):
                    if attempt==2:raise
            usage=account.get('plan_usage');paid=account.get('paygo_usage');needed=len(web_search.plan_queries(text))
            if (account.get('current_plan')!='Researcher' or account.get('plan_limit')!=1000
                    or type(usage) not in (int,float) or not 0<=usage<=900-needed
                    or type(paid) not in (int,float) or paid!=0):raise ValueError('Unconfirmed free quota')
        except Exception:
            cooldown=self.quota_cooldown()
            if cooldown:
                raise SearchNotStarted(f'Сервис проверки квоты временно ограничил обращения (HTTP 429). Повторите вручную через {cooldown} сек. Поиск не отправлен, дневная попытка сохранена.','free_quota_rate_limited') from None
            raise SearchNotStarted('Не удалось подтвердить бесплатную квоту поиска. Запросы не отправлены, дневная попытка сохранена. Повторите вручную новой проверкой, когда сервис восстановится.','free_quota_unconfirmed') from None
        persist=self.persist
        class Budget(web_search.SearchBudget):
            def reserve(self,identifier):super().reserve(identifier);persist()
            def finish(self,identifier):super().finish(identifier);persist()
        search=TavilySearch(Budget(self.root/'search-budget.sqlite3',limit=MONTHLY_SEARCHES))
        wiki=MediaWikiSearch(path=self.root/'private/mediawiki.sqlite3')
        return web_search.run(text,request_id,progress=progress,searcher=search,wiki_searcher=wiki)


class BetaHandler(server.Handler):
    def trusted_request(self):return self.server.beta.trusted(self.headers,self.command)

    def _dispatch(self,method,leases):
        path=urlsplit(self.path).path
        if path.startswith('/api/') or path.startswith('/reports/'):
            try:
                if not self.trusted_request():
                    self.send_json({'error':'Запрос разрешён только с сайта Истока.'},403);return
                self.owner=self.server.beta.owner_identity(self.headers)
                self.guest,cookie=(self.owner,None) if self.owner else self.server.beta.identity(self.headers)
                if cookie:self.response_headers.append(('Set-Cookie',cookie))
                self.tenant=leases.enter_context(self.server.beta.tenants.context(self.guest))
                if method not in {'GET','HEAD'}:self.server.beta.mutation(self.guest)
            except access.AccessError as error:
                self.send_json({'error':str(error),'code':error.code},error.status);return
        return super()._dispatch(method,leases)

    def static(self,path):
        if path.startswith('/setup-search'):raise access.AccessError('Настройка поиска недоступна посетителям.','operation_forbidden',403)
        return super().static(path)

    def route(self,method,path,body):
        beta=self.server.beta
        if method=='POST' and path=='/api/owner/login':
            cookie=beta.owner_login(body,self.guest);self.response_headers.append(('Set-Cookie',cookie))
            return {'ok':True}
        if method=='POST' and path=='/api/owner/logout':
            self.response_headers.append(('Set-Cookie','istok_owner=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0'+('; Secure' if beta.secure else '')))
            return {'ok':True}
        if method=='GET' and path=='/api/session':
            return {'mode':'local','web':web_search.status(),'beta':{'id':self.guest,'max_characters':MAX_CHARACTERS,
                **beta.remaining(self.guest,owner=bool(self.owner)),'owner':bool(self.owner),'persistent':True,'daily_checks':None if self.owner else DAILY_CHECKS,'monthly_search_queries':MONTHLY_SEARCHES}}
        if method=='GET' and path=='/api/owner/statistics':
            if not self.owner:raise access.AccessError('Статистика сайта доступна владельцу.','owner_required',403)
            return beta.owner_statistics()
        allowed=(method=='GET' and (path in {'/api/status','/api/sources','/api/source-packs','/api/reports','/api/data','/api/data-export','/api/dictionary'}
                or re.fullmatch(r'/api/(?:reports|jobs|report-exports)/[a-f0-9]{32}',path))
            or method=='POST' and path in {'/api/sources','/api/source-packs/install','/api/check','/api/report-reviews','/api/web-check-jobs','/api/demo','/api/import-document','/api/proofread','/api/dictionary'}
            or method=='POST' and re.fullmatch(r'/api/jobs/[a-f0-9]{32}/cancel',path)
            or method=='POST' and re.fullmatch(r'/api/data/(?:report|source)/[^/]{1,100}/(?:trash|restore)',path)
            or method=='DELETE' and re.fullmatch(r'/api/sources/[^/]{1,100}',path))
        if not allowed:raise access.AccessError('Эта операция недоступна в онлайн-бете.','operation_forbidden',403)
        if method=='POST':
            text=body.get('text','')
            if not isinstance(text,str) or len(text)>MAX_CHARACTERS:
                raise access.AccessError('В онлайн-бете проверяем до 10 000 знаков за один раз.','beta_text_limit',400)
            if path=='/api/sources' and self.tenant.store.data_summary()['totals']['sources']>=37:
                raise access.AccessError('В бете можно добавить до пяти собственных источников.','beta_source_limit',429)
        if method=='POST' and path=='/api/web-check-jobs':
            if body.get('internet_consent') is not True:raise access.AccessError('Нужно согласие на интернет-поиск.','internet_consent_required',400)
            text=server.field(body,'text',MAX_CHARACTERS);identifier=body.get('request_id')
            store,jobs=self.tenant.store,self.tenant.jobs
            saas.ensure_job_identifier(store,identifier)
            remote_id=sha256((self.guest+'\0'+identifier).encode()).hexdigest()[:32]
            def operation(progress):
                try:
                    payload=beta.runner(text,remote_id,progress)
                except SearchNotStarted:
                    beta.release_unstarted_check(self.guest,identifier)
                    raise
                report=store.save('report',{**payload,'id':saas.report_identifier(identifier)})
                return {'report':server.public_report(report,store.match_reviews(report['id']))}
            # Serialize admission with scheduling for this tenant: a rejected queue
            # cannot race a duplicate that starts work while its slot is refunded.
            with jobs.lock:
                existing=(store.get('model-request','model-request:'+identifier)
                          or store.get('model-job',identifier) or jobs.items.get(identifier))
                beta.admit_check(self.guest,identifier,text,owner=bool(self.owner),rearm=not existing)
                try:
                    return {'job':jobs.submit_model(identifier,'/api/web-check',{'request_id':identifier,'text':text,'internet_consent':True},operation,store)}
                except Exception:
                    if not store.get('model-request','model-request:'+identifier) and identifier not in jobs.items:
                        beta.release_unstarted_check(self.guest,identifier)
                    raise
        return super().route(method,path,body)

    def send_json(self,result,status=200):
        if self.command not in {'GET','HEAD'}:self.server.beta.persist()
        return super().send_json(result,status)


def create_server(root,origin,*,host='0.0.0.0',port=8000,persist=lambda:None,allow_http=False,runner=None):
    beta=BetaRuntime(root,origin,persist,allow_http=allow_http,runner=runner)
    os.environ.update(ISTOK_MODEL_PROVIDER='local',ISTOK_RUNTIME_DIR=str(beta.runtime),ISTOK_APP_MODE='local')
    service=ThreadingHTTPServer((host,port),BetaHandler);service.daemon_threads=True
    service.beta=beta;service.saas=None;service.store=None
    return service
