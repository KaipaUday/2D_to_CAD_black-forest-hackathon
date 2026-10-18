#!/usr/bin/env python3
"""Local web/API server for the drawing review workflow.

Browser -> this server -> OpenRouter (model with tool calling + vision)
                       -> drawing2cad MCP server (stdio: Docker or local Python; or Streamable HTTP)

The server runs the agent loop: it sends the drawing2cad tools to the model, executes
every tool call the model makes on the MCP server, and stops after export_step (or when
the cost limit from config.json is reached). The OpenRouter key comes from the
OPENROUTER_API_KEY environment variable; it never reaches the browser.

    echo "OPENROUTER_API_KEY=sk-or-..." > .env   # or export it in the shell
    python3 server.py            # http://127.0.0.1:8765
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import base64, json, mimetypes, os, queue, re, shlex, subprocess, threading, time, uuid
import urllib.request, urllib.error

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("DRAWING_REVIEW_CONFIG", ROOT / "config.json"))


def load_dotenv(path=ROOT / ".env"):
    """KEY=value lines (an `export ` prefix is fine) from .env; the shell environment wins."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value and not os.environ.get(key.strip()):
            os.environ[key.strip()] = value


load_dotenv()
OPENROUTER_URL = "https://openrouter.ai/api/v1"

DEFAULT_CONFIG = {
    "openrouter": {"model": "google/gemini-2.5-flash", "fallback_models": [], "max_cost_usd": 0.30,
                   "max_turns": 30, "max_tokens": 8000, "price_per_mtok": {"input": 0.30, "output": 2.50}},
    "mcp": {"transport": "stdio",
            "command": ["docker", "run", "-i", "--rm", "--network", "none", "--memory", "2g", "--cpus", "2",
                        "-v", "{output_dir}:/output", "-v", "{input_dir}:/input:ro", "drawing2cad"],
            "env": {}, "server_output_dir": "/output", "server_input_dir": "/input",
            "input_dir": "work/input", "output_dir": "work/output",
            "url": "http://127.0.0.1:3000/mcp", "call_timeout_s": 300},
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for section, values in user.items():
            if isinstance(values, dict) and isinstance(cfg.get(section), dict):
                cfg[section].update(values)
            else:
                cfg[section] = values
    m = cfg["mcp"]
    for key in ("input_dir", "output_dir"):
        p = Path(os.path.expanduser(m[key]))
        m[key] = str(p if p.is_absolute() else (ROOT / p).resolve())
        Path(m[key]).mkdir(parents=True, exist_ok=True)
    return cfg


CONFIG = load_config()


def json_response(handler, status, value):
    data = json.dumps(value).encode()
    handler.send_response(status); handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data))); handler.send_header("Cache-Control", "no-store")
    handler.end_headers(); handler.wfile.write(data)


# ---------------------------------------------------------------- MCP clients

class McpError(RuntimeError):
    pass


