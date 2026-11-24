"""zen-bridge: OpenAI-compatible front end for OpenCode Zen's free tier.

Why this exists
---------------
Zen's free models only answer the genuine opencode client (direct
OpenAI-compatible calls get `403 FreeTierError: free tier can only be used
from within OpenCode`). This shim translates OpenAI-compat requests into
opencode calls.

Why a pool of warm servers (2026-10-06)
---------------------------------------
The first implementation shelled out to `opencode run` per request. That
works, but every call pays opencode's own startup: measured ~5s fixed
(1s process start + ~4s init), so a 1s model took 6s+. Reusing the data dir
does not help -- the cost is opencode's init, not migrations.

Running `opencode serve` and talking to it over the SDK removes that cost
(measured 1.0-1.8s warm). But one server cannot serve several models: it
latches onto the first model it resolves and ignores `model_id` on every
later request (verified: a fresh server asked for space-bunny then
big-pickle served space-bunny both times). So we run ONE SERVER PER MODEL,
each pinned via its own generated opencode.json (`"model": "opencode/<id>"`).
Requests route to the server pinned to the model asked for.

Servers are started lazily and kept warm, with LRU/TTL eviction, because
each one costs 500-900 MB RSS.

If a server cannot be started we fall back to the one-shot CLI so the
bridge degrades instead of failing.

Env
---
BRIDGE_PORT            HTTP port for this shim            (8099)
BRIDGE_MAX_WARM        warm servers kept alive            (3)
BRIDGE_IDLE_TTL        seconds before an idle server dies (1800)
BRIDGE_MAX_CONCURRENT  in-flight model calls              (2)
BRIDGE_RUN_TIMEOUT     per-request budget, seconds        (60)
BRIDGE_SERVER_BOOT     seconds to wait for a server's /health (40)
BRIDGE_CONFIG_HOME     where per-model configs are written
BRIDGE_WORK_ROOT       scratch dir
BRIDGE_DISABLE_POOL    1 = always use the one-shot CLI (escape hatch)
"""
import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("BRIDGE_PORT", "8099"))
MAX_WARM = int(os.environ.get("BRIDGE_MAX_WARM", "3"))
IDLE_TTL = int(os.environ.get("BRIDGE_IDLE_TTL", "1800"))
MAX_CONCURRENT = int(os.environ.get("BRIDGE_MAX_CONCURRENT", "2"))
RUN_TIMEOUT = int(os.environ.get("BRIDGE_RUN_TIMEOUT", "60"))
SERVER_BOOT = int(os.environ.get("BRIDGE_SERVER_BOOT", "40"))
SEM_WAIT = int(os.environ.get("BRIDGE_SEM_WAIT", "90"))
CONFIG_HOME = os.environ.get("BRIDGE_CONFIG_HOME", "/tmp/ocbridge")
WORK_ROOT = os.environ.get("BRIDGE_WORK_ROOT", "/tmp/bridge-work")
PORT_BASE = int(os.environ.get("BRIDGE_PORT_BASE", "8200"))
DISABLE_POOL = os.environ.get("BRIDGE_DISABLE_POOL", "") not in ("", "0")
# Reported by /health. Zen's free tier is gated on the genuine client, so when
# every free id starts returning errors this is the first value to compare
# with the harness and with the latest published @opencode/cli.
CLI_VERSION = os.environ.get("OPENCODE_CLI_VERSION", "unknown")

# Zen free-tier ids are NOT hardcoded: the allowlist is derived from Zen's own
# catalog (https://opencode.ai/zen/v1/models) filtered on "free", refreshed
# every BRIDGE_ALLOW_REFRESH seconds. Hardcoding it was a real trap: on
# 2026-10-07 Zen advertised 12 free models, the static set had 8, and nothing
# in model-gardener could ever add the other 4 -- a ripe candidate could be
# approved by the brain and still be unservable, because line 377 rejects
# anything not listed here.
#
# The allowlist answers "is it worth ATTEMPTING", not "does it work": the pool
# is lazy, so an allowlisted-but-dead id costs nothing until it is called, and
# the gardener probe is what decides that. Superseded versions are kept on
# purpose (e.g. muse-spark-1.2 AND 1.3): Zen rotates lineups weekly and the
# older id often outlives the newer one on the free tier.
ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
ALLOWLIST_REFRESH = int(os.environ.get("BRIDGE_ALLOW_REFRESH", "900"))

