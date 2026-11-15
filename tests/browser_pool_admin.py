"""Drive the actual pool UI with synthetic service/auth/usage boundaries.

python3 tests/browser_pool_admin.py --output /absolute/private/evidence
Requires an already installed Playwright/Chromium. No account access.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plagiarism.access import AccessError
from plagiarism.search_pool_admin import PoolAdmin, route
from test_search_pool_admin import serp_usage, tavily_usage, runtime_metrics
from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_KEY = "synthetic-browser-search-key-111111"


def guard(handler, method):
    if handler.headers.get("Cookie") != "dashboard=synthetic-session":
        raise AccessError("Admin required", "dashboard_auth_required", 401)
    if handler.headers.get("Host") != urlsplit(handler.server.origin).netloc:
        raise AccessError("Host denied", "dashboard_origin_invalid", 403)
    if method != "GET" and (handler.headers.get("Origin") != handler.server.origin
                             or handler.headers.get("X-CSRF-Token") != "synthetic-csrf"):
        raise AccessError("CSRF denied", "dashboard_csrf_invalid", 403)


class UIHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self): self.dispatch("GET")
    def do_POST(self): self.dispatch("POST")

    def dispatch(self, method):
        try:
            guard(self, method)
            path=urlsplit(self.path).path
            files={"/dashboard/pool":("pool.html","text/html"),"/pool.js":("pool.js","text/javascript"),"/pool.css":("pool.css","text/css")}
            if method=="GET" and path in files:
                name,mime=files[path];self.send((ROOT/"web"/name).read_bytes(),200,mime);return
            if method=="GET" and path=="/api/dashboard/session":
                value={"authenticated":True,"username":"synthetic-admin","csrf_token":"synthetic-csrf","expires_at":time.time()+3600}
            else:
                length=self.headers.get("Content-Length","0")
                if not length.isdigit() or int(length)>4096:
                    raise AccessError("Request cap","pool_body_too_large",413)
                raw=self.rfile.read(int(length));body=json.loads(raw) if raw else {}
                value=route(self,method,path,body,authorize=guard)
            self.send(json.dumps(value).encode(),200,"application/json")
        except AccessError as error:
            self.send(json.dumps({"error":str(error),"code":error.code}).encode(),error.status,"application/json")

    def send(self,raw,status,mime):
        self.send_response(status);self.send_header("Content-Type",mime+"; charset=utf-8")
        self.send_header("Content-Length",str(len(raw)));self.send_header("Cache-Control","no-store")
        self.send_header("Content-Security-Policy","default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("X-Content-Type-Options","nosniff");self.end_headers();self.wfile.write(raw)


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--output",required=True);args=parser.parse_args()
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    calls=[];errors=[];outbound=[];steps=[];measurements=[]
    with TemporaryDirectory() as directory:
        def probe(provider,key):
            calls.append(provider)
            return serp_usage() if provider=="serpapi" else tavily_usage()
        pool=PoolAdmin(directory,usage_probe=probe,runtime_status=runtime_metrics)
        server=ThreadingHTTPServer(("127.0.0.1",0),UIHandler)
        server.beta=SimpleNamespace(pool_admin=pool);server.origin=f"http://127.0.0.1:{server.server_port}"
        port=server.server_port;thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(headless=True)
                context=browser.new_context(viewport={"width":1440,"height":1000},reduced_motion="reduce")
                context.add_cookies([{"name":"dashboard","value":"synthetic-session","url":server.origin,"httpOnly":True,"sameSite":"Strict"}])
                def local_only(request_route):
                    if request_route.request.url.startswith(server.origin+"/"):request_route.continue_()
                    else:outbound.append(request_route.request.url);request_route.abort()
                context.route("**/*",local_only)
                page=context.new_page();page.on("pageerror",lambda error:errors.append(str(error)))
                page.goto(server.origin+"/dashboard/pool",wait_until="load")
                expect(page.get_by_text("Ключей пока нет.",exact=False)).to_be_visible()
                page.screenshot(path=str(output/"empty-1440.png"),full_page=True)
                steps.append("Empty state rendered after real session+pool GET")
                page.get_by_label("Провайдер",exact=True).select_option("serpapi")
                page.get_by_label("Ключ API",exact=True).fill(SYNTHETIC_KEY)
                page.get_by_role("button",name="Сохранить ключ",exact=True).click()
                expect(page.get_by_label("Ключ API",exact=True)).to_have_value("")
                expect(page.get_by_role("button",name="Проверить квоту",exact=True)).to_be_enabled()
                expect(page.get_by_role("button",name="Подключить",exact=True)).to_be_disabled()
                page.get_by_role("button",name="Проверить квоту",exact=True).click()
                expect(page.get_by_text("Бесплатная квота подтверждена.",exact=True)).to_be_visible()
                page.get_by_role("button",name="Подключить",exact=True).click()
                expect(page.get_by_text("Активный ключ",exact=True)).to_be_visible()
                expect(page.locator("#provider-rows tr").first).to_contain_text("230")
                steps.append("Stage -> read-only validate -> activate, balance230 and monthlylimit180")
                page.get_by_role("button",name="Отключить",exact=True).click()
                expect(page.get_by_text("Активный ключ",exact=True)).to_have_count(0)
                steps.append("Disable updates actual stored configuration")
                page.get_by_label("Провайдер",exact=True).select_option("tavily")
                page.get_by_label("Ключ API",exact=True).fill("synthetic-invalid-prefix-key-111111")
                page.get_by_role("button",name="Сохранить ключ",exact=True).click()
                expect(page.locator("#form-error")).to_be_visible()
                expect(page.get_by_label("Ключ API",exact=True)).to_have_value("")
                steps.append("Invalid key shows safe error; password field cleared")
                page.get_by_label("Провайдер",exact=True).select_option("serper")
                page.get_by_label("Ключ API",exact=True).fill("synthetic-serper-browser-key-111111")
                page.get_by_role("button",name="Сохранить ключ",exact=True).click()
                expect(page.locator(".credential")).to_have_count(2)
                serper=page.locator(".credential").filter(has=page.get_by_text("Serper",exact=True))
                serper.get_by_role("button",name="Проверить квоту",exact=True).click()
                expect(serper).to_contain_text("Остаток провайдера не подтверждён")
                serper.get_by_role("button",name="Подключить",exact=True).click()
                expect(page.locator("#notice")).to_contain_text("нет действующего одобренного бюджета")
                assert calls==["serpapi"],calls
                steps.append("Serper balance stays unknown, no quota API call, activation without grant409")
                page.get_by_role("button",name="Обновить данные",exact=True).click()
                expect(page.locator("#notice")).to_contain_text("Данные обновлены")
                expect(page.locator("#monthly-limit")).to_have_text("180")
                expect(page.locator("#monthly-used")).to_have_text("26")
                for width in (1440,1024,768,390,320):
                    page.set_viewport_size({"width":width,"height":1000})
                    expect(page.get_by_role("heading",name="Пул поиска",exact=True)).to_be_visible()
                    page.screenshot(path=str(output/f"pool-{width}.png"),full_page=True)
                    metric=page.evaluate("""() => ({width:innerWidth,documentWidth:document.documentElement.scrollWidth,
                       bodyWidth:document.body.scrollWidth,text:document.body.innerText,
                       controls:[...document.querySelectorAll('button,input,select')].map(e=>({label:e.textContent||e.id,width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height}))})""")
                    assert metric["documentWidth"]<=width,metric
                    assert SYNTHETIC_KEY not in metric["text"]
                    measurements.append(metric)
                page.set_viewport_size({"width":390,"height":1000})
                table=page.locator(".table-scroll")
                table.focus();page.keyboard.press("ArrowRight")
                expect(table).not_to_have_js_property("scrollLeft",0)
                table_scroll=table.evaluate("e => e.scrollLeft")
                page.get_by_label("Ключ API",exact=True).focus();page.keyboard.press("Tab")
                expect(page.get_by_role("button",name="Сохранить ключ",exact=True)).to_be_focused()
                focus=page.get_by_role("button",name="Сохранить ключ",exact=True).evaluate("e => ({outline:getComputedStyle(e).outlineWidth,color:getComputedStyle(e).outlineColor,focused:document.activeElement===e})")
                assert focus["outline"]=="3px" and focus["focused"],focus
                page.screenshot(path=str(output/"pool-keyboard-focus-390.png"),full_page=True)
                steps.append("Real Tab focus outline3px and keyboard horizontal-table scroll verified at390px")
                assert page.evaluate("() => localStorage.length+sessionStorage.length")==0
                context.close();browser.close()
        finally:
            server.shutdown();server.server_close();thread.join()
        with socket.socket() as check:
            check.settimeout(1);closed=check.connect_ex(("127.0.0.1",port))!=0
        assert closed
    assert not errors,errors
    assert not outbound,outbound
    result={"steps":steps,"keyboard_focus":focus,"keyboard_table_scroll":table_scroll,
            "console_errors":errors,"external_requests":outbound,"provider_calls":calls,
            "measurements":measurements,"teardown":{"server_closed":closed,"thread_alive":thread.is_alive(),"temp_removed":not Path(directory).exists()}}
    (output/"browser-result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({"steps":len(steps),"viewports":[row["width"] for row in measurements],"console_errors":errors,"teardown":result["teardown"]},ensure_ascii=False))


if __name__=="__main__":main()