class StdioMcp:
    """MCP client over stdio (newline-delimited JSON-RPC), e.g. `docker run -i ... drawing2cad`."""

    def __init__(self, cfg):
        m = cfg["mcp"]
        fill = lambda s: s.format(input_dir=m["input_dir"], output_dir=m["output_dir"], root=str(ROOT))
        cmd = m["command"]
        cmd = [fill(c) for c in (shlex.split(cmd) if isinstance(cmd, str) else cmd)]
        env = {**os.environ, **{k: fill(str(v)) for k, v in (m.get("env") or {}).items()}}
        self.timeout = m.get("call_timeout_s", 300)
        self.stderr = open(Path(m["output_dir"]) / "mcp_stderr.log", "ab")
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, env=env)
        except FileNotFoundError as exc:
            raise McpError(f"Cannot start the MCP server ({cmd[0]} not found). Check mcp.command in config.json.") from exc
        self.messages = queue.Queue(); self.request_id = 0; self.server_info = {}
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                pass  # stray non-JSON output on stdout
        self.messages.put(None)

    def _send(self, msg):
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode()); self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpError("The MCP server stopped. See mcp_stderr.log in the output folder.") from exc

    def rpc(self, method, params=None, timeout=None):
        self.request_id += 1; rid = self.request_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.time() + (timeout or self.timeout)
        while True:
            try:
                msg = self.messages.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                raise McpError(f"MCP {method} timed out after {timeout or self.timeout}s")
            if msg is None:
                raise McpError("The MCP server exited. See mcp_stderr.log in the output folder.")
            if "method" in msg and "id" in msg:  # request from the server (ping, roots/list...)
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}}); continue
            if msg.get("id") != rid:
                continue  # notification or log message
            if msg.get("error"):
                raise McpError(msg["error"].get("message", "MCP error"))
            return msg.get("result", {})

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self):
        # the first start of a Docker container imports CadQuery: allow it time
        res = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "drawing-review-ui", "version": "2.0"}}, timeout=180)
        self.notify("notifications/initialized"); self.server_info = res
        return res

    def tools(self):
        return self.rpc("tools/list").get("tools", [])

    def call(self, name, args):
        return self.rpc("tools/call", {"name": name, "arguments": args})

    def close(self):
        try:
            self.proc.stdin.close(); self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        self.stderr.close()


