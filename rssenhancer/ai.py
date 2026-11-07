"""AI providers configured in config.toml.

Select ai.provider = "anthropic" | "gemini" | "openai" | "deepseek" | "local" | "rapid-mlx".
Credentials and model overrides live in [ai.anthropic], [ai.gemini], [ai.openai],
[ai.deepseek], and [ai.local]. The local and rapid-mlx providers share one endpoint;
rapid-mlx starts a local server only when needed and stops it after the poll.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import common
from .common import env, log, post_json


def call_anthropic(system, prompt, json_mode=True, max_tokens=4096):
    base = env("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if env("ANTHROPIC_API_KEY"):
        headers["x-api-key"] = env("ANTHROPIC_API_KEY")
    else:
        headers["Authorization"] = "Bearer " + env("ANTHROPIC_AUTH_TOKEN", "")
    r = post_json(base + "/v1/messages", headers, {
        "model": env("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    })
    return "".join(c.get("text", "") for c in r["content"] if c.get("type") == "text")


def call_gemini(system, prompt, json_mode=True, max_tokens=4096):
    key = env("GEMINI_API_KEY")
    model = env("GEMINI_MODEL", "gemini-2.5-flash")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }
    if json_mode:  # max_tokens is not sent: Gemini's default output limit is large
        body["generationConfig"] = {"responseMimeType": "application/json"}
    r = post_json(
        "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model,
        {"x-goog-api-key": key, "content-type": "application/json"}, body)
    return "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])


# name: (key env, base-url env, default base, model env, default model, supports json mode, send max_tokens)
COMPAT = {
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "https://api.openai.com/v1", "OPENAI_MODEL", "gpt-4o-mini", True, False),
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1", "DEEPSEEK_MODEL", "deepseek-chat", True, True),
}


def make_compat(name):
    keyvar, basevar, defbase, modelvar, defmodel, supports_json, send_limit = COMPAT[name]
    key = env(keyvar) if keyvar else None
    if keyvar and not key:
        sys.exit("%s is not set" % keyvar)

    def call(system, prompt, json_mode=True, max_tokens=4096):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        body = {
            "model": env(modelvar, defmodel),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        if supports_json and json_mode:
            body["response_format"] = {"type": "json_object"}
        if send_limit:
            body["max_tokens"] = min(max_tokens, 8192)  # deepseek-chat tops out at 8K
        r = post_json(env(basevar, defbase).rstrip("/") + "/chat/completions", headers, body)
        return r["choices"][0]["message"]["content"] or ""

    return call


# One endpoint for every local model server. Whatever another agent (or you) already runs there is
# reused, whichever tool started it. AI_PROVIDER=rapid-mlx additionally starts rapid-mlx there when
# nothing answers; AI_PROVIDER=local never starts anything.
LOCAL_URL = (env("LOCAL_BASE_URL") or "http://127.0.0.1:8000/v1").rstrip("/")
LOCAL_MODEL = env("LOCAL_MODEL", "mlx-community/Qwen3.6-35B-A3B-5bit")
_local_url = urlparse(LOCAL_URL)
LOCAL_HOST = _local_url.hostname or "127.0.0.1"
LOCAL_PORT = _local_url.port or (443 if _local_url.scheme == "https" else 80)
RAPID_STOP_AFTER_POLL = bool(env("RAPID_MLX_STOP_AFTER_POLL", True))
RAPID_STARTUP_SECONDS = int(env("RAPID_MLX_STARTUP_SECONDS", "900"))  # first run downloads the model
_rapid_proc = None
_rapid_failed = False
_local_model = LOCAL_MODEL  # model id actually sent (may be adopted from an already-running server)
_local_noted = False


def find_rapid_bin():
    """launchd has a minimal PATH, so look in the usual install locations too."""
    if env("RAPID_MLX_BIN"):
        return env("RAPID_MLX_BIN")
    home = Path.home()
    for cand in (shutil.which("rapid-mlx"), "/opt/homebrew/bin/rapid-mlx", "/usr/local/bin/rapid-mlx",
                 str(home / ".rapid-mlx/bin/rapid-mlx"), str(home / ".local/bin/rapid-mlx")):
        if cand and os.access(cand, os.X_OK):
            return cand
    return None


def local_probe():
    """Look at what answers at LOCAL_BASE_URL. Returns (state, model_ids):
    'down'    nothing is listening
    'up'      an OpenAI-compatible server (model_ids = what it serves; empty if auth hides the list)
    'foreign' something answers, but it is not an OpenAI-compatible /models endpoint"""
    req = urllib.request.Request(LOCAL_URL + "/models")
    if env("LOCAL_API_KEY"):
        req.add_header("Authorization", "Bearer " + env("LOCAL_API_KEY"))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        return "up", [m["id"] for m in data["data"]]
    except urllib.error.HTTPError as e:
        return ("up", []) if e.code in (401, 403) else ("foreign", [])
    except (ValueError, KeyError, TypeError):
        return "foreign", []
    except Exception:
        return "down", []


def local_up():
    return local_probe()[0] == "up"


def same_model(a, b):
    a, b = a.lower(), b.lower()
    return a == b or a.rsplit("/", 1)[-1] == b.rsplit("/", 1)[-1]


def stop_rapid_mlx():
    """Stop the server only if this script started it."""
    global _rapid_proc
    p, _rapid_proc = _rapid_proc, None
    if p and p.poll() is None:
        log("stopping rapid-mlx", tag="ai")
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def ensure_local(autostart):
    global _rapid_proc, _rapid_failed, _local_model, _local_noted
    state, ids = local_probe()
    if state == "foreign":
        raise RuntimeError("%s answers, but not like an OpenAI-compatible server; check LOCAL_BASE_URL" % LOCAL_URL)
    if state == "up":
        if (_rapid_proc is not None and _rapid_proc.poll() is None) or not ids:
            _local_model = LOCAL_MODEL  # our own server, or an auth-protected one we cannot list
        else:
            match = next((i for i in ids if same_model(i, LOCAL_MODEL)), None)
            if match:
                _local_model = match
            elif env("LOCAL_ON_MISMATCH", "use").lower() == "fail":
                raise RuntimeError("%s is serving %s, not %s (LOCAL_ON_MISMATCH=fail)"
                                   % (LOCAL_URL, ", ".join(ids), LOCAL_MODEL))
            else:
                _local_model = ids[0]
                if not _local_noted:
                    log("%s is already serving %s; using it instead of %s" % (LOCAL_URL, ids[0], LOCAL_MODEL), tag="ai")
                    _local_noted = True
        return
    if not autostart:
        raise RuntimeError("no model server answering at %s (AI_PROVIDER=local never starts one; "
                           "use AI_PROVIDER=rapid-mlx to auto-start)" % LOCAL_URL)
    if LOCAL_HOST not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("rapid-mlx can only be auto-started on this machine, but LOCAL_BASE_URL points to %s" % LOCAL_HOST)
    if _rapid_failed:
        raise RuntimeError("rapid-mlx failed to start earlier this poll; will retry next poll")
    if _rapid_proc is None or _rapid_proc.poll() is not None:
        binary = find_rapid_bin()
        if not binary:
            raise RuntimeError("rapid-mlx not found (brew install rapid-mlx, or set RAPID_MLX_BIN)")
        cmd = [binary, "serve", LOCAL_MODEL, "--host", LOCAL_HOST, "--port", str(LOCAL_PORT)]
        cmd += list(env("RAPID_MLX_ARGS", []) or [])
        log("starting:", " ".join(cmd), tag="ai")
        common.LOG_DIR.mkdir(parents=True, exist_ok=True)
        logf = open(common.LOG_DIR / "rapid-mlx.log", "ab")
        _rapid_proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + RAPID_STARTUP_SECONDS
    while time.time() < deadline:
        if _rapid_proc.poll() is not None:
            _rapid_failed = True
            raise RuntimeError("rapid-mlx exited with code %s (is port %d busy?), see %s"
                               % (_rapid_proc.returncode, LOCAL_PORT, common.LOG_DIR / "rapid-mlx.log"))
        st, _ = local_probe()
        if st == "up":
            _local_model = LOCAL_MODEL
            log("rapid-mlx is ready", tag="ai")
            return
        if st == "foreign":
            _rapid_failed = True
            stop_rapid_mlx()
            raise RuntimeError("%s was taken by another service while rapid-mlx was starting" % LOCAL_URL)
        time.sleep(2)
    _rapid_failed = True
    stop_rapid_mlx()
    raise RuntimeError("rapid-mlx did not become ready within %ds" % RAPID_STARTUP_SECONDS)


def rapid_poll_done():
    global _rapid_failed
    _rapid_failed = False
    if RAPID_STOP_AFTER_POLL:
        stop_rapid_mlx()


def make_local(autostart):
    if autostart and not find_rapid_bin() and not local_up():
        sys.exit("rapid-mlx not found. Install it (brew install rapid-mlx) or set RAPID_MLX_BIN.")

    def call(system, prompt, json_mode=True, max_tokens=4096):
        ensure_local(autostart)  # lazy: nothing is started until an email actually needs the model
        headers = {"Content-Type": "application/json"}
        if env("LOCAL_API_KEY"):
            headers["Authorization"] = "Bearer " + env("LOCAL_API_KEY")
        body = {
            "model": _local_model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        body.update(env("LOCAL_EXTRA_BODY", {}) or {})
        r = post_json(LOCAL_URL + "/chat/completions", headers, body, timeout=900)
        return r["choices"][0]["message"].get("content") or ""

    return call


def get_provider():
    name = (env("AI_PROVIDER") or "").lower()
    if not name:
        if env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN"):
            name = "anthropic"
        elif env("GEMINI_API_KEY"):
            name = "gemini"
        elif env("OPENAI_API_KEY"):
            name = "openai"
        elif env("DEEPSEEK_API_KEY"):
            name = "deepseek"
        elif find_rapid_bin() or local_up():
            name = "rapid-mlx"
        else:
            sys.exit("No AI key found. Set AI_PROVIDER plus ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / "
                     "GEMINI_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY (or AI_PROVIDER=local|rapid-mlx).")
    if name == "rapid-mlx":
        return name, make_local(True)
    if name == "local":
        return name, make_local(False)
    if name == "anthropic":
        if not (env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN")):
            sys.exit("ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is not set")
        return name, call_anthropic
    if name == "gemini":
        if not env("GEMINI_API_KEY"):
            sys.exit("GEMINI_API_KEY is not set")
        return name, call_gemini
    if name in COMPAT:
        return name, make_compat(name)
    sys.exit("Unknown AI_PROVIDER %r" % name)



def extract_json(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)  # reasoning models (Qwen3.x)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text[text.find("{"): text.rfind("}") + 1])