# Free ids that do not contain the string "free" (Zen's naming is not
# consistent). Verified free on 2026-10-06.
ALLOWLIST_EXTRA = {"big-pickle", "nemotron-3-ultra-free"}
# nemotron-3-ultra-free (2026-10-22): raw catalogue dropped it, but the
# @opencode/cli@2.1.2 roster still lists it and it answers -- first verified
# catalogue-vs-client divergence: after jumping to the 2.x client, the client
# roster is the source of truth for "still servable", not /(zen/)v1/models.

ALLOWLIST = set(ALLOWLIST_EXTRA)   # mutated in place, never rebound
_allow_lock = threading.Lock()
_allow_checked = 0.0


def _fetch_zen_free():
    # A User-Agent is mandatory: Zen's edge answers 403 to urllib's default
    # "Python-urllib/3.x" (verified 2026-10-07) while the discoverer's
    # requests-based fetch returns 200. Without this the allowlist would stay
    # at ALLOWLIST_EXTRA and every Zen group would be rejected.
    req = urllib.request.Request(
        ZEN_MODELS_URL,
        headers={"Accept": "application/json",
                 "User-Agent": "llm-zen-bridge/1.0 (+model-gardener)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", "replace")).get("data") or []
    return {m["id"] for m in data
            if isinstance(m, dict) and m.get("id") and "free" in m["id"].lower()}


def refresh_allowlist(force=False):
    """Re-derive the allowlist from Zen. Never raises: on failure the last
    known good set is kept, because a transient DNS blip must not empty the
    allowlist and take every Zen group down with it."""
    global _allow_checked
    now = time.time()
    if not force and (now - _allow_checked) < ALLOWLIST_REFRESH:
        return sorted(ALLOWLIST)
    try:
        found = _fetch_zen_free()
    except Exception as e:
        log_event("allowlist_refresh_failed", error=type(e).__name__)
        return sorted(ALLOWLIST)
    with _allow_lock:
        before = set(ALLOWLIST)
        ALLOWLIST.clear()
        ALLOWLIST.update(found | ALLOWLIST_EXTRA)
        _allow_checked = now
        added, gone = sorted(ALLOWLIST - before), sorted(before - ALLOWLIST)
    if added or gone:
        log_event("allowlist_refreshed", total=len(ALLOWLIST),
                  added=added, removed=gone)
    return sorted(ALLOWLIST)


def model_allowed(model):
    """Membership test that self-heals: an unknown id triggers a refresh if the
    cache is stale, so a model published minutes ago is servable immediately."""
    if model in ALLOWLIST:
        return True
    refresh_allowlist()
    return model in ALLOWLIST


def _allowlist_loop():
    # Reap far more often than the allowlist refresh: a leak should be caught
    # in minutes, not after a 15-minute refresh window has already compounded.
    last_reap = 0.0
    while True:
        time.sleep(60)
        if time.time() - last_reap > 300:
            last_reap = time.time()
            try:
                reap_orphans("periodic")
            except Exception:
                pass
        try:
            refresh_allowlist(force=True)
        except Exception:
            pass

# opencode refuses a config with top-level `"tool": "deny"` and silently
# degrades (dropping the Zen attestation -> 403). And a bash block whose
# only rule is "*" also fails to parse. Both verified by A/B testing; see
# AGENTS.md. Never add top-level denies to this file's generated configs.
PERMISSION = {"bash": {"*": "deny", "rm *": "deny", "sudo *": "deny",
                       "curl *": "deny", "wget *": "deny", "docker *": "deny"}}

SEMAPHORE = threading.Semaphore(MAX_CONCURRENT)
_pool_lock = threading.Lock()
_pool = {}          # model -> dict(port, proc, session, client, last_used, cwd)
_next_port = [PORT_BASE]


def log_event(event, **kw):
    rec = {"ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
           "event": event, **kw}
    print(json.dumps(rec), flush=True)


def to_prompt(messages):
    parts = []
    for m in messages or []:
        role = m.get("role", "user")
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        parts.append(f"{role}: {c}")
    return "\n".join(parts)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- pool ----

