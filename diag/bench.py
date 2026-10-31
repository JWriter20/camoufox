"""Stock Firefox getter microbench: outerWidth vs controls, under env/pref variants.
usage: bench.py FIREFOX VARIANT [VARIANT...]   (variant names below)"""
import http.server, json, os, shutil, subprocess, sys, tempfile, threading, time
EXPRS = ["outerWidth", "outerHeight", "innerWidth", "screenX", "screen.orientation.type", "navigator.onLine"]
PAGE = """<!doctype html><script>
function bench(e){const f=new Function('N','let t=performance.now();let x;for(let i=0;i<N;i++){x=('+e+');}return performance.now()-t;');
const g=new Function('N','let t=performance.now();let x;for(let i=0;i<N;i++){x=i;}return performance.now()-t;');
let N=100;while(N<5e6&&f(N)<25)N*=4;let b=1e9,c=1e9;for(let r=0;r<9;r++){b=Math.min(b,f(N));c=Math.min(c,g(N));}return Math.max(0,b-c)*1e6/N;}
setTimeout(()=>{const o={};for(const e of %s)o[e]=bench(e);o.dpr=devicePixelRatio;o.ow=outerWidth;o.iw=innerWidth;
fetch('/r',{method:'POST',body:JSON.stringify(o)});},%d);
</script>"""
VARIANTS = {
  "base": ({}, {}),
  "nodisplay": ({}, {"DISPLAY": None, "WAYLAND_DISPLAY": None, "XDG_SESSION_TYPE": None, "DBUS_SESSION_BUS_ADDRESS": None}),
  "dpp1": ({"layout.css.devPixelsPerPx": '"1"'}, {}),
  "dpp15": ({"layout.css.devPixelsPerPx": '"1.5"'}, {}),
  "oszoom0": ({"browser.display.os-zoom-behavior": "0"}, {}),
  "fppoff": ({"privacy.fingerprintingProtection": "false", "privacy.baselineFingerprintingProtection": "false"}, {}),
  "fppon": ({"privacy.fingerprintingProtection": "true"}, {}),
  "late": ({}, {}),  # benchmark 20 s after load instead of at once
}
class S(http.server.ThreadingHTTPServer): res = None
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        delay = 20000 if self.path.startswith("/late") else 500
        b = (PAGE % (json.dumps(EXPRS), delay)).encode(); self.send_response(200)
        self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        self.server.res = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); self.send_response(204); self.end_headers()
def run(binary, name):
    prefs, envd = VARIANTS[name]
    srv = S(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    prof = tempfile.mkdtemp(prefix="owbench-")
    base = {"app.update.enabled": "false", "browser.shell.checkDefaultBrowser": "false",
            "datareporting.policy.dataSubmissionEnabled": "false", "toolkit.telemetry.reportingpolicy.firstRun": "false",
            "browser.aboutwelcome.enabled": "false"}
    open(os.path.join(prof, "user.js"), "w").write("".join(f'user_pref("{k}", {v});\n' for k, v in {**base, **prefs}.items()))
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    for k, v in envd.items():
        if v is None: env.pop(k, None)
        else: env[k] = v
    url = f"http://127.0.0.1:{srv.server_address[1]}/{'late' if name == 'late' else 'x'}"
    p = subprocess.Popen([binary, "--headless", "--no-remote", "--profile", prof, url], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t = time.time()
    while srv.res is None and time.time() - t < 120: time.sleep(0.3)
    subprocess.run(["pkill", "-9", "-f", prof]); p.wait(); shutil.rmtree(prof, ignore_errors=True); srv.shutdown()
    return srv.res
for name in sys.argv[2:]:
    r = run(sys.argv[1], name)
    if not r: print(name, "NO RESULT"); continue
    c = r["screen.orientation.type"]
    print(f"{name:10s} " + " ".join(f"{e.split('.')[-1][:11]}={r[e]:.0f}" for e in EXPRS) + f"  ow/ctl={r['outerWidth']/c:.2f} dpr={r['dpr']} ow={r['ow']} iw={r['iw']}", flush=True)