class HttpMcp:
    """MCP client over Streamable HTTP (mcp.url in config.json)."""

    def __init__(self, cfg):
        self.url = cfg["mcp"]["url"]; self.timeout = cfg["mcp"].get("call_timeout_s", 300)
        self.session = None; self.request_id = 0; self.server_info = {}

    def _post(self, payload):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                return response.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            raise McpError(f"MCP HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"Cannot reach the MCP server at {self.url}: {exc.reason}") from exc

    def rpc(self, method, params=None, timeout=None):
        self.request_id += 1
        body = self._post({"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params or {}})
        msgs = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")] or [body]
        for raw in msgs:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") == self.request_id:
                if msg.get("error"):
                    raise McpError(msg["error"].get("message", "MCP error"))
                return msg.get("result", {})
        raise McpError(f"MCP returned an unreadable response: {body[:300]}")

    def initialize(self):
        self.server_info = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                   "clientInfo": {"name": "drawing-review-ui", "version": "2.0"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self.server_info

    def tools(self):
        return self.rpc("tools/list").get("tools", [])

    def call(self, name, args):
        return self.rpc("tools/call", {"name": name, "arguments": args})

    def close(self):
        pass


def open_mcp(cfg):
    client = HttpMcp(cfg) if cfg["mcp"]["transport"] == "http" else StdioMcp(cfg)
    try:
        client.initialize()
    except Exception:
        client.close(); raise
    return client


def tool_text(result):
    """Text of an MCP tool result, parsed as JSON when possible."""
    if result.get("structuredContent") is not None:
        sc = result["structuredContent"]
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    try:
        return json.loads(text)
    except ValueError:
        return {"text": text}


def host_path(cfg, path):
    """Map a path the MCP server reports (e.g. /output/x.step inside Docker) to this machine."""
    if not path:
        return None
    m = cfg["mcp"]
    for server_dir, local_dir in ((m.get("server_output_dir"), m["output_dir"]), (m.get("server_input_dir"), m["input_dir"])):
        if server_dir and str(path).startswith(server_dir.rstrip("/") + "/"):
            return str(Path(local_dir) / str(path)[len(server_dir.rstrip("/")) + 1:])
    p = Path(path)
    return str(p) if p.exists() else str(Path(m["output_dir"]) / p.name)


# ---------------------------------------------------------------- OpenRouter

_PRICES = {}


def model_price(cfg, model):
    """USD per token (input, output) from OpenRouter's model list, else from config.json."""
    if not _PRICES:
        try:
            with urllib.request.urlopen(OPENROUTER_URL + "/models", timeout=20) as r:
                for m in json.loads(r.read())["data"]:
                    p = m.get("pricing", {})
                    _PRICES[m["id"]] = (float(p.get("prompt", 0)), float(p.get("completion", 0)))
        except Exception:
            _PRICES["_failed"] = (0, 0)
    if model in _PRICES:
        return _PRICES[model]
    fallback = cfg["openrouter"].get("price_per_mtok", {})
    return fallback.get("input", 3) / 1e6, fallback.get("output", 15) / 1e6


def openrouter_chat(key, payload):
    req = urllib.request.Request(OPENROUTER_URL + "/chat/completions", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key,
                                          "HTTP-Referer": "http://127.0.0.1:8765", "X-Title": "Drawing2CAD Review"})
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            out = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        if exc.code == 401:
            raise RuntimeError("OpenRouter rejected the API key (401). Check OPENROUTER_API_KEY.") from exc
        if exc.code == 402:
            raise RuntimeError("OpenRouter account has no credits left (402).") from exc
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail[:600]}") from exc
    if out.get("error"):
        raise RuntimeError("OpenRouter: " + str(out["error"].get("message", out["error"]))[:600])
    return out


def mcp_tools_for_openai(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": (t.get("description") or "")[:4000],
                                              "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}}
            for t in tools]


WEB_RULES = """You are running inside a web app, not a chat: nobody can answer questions during this run.
- The drawing PDF is already in the input folder as "{pdf}". Call load_drawing with exactly that name.
- Go all the way: load_drawing -> get_part_schema -> write the part JSON -> build_from_json -> fix errors and
  rebuild until the model check passes -> export_step("{stem}.step").
- Do NOT call confirm_items: open confirm items stay open (the STEP is then saved as a draft) and the web page
  shows them to the engineer for review.
- Be economical: one get_part_schema call, zoom only where you really cannot read a value. When export_step
  succeeded, answer with one short sentence and stop."""


class Job:
    def __init__(self, job_id):
        self.id = job_id; self.log = []; self.cost = 0.0; self.done = False; self.started = time.time()

    def say(self, text):
        self.log.append({"t": round(time.time() - self.started, 1), "text": text}); print(f"[job {self.id}] {text}")


JOBS = {}


def get_job(job_id):
    job_id = re.sub(r"[^A-Za-z0-9_-]", "", job_id or "")[:40] or uuid.uuid4().hex[:12]
    job = JOBS.get(job_id) or JOBS.setdefault(job_id, Job(job_id))
    return job


def run_agent(cfg, job, client, key, model, messages, tools, stop_after_export=True):
    """OpenRouter tool-calling loop over the MCP tools. Returns a dict with the tool results seen."""
    orc = cfg["openrouter"]; limit = float(orc.get("max_cost_usd", 0.3))
    price_in, price_out = model_price(cfg, model)
    seen = {"build": None, "export": None, "part": None, "drawing_loaded": False, "stopped": None}
    last_prompt_tokens = 0; sent = 0
    for turn in range(int(orc.get("max_turns", 30))):
        # older drawing images are dropped (the model has read them), the last two stay
        image_msgs = [m for m in messages if m["role"] == "user" and isinstance(m["content"], list)]
        for m in image_msgs[:-2]:
            m["content"] = "[earlier tool images removed to save tokens]"
        # estimate the next call before sending it: last prompt + what was added since
        new_tokens = 0
        for m in messages[sent:]:
            if isinstance(m.get("content"), list):
                new_tokens += 1500 * sum(1 for c in m["content"] if c.get("type") == "image_url")
            else:
                new_tokens += len(json.dumps(m)) / 3.5
        estimate = (last_prompt_tokens + new_tokens) * price_in + orc.get("max_tokens", 8000) * price_out * 0.25
        if job.cost + estimate > limit:
            seen["stopped"] = f"Cost limit ${limit:.2f} reached (spent ${job.cost:.3f})"
            job.say(seen["stopped"]); break
        payload = {"model": model, "messages": messages, "tools": tools, "tool_choice": "auto",
                   "max_tokens": orc.get("max_tokens", 8000), "temperature": 0.2, "usage": {"include": True}}
        if orc.get("fallback_models"):
            payload["models"] = [model] + list(orc["fallback_models"])
        job.say(f"Asking {model} (turn {turn + 1})")
        out = openrouter_chat(key, payload); sent = len(messages)
        usage = out.get("usage") or {}
        last_prompt_tokens = usage.get("prompt_tokens", last_prompt_tokens)
        cost = usage.get("cost")
        if cost is None:
            cost = usage.get("prompt_tokens", 0) * price_in + usage.get("completion_tokens", 0) * price_out
        job.cost += float(cost)
        msg = (out.get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": calls} if calls else {})})
        if not calls:
            if msg.get("content"):
                job.say("Model: " + msg["content"].strip()[:300])
            nudges = seen.setdefault("nudges", 0)
            if seen["export"] or nudges >= 2:
                break
            seen["nudges"] = nudges + 1
            messages.append({"role": "user", "content": "Continue with the tools until export_step has succeeded."})
            continue
        images = []
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except ValueError:
                args = None
            if args is None:
                content = json.dumps({"ok": False, "error": "Tool arguments were not valid JSON. Send valid JSON."})
            else:
                if name == "confirm_items":
                    content = json.dumps({"ok": False, "error": "Not available in the web app: leave confirm items open."})
                else:
                    job.say(f"Tool {name}")
                    result = client.call(name, args)
                    data = tool_text(result)
                    if name == "load_drawing" and isinstance(data, dict) and data.get("ok"):
                        seen["drawing_loaded"] = True
                    if name == "build_from_json" and isinstance(data, dict):
                        part = args.get("part")
                        seen["part"] = json.loads(part) if isinstance(part, str) else part
                        seen["build"] = data
                        job.say("Build " + ("passed the model check" if data.get("ok") else "failed: " + _first_error(data)))
                    if name == "export_step" and isinstance(data, dict) and data.get("path"):
                        seen["export"] = data; job.say(f"STEP exported ({data.get('status')})")
                    content = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
                    for c in result.get("content", []):
                        if c.get("type") == "image":
                            images.append({"type": "image_url", "image_url": {"url": f"data:{c.get('mimeType', 'image/png')};base64,{c['data']}"}})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": content[:150000]})
        if images:
            messages.append({"role": "user", "content": [{"type": "text", "text": "Images returned by the tool calls above:"}, *images]})
        if seen["export"] and stop_after_export:
            break
    else:
        seen["stopped"] = f"Stopped after {orc.get('max_turns', 30)} turns"
    job.say(f"Model cost so far ${job.cost:.3f}")
    return seen


def _first_error(data):
    errs = data.get("errors") or []
    if errs:
        e = errs[0]
        return str(e.get("problem") or e.get("msg") or e)[:200]
    failed = [k for k, v in (data.get("checks") or {}).items() if not v]
    return ", ".join(failed) or "see the model check"


def finish(cfg, job, client, seen, stem):
    """Export when the model built but did not export, then collect STEP, review JSON and DXFs."""
    export = seen.get("export")
    if not export and seen.get("build") and seen["build"].get("ok"):
        job.say("Exporting the last good build")
        export = tool_text(client.call("export_step", {"filename": stem + ".step"}))
    if not export or not export.get("path"):
        problem = seen.get("stopped") or "The model did not reach a valid build."
        if seen.get("build"):
            problem += " Last build: " + _first_error(seen["build"])
        raise RuntimeError(problem)
    step_path = Path(host_path(cfg, export["path"]))
    if not step_path.exists():
        raise RuntimeError(f"STEP was written to {export['path']} but is not visible at {step_path}. "
                           "Check mcp.output_dir / server_output_dir in config.json.")
    review = {}
    review_file = host_path(cfg, export.get("review_file"))
    if review_file and Path(review_file).exists():
        review = json.loads(Path(review_file).read_text(encoding="utf-8"))
    review_rows = [dict(c, kind="confirm") for c in review.get("open_confirmations", [])]
    review_rows += [r for r in review.get("review_list", []) if r.get("kind") != "assumption" or not review_rows]
    page_json = {"status": review.get("status", export.get("status")), "part": seen.get("part"),
                 "confirm": [dict(c, confirmed=bool(c.get("answer"))) for c in review.get("confirm", [])],
                 "review_list": review_rows, "model_check": review.get("model_check"),
                 "step_file": step_path.name, "model_cost_usd": round(job.cost, 4)}
    return {"json": page_json, "dxf": collect_dxfs(cfg, export.get("dxf") or {}),
            "step": {"name": step_path.name, "base64": base64.b64encode(step_path.read_bytes()).decode()},
            "log": job.log, "cost": round(job.cost, 4)}


def collect_dxfs(cfg, dxf):
    paths = []

    def walk(v):
        if isinstance(v, str) and v.lower().endswith(".dxf"):
            paths.append(v)
        elif isinstance(v, dict):
            [walk(x) for x in v.values()]
        elif isinstance(v, list):
            [walk(x) for x in v]
    walk(dxf)
    files = []
    for p in dict.fromkeys(paths):
        local = Path(host_path(cfg, p))
        if local.exists() and local.stat().st_size < 4_000_000:
            files.append({"name": local.name, "content": local.read_text(encoding="utf-8", errors="replace")})
    return files


def process_pdf(cfg, job, key, model, pdf_name, pdf_bytes):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(pdf_name).stem)[:60] or "drawing"
    stem = f"{safe}_{job.id[:6]}"
    name = stem + ".pdf"
    (Path(cfg["mcp"]["input_dir"]) / name).write_bytes(pdf_bytes)
    job.say("Starting the drawing2cad MCP server")
    client = open_mcp(cfg)
    try:
        tools = mcp_tools_for_openai(client.tools())
        system = (client.server_info.get("instructions") or "") + "\n\n" + WEB_RULES.format(pdf=name, stem=stem)
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": f"Convert the drawing {name} into a validated STEP model."}]
        seen = run_agent(cfg, job, client, key, model, messages, tools)
        return finish(cfg, job, client, seen, stem)
    finally:
        client.close()


def regenerate(cfg, job, key, model, edited, pdf_name):
    """Rebuild from the edited page JSON: the part JSON is built as written (no model call);
    the model is asked only when the edited JSON does not build."""
    part = edited.get("part") if isinstance(edited, dict) else None
    if not isinstance(part, dict):
        raise ValueError("The edited JSON needs the 'part' object (the part JSON from the first run).")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(pdf_name or part.get("name") or "part").stem)[:60] + "_" + job.id[:6]
    job.say("Starting the drawing2cad MCP server")
    client = open_mcp(cfg)
    try:
        job.say("Building the edited part JSON")
        build = tool_text(client.call("build_from_json", {"part": part}))
        seen = {"build": build, "export": None, "part": part, "stopped": None}
        if not build.get("ok"):
            job.say("Build failed: " + _first_error(build))
            if not key:
                raise RuntimeError("The edited JSON does not build: " + _first_error(build))
            job.say("Asking the model to fix the JSON")
            tools = mcp_tools_for_openai(client.tools())
            system = (client.server_info.get("instructions") or "") + "\n\n" + WEB_RULES.format(pdf=pdf_name or "", stem=stem)
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": "The engineer edited this part JSON. Keep every edited value, fix only "
                         "what the errors require, build_from_json until the check passes, then export_step.\n\n"
                         f"Part JSON:\n{json.dumps(part)}\n\nBuild result:\n{json.dumps(build)[:20000]}"}]
            seen = run_agent(cfg, job, client, key, model, messages, tools)
        ids = [c["id"] for c in edited.get("confirm", []) if isinstance(c, dict) and c.get("confirmed") and c.get("id")]
        if ids and seen.get("build", {}).get("ok"):
            client.call("confirm_items", {"ids": ids, "answer": "confirmed in the review page"})
            job.say(f"Confirmed {len(ids)} item(s)")
        if seen.get("build", {}).get("ok"):
            seen["export"] = tool_text(client.call("export_step", {"filename": stem + ".step"}))
        return finish(cfg, job, client, seen, stem)
    finally:
        client.close()


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, value): json_response(self, status, value)

    def read_body(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n > 80 * 1024 * 1024: raise ValueError("Request is larger than 80 MB")
        return self.rfile.read(n)

    def llm_settings(self):
        key = self.headers.get("X-LLM-API-Key", "").strip() or os.getenv("OPENROUTER_API_KEY", "").strip()
        model = self.headers.get("X-LLM-Model", "").strip() or CONFIG["openrouter"]["model"]
        return key, model

    def do_GET(self):
        url = urlparse(self.path); path = url.path
        if path == "/api/config":
            self.send_json(200, {"provider": "OpenRouter", "server_key": bool(os.getenv("OPENROUTER_API_KEY")),
                                 "model": CONFIG["openrouter"]["model"], "max_cost_usd": CONFIG["openrouter"]["max_cost_usd"],
                                 "mcp": CONFIG["mcp"]["transport"]}); return
        if path == "/api/progress":
            job = JOBS.get(parse_qs(url.query).get("job", [""])[0])
            self.send_json(200, {"log": job.log, "cost": round(job.cost, 4), "done": job.done} if job else {"log": []}); return
        file = ROOT / ("index.html" if path in ("/", "/index.html") else path.lstrip("/"))
        if not file.resolve().is_relative_to(ROOT) or not file.is_file() or file.name == "config.json":
            self.send_error(404); return
        data = file.read_bytes(); ct = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self.send_response(200); self.send_header("Content-Type", ct + ("; charset=utf-8" if ct.startswith("text/") or ct == "application/javascript" else ""))
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        path = urlparse(self.path).path
        job = get_job(self.headers.get("X-Job-Id"))
        try:
            key, model = self.llm_settings()
            if path == "/api/process":
                if not key: raise ValueError("No OpenRouter key: set OPENROUTER_API_KEY before starting server.py.")
                from email.parser import BytesParser
                from email import policy
                raw = self.read_body()
                msg = BytesParser(policy=policy.default).parsebytes(
                    f"Content-Type: {self.headers.get('Content-Type')}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw)
                parts = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
                pdfpart = parts.get("pdf")
                if not pdfpart or not (pdfpart.get_filename() or "").lower().endswith(".pdf"): raise ValueError("Upload a PDF drawing")
                self.send_json(200, process_pdf(CONFIG, job, key, model, pdfpart.get_filename(), pdfpart.get_payload(decode=True))); return
            if path == "/api/regenerate":
                body = json.loads(self.read_body())
                self.send_json(200, regenerate(CONFIG, job, key, model, body.get("json"), body.get("pdf_name"))); return
            self.send_error(404)
        except Exception as exc:
            job.say("Error: " + str(exc))
            self.send_json(502, {"error": str(exc), "log": job.log, "cost": round(job.cost, 4)})
        finally:
            job.done = True

    def log_message(self, fmt, *args):
        if "/api/progress" not in str(args[0] if args else ""):
            print("[review-ui] " + fmt % args)


if __name__ == "__main__":
    address = ("127.0.0.1", 8765)
    print(f"Drawing review UI: http://{address[0]}:{address[1]}")
    print(f"Model {CONFIG['openrouter']['model']}, cost limit ${CONFIG['openrouter']['max_cost_usd']} per run, "
          f"MCP over {CONFIG['mcp']['transport']}")
    if not os.getenv("OPENROUTER_API_KEY"):
        print(f"Warning: OPENROUTER_API_KEY is not set. Put it in {ROOT / '.env'} "
              "(OPENROUTER_API_KEY=sk-or-...) or export it, then restart.")
    ThreadingHTTPServer(address, Handler).serve_forever()