def write_pinned_config(model, root):
    d = os.path.join(root, "opencode")
    os.makedirs(d, exist_ok=True)
    cfg = {"$schema": "https://opencode.ai/config.json",
           "model": f"opencode/{model}",
           "permission": PERMISSION}
    with open(os.path.join(d, "opencode.json"), "w") as f:
        json.dump(cfg, f, indent=2)
    return root


def spawn_server(model):
    """Start a config-pinned opencode server for `model`. Returns entry."""
    from opencode_ai import Opencode  # imported lazily: CLI-only mode skips it

    cwd = os.path.join(WORK_ROOT, "srv-" + model.replace("/", "_"))
    os.makedirs(cwd, exist_ok=True)
    xdg = write_pinned_config(model, os.path.join(cwd, "config"))
    port = _next_port[0]
    _next_port[0] += 1
    # 2.x writes service state under XDG_STATE_HOME (service.json); without
    # this it falls back to $HOME/.local/state on the read-only rootfs and
    # the child dies at boot (PlatformError writeFile, verified 2026-10-22).
    env = dict(os.environ, XDG_CONFIG_HOME=xdg,
               XDG_DATA_HOME=os.path.join(cwd, "data"),
               XDG_STATE_HOME=os.path.join(cwd, "state"))
    env.pop("OPENCODE_SERVER_PASSWORD", None)
    proc = subprocess.Popen(
        ["opencode", "serve", "--port", str(port), "--hostname", "127.0.0.1"],
        cwd=cwd, env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + SERVER_BOOT
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited rc={proc.returncode}")
        try:
            import urllib.request
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.4)
    else:
        proc.terminate()
        raise RuntimeError("server did not become healthy in time")

    client = Opencode(base_url=base, max_retries=1)
    sess = client.session.create()
    entry = {"port": port, "proc": proc, "client": client,
             "session": sess.id, "last_used": time.time(), "cwd": cwd}
    log_event("spawn", model=model, port=port, session=sess.id)
    _warm(entry, model)
    return entry


def _warm(entry, model):
    """Fire-and-forget first call so the *user's* request doesn't pay the
    cold start (measured 6.0s cold vs 1.8s warm)."""
    def go():
        try:
            entry["client"].session.chat(
                entry["session"], model_id=model, provider_id="opencode",
                mode="build",
                parts=[{"type": "text", "text": "hi"}], timeout=45)
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def _kill_group(proc, why, grace=4.0):
    """SIGTERM the whole process group, then SIGKILL whatever survives, then
    actually verify.

    The leak this fixes (2026-10-10): `opencode serve` did not always die on
    SIGTERM, and two code paths dropped pool entries without signalling the
    process at all -- so each occurrence left a ~700MB orphan running forever.
    The host reached 98% RAM with a crowd of opencode processes and needed a
    reboot. The pool dict was bounded; the OS processes were not.
    """
    def _alive(p):
        try:
            return p.poll() is None
        except Exception:
            return False

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except Exception:
            try:
                proc.send_signal(sig)
            except Exception:
                return False
        deadline = time.time() + (grace if sig == signal.SIGTERM else 3.0)
        while time.time() < deadline:
            if not _alive(proc):
                return True
            time.sleep(0.1)
    return not _alive(proc)


