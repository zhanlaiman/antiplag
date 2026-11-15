"""Synthetic keys only. Exercises the real service, filesystem and HTTP adapter."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from plagiarism.access import AccessError
from plagiarism.auth_provider import NoRedirect
from plagiarism.search_pool_admin import PoolAdmin, probe_usage, route, MAX_USAGE_BYTES


SERP_KEY = "offline-credential-" + "1" * 32
OTHER_KEY = "offline-credential-" + "2" * 32
TAVILY_KEY = "tvly-synthetic-offline-fixture-111111"


def serp_usage(used=20):
    return {"account_id": "synthetic-account-1", "account_status": "Active", "plan_name": "Free Plan",
            "plan_monthly_price": 0, "searches_per_month": 250, "plan_searches_left": 250-used,
            "extra_credits": 0, "total_searches_left": 250-used, "this_month_usage": used,
            "account_email": "never-return@example.test", "api_key": SERP_KEY}


def tavily_usage(used=20):
    return {"key": {"usage": used, "limit": 1000}, "account": {"current_plan": "Researcher",
            "plan_usage": used, "plan_limit": 1000, "paygo_usage": 0, "paygo_limit": 0}}


def runtime_metrics():
    return {"monthly_limit": 999999, "monthly_used": 26, "ignored": SERP_KEY,
            "providers": {provider: {"local_used": 26, "local_remaining": 154,
                                    "grant_remaining": 42, "grant_approved": True,
                                    "grant_expires_at": 1700001000, "raw_error": SERP_KEY}
                          for provider in ("serpapi", "tavily", "serper", "exa")}}


def grant(candidate, **overrides):
    return {"approved": True, "provider": candidate["provider"], "credential_id": candidate["credential_id"],
            "account_ref": "approved-account-1", "grant_id": "existing-free-grant-1", "remaining": 42,
            "expires_at": 1700001000, "cash_spend_limit_usd": 0,
            "max_unit_cost_usd": 0.004 if candidate["provider"] == "exa" else 0, **overrides}


def synthetic_guard(handler, method):
    """Harness guard only; production guard is owned by cloud.dashboard."""
    if handler.headers.get("Cookie") != "dashboard=synthetic-session":
        raise AccessError("Admin session required", "dashboard_auth_required", 401)
    if handler.headers.get("Host") != "dashboard.example":
        raise AccessError("Host denied", "dashboard_origin_invalid", 403)
    if method != "GET" and (handler.headers.get("Origin") != "https://dashboard.example"
                             or handler.headers.get("X-CSRF-Token") != "synthetic-csrf"):
        raise AccessError("Origin or CSRF denied", "dashboard_csrf_invalid", 403)


class PoolHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self): self.dispatch("GET")
    def do_POST(self): self.dispatch("POST")
    def do_DELETE(self): self.dispatch("DELETE")

    def dispatch(self, method):
        try:
            synthetic_guard(self, method)  # Real deployment must gate before parsing too.
            length = self.headers.get("Content-Length", "0")
            if not length.isdigit() or int(length) > 4096:
                raise AccessError("Request cap", "pool_body_too_large", 413)
            raw = self.rfile.read(int(length))
            try:
                body = json.loads(raw) if raw else {}
            except (ValueError, UnicodeError):
                raise AccessError("Invalid JSON", "pool_invalid_input", 400) from None
            value = route(self, method, self.path, body, authorize=synthetic_guard)
            status = 200
        except AccessError as error:
            value, status = {"error": str(error), "code": error.code}, error.status
        raw = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class PoolServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.clock = Mock(return_value=1700000000)
        self.probe = Mock(side_effect=lambda provider, key: serp_usage() if provider == "serpapi" else tavily_usage())
        self.persist = Mock()
        self.service = self.create()

    def tearDown(self): self.directory.cleanup()

    def create(self, **kw):
        return PoolAdmin(self.root, kw.pop("persist", self.persist), usage_probe=self.probe,
                         runtime_status=runtime_metrics, clock=self.clock, **kw)

    def stage(self, provider="serpapi", key=SERP_KEY):
        return self.service.stage(provider, key)["credential_id"]

    def assert_code(self, code, operation):
        with self.assertRaises(AccessError) as raised: operation()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_invalid_provider_type_size_and_controls_do_not_write(self):
        for provider, key in [(None, SERP_KEY), ([], SERP_KEY), ("unknown", SERP_KEY), ("serpapi", None),
                              ("serpapi", "short"), ("serpapi", "x"*1025), ("tavily", SERP_KEY),
                              ("serpapi", "x"*20+"\n"), ("serpapi", "x"*20+"\u00e9")]:
            with self.subTest(provider=provider, size=len(key) if isinstance(key, str) else None):
                self.assert_code("pool_invalid_input", lambda: self.service.stage(provider, key))
        self.assertFalse((self.service.vault.root / "vault.json").exists())
        self.persist.assert_not_called()

    def test_paid_contradictory_and_boolean_serpapi_policies_are_rejected(self):
        for change in [{"plan_monthly_price": 1}, {"plan_monthly_price": False}, {"extra_credits": 1},
                       {"account_status": "Inactive"}, {"plan_name": "Developer Plan"},
                       {"searches_per_month": 1000}, {"plan_searches_left": True},
                       {"this_month_usage": 21}, {"error": SERP_KEY}]:
            with self.subTest(change=change):
                identifier = self.stage(key="synthetic-policy-"+str(len(self.service.status()["credentials"])).zfill(16))
                self.probe.return_value = None
                self.probe.side_effect = lambda *args, change=change: {**serp_usage(), **change}
                result = self.service.validate(identifier)
                self.assertEqual(result["pool"]["credentials"][-1]["code"], "quota_unconfirmed")
                self.assert_code("pool_check_required", lambda: self.service.activate(identifier))

    def test_tavily_rejects_paygo_and_enforces_guard900_and_key_cap(self):
        identifier = self.stage("tavily", TAVILY_KEY)
        for value, code, guard_left in [(tavily_usage(899), "free_verified", 1),
                                       (tavily_usage(900), "quota_exhausted", 0)]:
            self.clock.return_value += 4
            self.probe.side_effect = lambda *args, value=value: value
            row = self.service.validate(identifier)["pool"]["credentials"][0]
            self.assertEqual((row["code"], row["guard_remaining"]), (code, guard_left))
        for field, value in [("paygo_usage", 1), ("paygo_limit", 1), ("plan_limit", 999),
                             ("plan_usage", True), ("current_plan", "Bootstrap")]:
            self.clock.return_value += 4
            payload = tavily_usage(); payload["account"][field] = value
            self.probe.side_effect = lambda *args, payload=payload: payload
            row = self.service.validate(identifier)["pool"]["credentials"][0]
            self.assertEqual(row["code"], "quota_unconfirmed")
        self.clock.return_value += 4
        payload=tavily_usage();payload["key"]={"usage":20,"limit":20}
        self.probe.side_effect=lambda *args:payload
        self.assertEqual(self.service.validate(identifier)["pool"]["credentials"][0]["guard_remaining"],0)

    def test_exception_reflection_and_metrics_never_escape(self):
        identifier = self.stage()
        self.probe.side_effect = URLError("https://example.test/?api_key="+SERP_KEY)
        result = self.service.validate(identifier)
        public = json.dumps(result)
        self.assertNotIn(SERP_KEY, public)
        self.assertNotIn("api_key", public)
        self.assertEqual(result["pool"]["credentials"][0]["code"], "provider_unavailable")
        self.service.runtime_status = Mock(side_effect=ValueError(SERP_KEY))
        self.assertFalse(self.service.status()["runtime_connected"])

    def test_rotation_restart_pinned_versions_audit_and_counters_are_retained(self):
        first = self.stage(); self.service.validate(first); self.service.activate(first)
        initial_ref = self.service.runtime_snapshot()["active"]["serpapi"]["account_ref"]
        second = self.stage(key=OTHER_KEY);self.service.validate(second);self.service.activate(second)
        current = self.service.runtime_snapshot()
        self.assertEqual(current["active"]["serpapi"]["credential_id"], second)
        self.assertEqual(current["active"]["serpapi"]["account_ref"], initial_ref)
        self.assertNotIn("synthetic-account",initial_ref)
        self.assertEqual(self.service.resolve_credential(first), SERP_KEY)
        status = self.create().status()
        self.assertEqual(status["monthly_limit"], 180)
        self.assertEqual(status["monthly_used"], 26)
        self.assertEqual(status["providers"][0]["local_used"],26)
        self.assertEqual(status["providers"][0]["local_remaining"],154)
        self.assertEqual(status["providers"][0]["provider_remaining"],230)
        self.assertEqual(len(status["credentials"]), 2)
        raw = json.loads((self.service.vault.root/"vault.json").read_text())
        self.assertEqual(raw["revision"],6)
        self.assertEqual(len(raw["audit"]),6)
        self.assertNotIn(SERP_KEY,json.dumps(raw["audit"]))
        self.assertNotIn("never-return@example.test", json.dumps(raw))
        self.service.disable("serpapi")
        self.assertEqual(self.create().runtime_snapshot()["active"],{})
        self.assertEqual(self.create().resolve_credential(second),OTHER_KEY)

    def test_unknown_provider_never_probes_and_requires_explicit_existing_grant(self):
        for provider in ("serper", "exa"):
            identifier=self.stage(provider,"synthetic-"+provider+"-search-key-111111")
            row=self.service.validate(identifier)["pool"]["credentials"][-1]
            self.assertEqual(row["code"],"balance_unconfirmed")
            self.assertIsNone(row["provider_remaining"])
            self.assert_code("pool_grant_required",lambda:self.service.activate(identifier))
        self.probe.assert_not_called()

    def test_missing_expired_wrong_binding_and_nonzero_cash_grants_deny(self):
        identifier=self.stage("exa","synthetic-exa-search-key-111111")
        for change in [{"approved":False},{"provider":"serper"},{"credential_id":"b"*32},
                       {"account_ref":""},{"grant_id":None},{"remaining":0},{"remaining":True},
                       {"expires_at":1700000000},{"expires_at":float("inf")},
                       {"cash_spend_limit_usd":0.001},{"cash_spend_limit_usd":False},
                       {"max_unit_cost_usd":0.01},{"max_unit_cost_usd":float("nan")}]:
            with self.subTest(fields=list(change)):
                self.service.activation_policy=lambda candidate,change=change:grant(candidate,**change)
                self.assert_code("pool_grant_required",lambda:self.service.activate(identifier))
        for value in [True,{},None]:
            self.service.activation_policy=lambda candidate,value=value:value
            self.assert_code("pool_grant_required",lambda:self.service.activate(identifier))

    def test_unknown_rotation_keeps_same_grant_and_local_usage(self):
        self.service.activation_policy=grant
        first=self.stage("exa","synthetic-exa-first-searchkey111")
        self.service.activate(first)
        second=self.stage("exa","synthetic-exa-second-searchkey222")
        self.service.activate(second)
        snapshot=self.create(activation_policy=grant).runtime_snapshot()
        self.assertEqual(snapshot["active"]["exa"]["admission"]["grant_id"],"existing-free-grant-1")
        status=self.service.status()
        self.assertEqual(status["providers"][3]["local_used"],26)
        self.assertEqual(status["providers"][3]["grant_remaining"],42)
        self.assertIsNone(status["providers"][3]["provider_remaining"])
        self.assertNotIn("existing-free-grant-1",json.dumps(status))
        self.clock.return_value=1700001001
        self.assert_code("pool_grant_required",lambda:self.service.activate(second))

    def test_current_runtime_exhaustion_overrides_saved_activation_grant(self):
        self.service.activation_policy=grant
        identifier=self.stage("exa","synthetic-exa-searchkey-111111")
        self.service.activate(identifier)
        metrics=runtime_metrics()
        metrics["providers"]["exa"].update(grant_remaining=0,local_remaining=0)
        self.service.runtime_status=lambda:metrics
        status=self.service.status()
        self.assertEqual(status["providers"][3]["health"],"exhausted")
        self.assertEqual(status["providers"][3]["grant_remaining"],0)
        self.assertTrue(status["providers"][3]["low_quota"])

    def test_monthly_limit_default_and_trusted_configuration_preserve_spend(self):
        identifier=self.stage();self.service.validate(identifier);self.service.activate(identifier)
        metrics=runtime_metrics();metrics["monthly_used"]=177
        metrics["providers"]["serpapi"].update(local_used=177,local_remaining=999999)
        before=self.service.vault.read()[0]
        for configured,expected_remaining in ((180,3),(900,723)):
            with self.subTest(configured=configured):
                service=self.create() if configured==180 else self.create(monthly_limit=configured)
                service.runtime_status=lambda:metrics
                status=service.status();provider=status["providers"][0]
                self.assertEqual((status["monthly_limit"],status["monthly_used"]),(configured,177))
                self.assertEqual((provider["local_used"],provider["local_remaining"]),(177,expected_remaining))
                self.assertEqual(provider["provider_remaining"],230)
                self.assertEqual(provider["grant_remaining"],42)
                self.assertEqual(provider["health"],"healthy")
                self.assertEqual(service.resolve_credential(identifier),SERP_KEY)
                self.assertEqual(service.vault.read()[0],before)
        self.assertEqual(self.create().status()["monthly_limit"],180)

    def test_monthly_limit_health_threshold_and_tighter_remaining(self):
        identifier=self.stage();self.service.validate(identifier);self.service.activate(identifier)
        metrics=runtime_metrics()
        service=self.create(monthly_limit=900);service.runtime_status=lambda:metrics
        for used,remaining,health in ((180,720,"healthy"),(899,1,"healthy"),(900,0,"exhausted"),(901,0,"exhausted")):
            with self.subTest(used=used):
                metrics["monthly_used"]=used;metrics["providers"]["serpapi"]["local_remaining"]=999999
                status=service.status();row=status["providers"][0]
                self.assertEqual((status["monthly_used"],row["local_remaining"],row["health"]),(used,remaining,health))
        metrics["monthly_used"]=177;metrics["providers"]["serpapi"]["local_remaining"]=2
        row=service.status()["providers"][0]
        self.assertEqual(row["local_remaining"],2)
        self.assertTrue(row["low_quota"])

    def test_monthly_limit_unknown_metrics_stay_unknown(self):
        service=self.create(monthly_limit=900)
        metrics={"monthly_limit":999999,"providers":{"serpapi":{"local_remaining":999999}}}
        service.runtime_status=lambda:metrics
        status=service.status()
        self.assertIsNone(status["monthly_used"])
        self.assertEqual(status["providers"][0]["local_remaining"],900)
        for invalid in (None,False,True,-1,"723",float("nan")):
            with self.subTest(invalid_type=type(invalid).__name__):
                metrics["monthly_used"]=invalid;metrics["providers"]["serpapi"]["local_remaining"]=invalid
                status=service.status()
                self.assertIsNone(status["monthly_used"])
                self.assertIsNone(status["providers"][0]["local_remaining"])

    def test_monthly_limit_strict_validation_precedes_vault_io(self):
        invalid=(True,False,0,-1,1001,180.0,900.0,float("nan"),float("inf"),"900",None,[],{})
        with patch("plagiarism.search_pool_admin.KeyVault") as vault:
            for value in invalid:
                with self.subTest(value_type=type(value).__name__):
                    with self.assertRaises(ValueError):self.create(monthly_limit=value)
            vault.assert_not_called()
        for value in (1,1000):
            self.assertEqual(self.create(monthly_limit=value).status()["monthly_limit"],value)

    def test_expired_future_checks_block_activation_but_config_remains_for_runtime_preflight(self):
        identifier=self.stage();self.service.validate(identifier);self.service.activate(identifier)
        self.clock.return_value+=301
        self.assert_code("pool_check_required",lambda:self.service.activate(identifier))
        self.clock.return_value=1699999999
        self.assert_code("pool_check_required",lambda:self.service.activate(identifier))
        self.assertEqual(self.service.runtime_snapshot()["active"]["serpapi"]["credential_id"],identifier)
        self.assertEqual(self.service.resolve_credential(identifier),SERP_KEY)

    def test_cooldown_missing_ids_and_duplicate_stage(self):
        identifier=self.stage();self.service.validate(identifier)
        self.assert_code("pool_check_rate_limited",lambda:self.service.validate(identifier))
        self.assertEqual(self.stage(),identifier)
        self.assertEqual(len(self.service.status()["credentials"]),1)
        self.assert_code("pool_invalid_input",lambda:self.service.validate("bad-id"))
        self.assert_code("pool_credential_missing",lambda:self.service.activate("c"*32))

    def test_persist_failure_at_each_barrier_restores_pending_oldstate_on_restart(self):
        first=self.stage();self.service.validate(first);self.service.activate(first)
        second=self.stage(key=OTHER_KEY);self.service.validate(second)
        previous=self.service.status()
        for barrier in (1,2,3):
            calls=[]
            def persist():
                calls.append(1)
                if len(calls)==barrier: raise OSError(SERP_KEY)
            self.service.vault.persist=persist
            self.assert_code("pool_recovery_required",lambda:self.service.activate(second))
            restarted=self.create()
            status=restarted.status()
            self.assertTrue(status["recovery_required"])
            self.assertEqual(status["revision"],previous["revision"])
            self.assertEqual(status["audit_count"],previous["audit_count"])
            self.assertEqual(status["providers"][0]["active_credential_id"],first)
            self.assert_code("pool_recovery_required",restarted.runtime_snapshot)
            self.assertEqual(restarted.resolve_credential(first),SERP_KEY)
            restarted.vault.recover()
            self.assertFalse(self.create().status()["recovery_required"])
            self.assertEqual(self.create().runtime_snapshot()["active"]["serpapi"]["credential_id"],first)

    def test_private_files_corruption_symlinks_and_modes_never_reset_state(self):
        self.stage()
        for path in self.service.vault.root.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.service.vault.root.stat().st_mode & 0o777,0o700)
        path=self.service.vault.root/"vault.json";original=path.read_bytes()
        path.chmod(0o644)
        self.assert_code("pool_storage_unavailable",self.service.status)
        path.chmod(0o600)
        for raw in (b"{}",b"null",b"corrupt"):
            path.write_bytes(raw)
            self.assert_code("pool_storage_unavailable",self.service.status)
        path.write_bytes(original)
        target=self.root/"other.json";target.write_bytes(original);target.chmod(0o600)
        path.unlink();path.symlink_to(target)
        self.assert_code("pool_storage_unavailable",self.service.status)

    def test_concurrent_versions_have_serial_revisions_and_all_keys_survive(self):
        def stage(index):
            return self.create().stage("serper","synthetic-concurrent-key-"+str(index).zfill(16))["credential_id"]
        with ThreadPoolExecutor(max_workers=4) as executor:
            identifiers=list(executor.map(stage,range(12)))
        status=self.service.status()
        self.assertEqual((status["revision"],status["audit_count"],len(status["credentials"])),(12,12,12))
        self.assertEqual(len(set(identifiers)),12)


class PoolTransportTests(unittest.TestCase):
    def test_fixed_endpoints_current_key_cap_timeout_and_no_redirect_handler(self):
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.status=200;response.read.return_value=b'{"key":{},"account":{}}'
        opener=Mock();opener.open.return_value=response
        with patch("plagiarism.search_pool_admin.build_opener",return_value=opener) as build:
            probe_usage("tavily",TAVILY_KEY)
            request=opener.open.call_args.args[0]
            self.assertEqual(request.full_url,"https://api.tavily.com/usage")
            self.assertEqual(request.get_header("Authorization"),"Bearer "+TAVILY_KEY)
            self.assertEqual(opener.open.call_args.kwargs,{"timeout":12})
            self.assertIsInstance(build.call_args.args[0],NoRedirect)
            self.assertIsNone(build.call_args.args[0].redirect_request(request,None,302,"x",{},"https://example.test"))
            response.read.assert_called_with(MAX_USAGE_BYTES+1)
            probe_usage("serpapi",SERP_KEY)
            self.assertEqual(opener.open.call_args.args[0].full_url,"https://serpapi.com/account.json?api_key="+SERP_KEY)
            response.read.return_value=b"x"*(MAX_USAGE_BYTES+1)
            with self.assertRaises(ValueError):probe_usage("tavily",TAVILY_KEY)
            response.read.return_value=b"[]"
            with self.assertRaises(ValueError):probe_usage("tavily",TAVILY_KEY)
            error=HTTPError("https://example.test/?key="+SERP_KEY,302,SERP_KEY,{},BytesIO(SERP_KEY.encode()))
            opener.open.side_effect=error
            with self.assertRaises(ValueError) as raised:probe_usage("serpapi",SERP_KEY)
            self.assertNotIn(SERP_KEY,str(raised.exception))


class PoolHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory=TemporaryDirectory()
        cls.pool=PoolAdmin(cls.directory.name,usage_probe=lambda *args:serp_usage(),runtime_status=runtime_metrics)
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),PoolHTTPHandler)
        cls.server.beta=SimpleNamespace(pool_admin=cls.pool)
        cls.thread=Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.directory.cleanup()

    def request(self,method,path,body=None,**headers):
        default={"Host":"dashboard.example","Origin":"https://dashboard.example","Cookie":"dashboard=synthetic-session",
                 "X-CSRF-Token":"synthetic-csrf","Content-Type":"application/json"}
        default.update(headers)
        connection=HTTPConnection("127.0.0.1",self.server.server_port,timeout=5)
        try:
            connection.request(method,path,json.dumps(body).encode() if body is not None else None,default)
            response=connection.getresponse();raw=response.read()
            return response.status,dict(response.getheaders()),json.loads(raw)
        finally:connection.close()

    def test_missing_guest_user_api_cookie_cannot_read_or_mutate(self):
        for cookie in ("","istok_beta=guest","istok_owner=user","api_token=user-token"):
            for method,path,body in [("GET","/api/dashboard/pool",None),("POST","/api/dashboard/pool/stage",{"provider":"serpapi","api_key":SERP_KEY})]:
                with self.subTest(cookie=cookie,method=method):
                    status,headers,value=self.request(method,path,body,Cookie=cookie,Authorization="Bearer user-api-token")
                    self.assertEqual(status,401);self.assertNotIn(SERP_KEY,json.dumps(value))
                    self.assertEqual(headers["Cache-Control"],"no-store")

    def test_default_deny_and_guard_runs_before_vault_attribute_access(self):
        handler=SimpleNamespace(headers={},server=object())
        for guard in (None,Mock(side_effect=AccessError("Denied","dashboard_auth_required",401))):
            with self.assertRaises(AccessError) as raised:route(handler,"GET","/api/dashboard/pool",{},authorize=guard)
            self.assertEqual(raised.exception.status,401)

    def test_cross_origin_csrf_host_wrong_method_and_unknown_fields(self):
        payload={"provider":"serpapi","api_key":SERP_KEY}
        for headers in [{"Origin":"https://evil.example"},{"X-CSRF-Token":"wrong"},{"Host":"evil.example"}]:
            self.assertEqual(self.request("POST","/api/dashboard/pool/stage",payload,**headers)[0],403)
        self.assertEqual(self.request("GET","/api/dashboard/pool/stage")[0],405)
        self.assertEqual(self.request("DELETE","/api/dashboard/pool")[0],405)
        self.assertEqual(self.request("POST","/api/dashboard/pool/stage",{**payload,"approved":True})[0],400)
        self.assertEqual(self.request("POST","/api/dashboard/pool/stage",payload,**{"Content-Type":"text/plain"})[0],415)
        self.assertEqual(self.request("POST","/api/dashboard/pool/stage",{"provider":"serpapi","api_key":"x"*5000})[0],413)

    def test_browser_monthly_limit_fields_cannot_reconfigure_or_mutate_pool(self):
        before=self.pool.status()
        for action,body in (("stage",{"provider":"serpapi","api_key":SERP_KEY}),
                            ("validate",{"credential_id":"c"*32}),
                            ("activate",{"credential_id":"c"*32}),
                            ("disable",{"provider":"serpapi"})):
            with self.subTest(action=action):
                status,_,value=self.request("POST","/api/dashboard/pool/"+action,{**body,"monthly_limit":900})
                self.assertEqual((status,value["code"]),(400,"pool_invalid_input"))
        self.assertEqual(self.request("POST","/api/dashboard/pool/config",{"monthly_limit":900})[0],404)
        status,_,after=self.request("GET","/api/dashboard/pool")
        self.assertEqual((status,after["monthly_limit"]),(200,180))
        self.assertEqual((after["revision"],after["audit_count"]),(before["revision"],before["audit_count"]))

    def test_real_http_stage_validate_activate_disable_and_safe_status(self):
        status,_,result=self.request("POST","/api/dashboard/pool/stage",{"provider":"serpapi","api_key":SERP_KEY})
        self.assertEqual(status,200);identifier=result["credential_id"]
        for action in ("validate","activate"):
            status,_,result=self.request("POST","/api/dashboard/pool/"+action,{"credential_id":identifier})
            self.assertEqual(status,200)
        status,_,result=self.request("GET","/api/dashboard/pool")
        self.assertEqual(status,200)
        self.assertEqual(result["providers"][0]["provider_remaining"],230)
        self.assertNotIn(SERP_KEY,json.dumps(result))
        self.assertNotIn("never-return@example.test",json.dumps(result))
        self.assertEqual(self.request("POST","/api/dashboard/pool/disable",{"provider":"serpapi"})[0],200)

    def test_persistfailure_over_http_is_safe_and_restart_sees_prior_state(self):
        prior=self.pool.status()
        with patch.object(self.pool.vault,"persist",side_effect=OSError(SERP_KEY)):
            status,_,value=self.request("POST","/api/dashboard/pool/stage",{"provider":"serpapi","api_key":OTHER_KEY})
        self.assertEqual(status,503)
        self.assertEqual(value["code"],"pool_recovery_required")
        self.assertNotIn(SERP_KEY,json.dumps(value))
        self.assertNotIn(OTHER_KEY,json.dumps(value))
        restarted=PoolAdmin(self.directory.name,runtime_status=runtime_metrics)
        self.assertTrue(restarted.status()["recovery_required"])
        self.assertEqual(restarted.status()["revision"],prior["revision"])
        self.pool.vault.recover()


if __name__ == "__main__": unittest.main()