def reap_orphans(reason="periodic"):
    """Kill any `opencode serve` we started that is NOT a live pool member.

    The pool dict was bounded but the OS processes were not, and two separate
    code paths could drop an entry without signalling its process. That is how
    25 untracked ~700MB servers accumulated and took the host to 98% RAM. This
    is the backstop: it does not care how an orphan appeared, only that it is
    ours and untracked. Kills only direct children, never anything else.
    """
    with _pool_lock:
        keep = {e["proc"].pid for e in _pool.values()
                if e["proc"] and e["proc"].poll() is None}
    me = os.getpid()
    killed = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        p = int(pid)
        if p == me or p in keep:
            continue
        try:
            with open(f"/proc/{p}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        if "opencode" not in cmd or "serve" not in cmd:
            continue
        try:
            with open(f"/proc/{p}/stat") as f:
                ppid = int(f.read().split(") ", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        if ppid != me:               # only our own children
            continue
        try:
            os.killpg(os.getpgid(p), signal.SIGKILL)
        except Exception:
            try:
                os.kill(p, signal.SIGKILL)
            except Exception:
                continue
        killed.append(p)
    if killed:
        log_event("reaped_orphans", reason=reason, pids=killed,
                  killed=len(killed))
    return killed


def stop_entry(model, entry, why):
    died = _kill_group(entry["proc"], why)
    # LEAK FIX (2026-10-09): same tmpfs, same disease. The srv-<model> dir holds
    # that server's opencode data dir; killing the process does not remove it,
    # so every eviction left a directory behind.
    cwd = entry.get("cwd")
    if cwd:
        shutil.rmtree(cwd, ignore_errors=True)
    log_event("evict", model=model, port=entry.get("port"), why=why,
              killed=died)


def evict_locked(exclude=None):
    """LRU/TTL eviction, caller holds _pool_lock. Returns models evicted."""
    now = time.time()
    evicted = []
    for m, e in list(_pool.items()):
        if m == exclude:
            continue
        if now - e["last_used"] > IDLE_TTL:
            stop_entry(m, e, f"idle>{IDLE_TTL}s")
            _pool.pop(m, None)
            evicted.append(m)
    while len(_pool) >= MAX_WARM:
        if not _pool:
            break
        oldest = min(_pool, key=lambda m: _pool[m]["last_used"])
        if oldest == exclude:
            break
        stop_entry(oldest, _pool[oldest], "lru")
        _pool.pop(oldest, None)
        evicted.append(oldest)
    return evicted


def get_entry(model):
    with _pool_lock:
        e = _pool.get(model)
        if e is not None and e["proc"].poll() is None:
            e["last_used"] = time.time()
            return e, None
        if e is not None:
            _pool.pop(model, None)   # already dead; nothing to signal
    try:
        entry = spawn_server(model)
    except Exception as ex:
        return None, str(ex)
    with _pool_lock:
        existing = _pool.get(model)
        if existing is not None and existing["proc"].poll() is None:
            stop_entry(model, entry, "duplicate")
            existing["last_used"] = time.time()
            return existing, None
        evict_locked(exclude=model)
        _pool[model] = entry
    return entry, None


def pool_status():
    with _pool_lock:
        now = time.time()
        return {m: {"port": e["port"],
                    "idle_s": int(now - e["last_used"]),
                    "warm": e["proc"].poll() is None}
                for m, e in _pool.items()}


# ------------------------------------------------------------- engines ----

def call_server(entry, model, prompt):
    from opencode_ai.types import TextPartInputParam
    r = entry["client"].session.chat(
        entry["session"], model_id=model, provider_id="opencode", mode="build",
        parts=[TextPartInputParam(type="text", text=prompt)],
        timeout=RUN_TIMEOUT)
    texts = [p.get("text", "") for p in (r.parts or [])
             if isinstance(p, dict) and p.get("type") == "text"]
    t = (r.info or {}).get("tokens") or {}
    usage = {"prompt_tokens": t.get("input", 0),
             "completion_tokens": t.get("output", 0),
             "total_tokens": t.get("total", 0)}
    served = (r.info or {}).get("modelID")
    return "\n".join(texts), usage, "" if texts else f"no text (served={served})"


def _pick_free_port():
    """Ephemeral free port (bind(0) + close): unique per-child managed service."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def call_cli(model, prompt):
    """One-shot `opencode run`: the always-works fallback (~5s startup)."""
    p = None
    # 2026-10-22: the 2.x client starts a "managed service" per run on a
    # deterministic port (49374). Two overlapping children collide ("Managed
    # service port ... is already in use") -- so: always reap the process
    # group when the run ends, and retry once if the next child bound while
    # the previous group's listening socket was still dying.
    for attempt in (0, 1):
        workdir = os.path.join(WORK_ROOT, "cli-" + uuid.uuid4().hex[:12])
        os.makedirs(workdir, exist_ok=True)
        xdg = write_pinned_config(model, os.path.join(workdir, "config"))
        env = dict(os.environ, XDG_CONFIG_HOME=xdg,
                   XDG_DATA_HOME=os.path.join(workdir, "data"),
                   XDG_STATE_HOME=os.path.join(workdir, "state"))
        # Unique managed-service port per child: without this every child
        # binds the deterministic default (49374) and overlapping children
        # kill each other at boot ("Managed service port ... already in use").
        try:
            port = _pick_free_port()
            subprocess.run(["opencode", "service", "set", "port", str(port)],
                           cwd=workdir, env=env, capture_output=True,
                           text=True, timeout=10)
        except Exception:
            pass
        try:
            # Popen + killpg rather than subprocess.run(timeout=...): run() kills
            # only the direct child, and with start_new_session=True any descendant
            # it spawned used to survive the timeout as an orphan.
            #
            # The prompt goes on STDIN, never in argv (2026-11-24). `opencode run`
            # takes the message from stdin when no positional argument is given,
            # and Linux caps a single argv entry at MAX_ARG_STRLEN (128 KiB):
            # passing the prompt as an argument made every conversation larger
            # than ~128 KB raise `OSError: [Errno 7] Argument list too long`,
            # which escaped do_POST uncaught and reached the client as an empty
            # reply (litellm logs it as 500 InternalServerError / Connection
            # error). A real agent session is hundreds of KB, so this bit hard
            # on exactly the sessions people care about.
            try:
                proc = subprocess.Popen(
                    ["opencode", "run", "-m", f"opencode/{model}",
                     "--format", "json"],
                    cwd=workdir, env=env, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=True)
            except OSError as e:
                # Never let a spawn failure escape as an empty reply: hand the
                # caller a real error string so do_POST can send a 502.
                return "", {}, f"spawn failed: {type(e).__name__}: {e}"
            try:
                out, errout = proc.communicate(input=prompt,
                                               timeout=RUN_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                return "", {}, f"cli timeout after {RUN_TIMEOUT}s (group killed)"
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            if (proc.returncode and attempt == 0
                    and "Managed service port" in (errout or "")):
                time.sleep(2.0)
                continue
            p = subprocess.CompletedProcess(proc.args, proc.returncode,
                                            out, errout)
            break
        finally:
            # LEAK FIX (2026-10-09): this workdir holds a full opencode XDG_DATA_HOME
            # and used to survive the call. /tmp is a 512MB tmpfs, so the bridge
            # slowly filled itself and then every request died with
            # OSError 28 "No space left on device" -- health still said ok.
            shutil.rmtree(workdir, ignore_errors=True)
    if p is None:
        return "", {}, "cli spawn failed twice (managed-service port race)"
    texts, usage = [], {}
    for line in (p.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "text" and isinstance(ev.get("part"), dict):
            t = ev["part"].get("text")
            if t:
                texts.append(t)
        if ev.get("type") == "step_finish" and isinstance(ev.get("part"), dict):
            u = ev["part"].get("tokens") or {}
            usage = {"prompt_tokens": u.get("input", 0),
                     "completion_tokens": u.get("output", 0),
                     "total_tokens": u.get("total", 0)}
    if not texts:
        # Keep the HEAD of stderr, not the tail. opencode prints a JSON error
        # envelope and the useful part (name/message/ref) is at the START; a
        # tail slice showed "...imestamp\":1791...\"error\":{\"name\":\"Unknown
        # Error\"...}" with the cause cut off (verified 2026-10-07 on
        # exo-free, fledge-alpha-free, jev-1.13-free, muse-spark-1.2).
        return "", {}, ((p.stderr or p.stdout or "")[:600] or "empty")
    return "\n".join(texts), usage, ""


def run_model(model, prompt):
    """-> (text, usage, error). Pool first, CLI as fallback."""
    if not DISABLE_POOL:
        if not SEMAPHORE.acquire(timeout=SEM_WAIT):
            return "", {}, f"bridge busy (waited {SEM_WAIT}s)"
        try:
            entry, err = get_entry(model)
            if entry is not None:
                try:
                    return call_server(entry, model, prompt)
                except Exception as e:
                    log_event("server_call_failed", model=model,
                              error=f"{type(e).__name__}: {e}"[:180])
                    # Drop the entry AND kill its process. Popping alone left a
                    # live ~700MB opencode server nobody tracked; 25 of these
                    # is ~17GB, which is how the host hit 98% RAM.
                    stop_entry(model, entry, "call-failed")
            else:
                log_event("spawn_failed", model=model, error=str(err)[:180])
        finally:
            SEMAPHORE.release()
    return call_cli(model, prompt)


# -------------------------------------------------------------- server ----

class Handler(BaseHTTPRequestHandler):
    server_version = "zen-bridge/2"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client gave up (probe timeout): normal, not worth a traceback

    def do_GET(self):
        if self.path in ("/livez", "/healthz"):
            # Deliberately does NOT refresh the allowlist: this is the
            # liveness/readiness probe, and a slow Zen fetch must never get
            # the pod killed or removed from the Service. /health is the
            # diagnostic endpoint that may block briefly.
            return self._send(200, {"ok": True})
        if self.path == "/health":
            models = refresh_allowlist()
            checked = _allow_checked
            return self._send(200, {
                "ok": True, "cli": CLI_VERSION,
                "models": models,
                "allowlist_refresh": ALLOWLIST_REFRESH,
                "allowlist_checked": (
                    datetime.fromtimestamp(checked, timezone.utc)
                    .isoformat(timespec="seconds") if checked else None),
                "pool": pool_status(), "max_warm": MAX_WARM,
                "idle_ttl": IDLE_TTL, "pool_disabled": DISABLE_POOL})
        if self.path in ("/v1/models", "/models"):
            return self._send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "zen-bridge"}
                for m in sorted(ALLOWLIST)]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            return self._send(404, {"error": "not found"})
        # Read EXACTLY Content-Length bytes. json.load(self.rfile) calls
        # read() with no size and blocks until the client hangs up: a
        # deadlock that made every probe read as `hung` (2026-10-05).
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "bad Content-Length"})
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        model = body.get("model", "")
        if not model_allowed(model):
            return self._send(404, {"error": f"model not bridged: {model}"})
        t0 = time.time()
        text, usage, err = run_model(model, to_prompt(body.get("messages")))
        dt = time.time() - t0
        log_event("call", model=model, seconds=round(dt, 1),
                  ok=bool(text), error=err[:160] if err else "")
        if err and not text:
            return self._send(502, {"error": f"upstream failed: {err}"})
        if body.get("stream"):
            return self._sse(model, text, usage)
        return self._send(200, {
            "id": "chatcmpl-bridge", "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0,
                               "total_tokens": 0}})

    def _sse(self, model, text, usage):
        # Deltas are whole-answer shaped, not token-stream: the agent loop
        # answers in one piece. Shape-compatible, not latency-equivalent.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        mid = f"chatcmpl-{uuid.uuid4().hex[:8]}"
        for chunk in (text[i:i + 200] for i in range(0, max(len(text), 1), 200)):
            self.wfile.write(("data: " + json.dumps({
                "id": mid, "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {"content": chunk},
                             "finish_reason": None}]}) + "\n\n").encode())
        self.wfile.write(("data: " + json.dumps({
            "id": mid, "object": "chat.completion.chunk", "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": usage}) + "\n\ndata: [DONE]\n\n").encode())


@atexit.register
def _cleanup():
    with _pool_lock:
        for m, e in list(_pool.items()):
            stop_entry(m, e, "shutdown")


def main():
    os.makedirs(WORK_ROOT, exist_ok=True)
    os.makedirs(CONFIG_HOME, exist_ok=True)
    reap_orphans("startup")     # in case a previous run left orphans behind
    threading.Thread(target=_allowlist_loop, daemon=True).start()
    log_event("start", models=refresh_allowlist(force=True), max_warm=MAX_WARM,
              idle_ttl=IDLE_TTL, pool_disabled=DISABLE_POOL,
              max_concurrent=MAX_CONCURRENT, run_timeout=RUN_TIMEOUT)
    if not DISABLE_POOL:
        try:
            import opencode_ai  # noqa: F401
        except Exception as e:
            # Loud on purpose: this once hid for a whole test round because
            # the downgrade to CLI still "worked", just 4x slower.
            log_event("sdk_missing", error=str(e)[:160], fatal=True,
                      note="POOL DISABLED, falling back to one-shot CLI "
                           "(~5s/call). Rebuild the image to fix.")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"zen-bridge listening on :{PORT} "
          f"(pool={'off' if DISABLE_POOL else f'{MAX_WARM} warm'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()