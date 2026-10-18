# Drawing2CAD (Black Forest Hackathon, team MindfulAutomation-3D)

2D technical drawing (PDF) -> 3D model (STEP), driven by an AI through a local MCP server.
The AI reads the drawing and writes a part JSON; the server is deterministic: it validates the JSON,
builds the solid with CadQuery, checks the model against the spec and exports STEP plus a review JSON
that lists every assumption. No dimension is invented silently. Details: [MCP/README.md](MCP/README.md).

    drawing PDF ──► load_drawing (PDF -> DXF + drawing JSON + page image)
                ──► AI (Claude Desktop, or the web UI via OpenRouter) writes the part JSON
                ──► validate_part_json ──► build_from_json (CadQuery) ──► model check + preview
                ──► confirm_items (user answers the short confirm list) ──► export_step
                ──► <part>.step + <part>_review.json + model view DXFs

Two ways to use it, both on the same MCP server image:
- **Claude Desktop** (or any MCP client) talks to the server directly, see below.
- **Web review UI** ([UI-Server/README.md](UI-Server/README.md)): upload a PDF, see PDF, DXF, STEP and the
  review list side by side, edit the JSON and rebuild. Needs an OpenRouter key.

| Folder | What | Read |
|---|---|---|
| `MCP/` | the MCP server (tools, schema, builder, checks, PDF reader) | [MCP/README.md](MCP/README.md) |
| `UI-Server/` | local web review app (stdlib Python + three.js STEP viewer) | [UI-Server/README.md](UI-Server/README.md) |
| `skills/drawing2cad-convert/` | agent skill: how an AI should use the tools | [SKILL.md](skills/drawing2cad-convert/SKILL.md) |
| `Dockerfile` | the image the evaluation builds; runs all tests at build time | "What the build checks" |

## Run with Docker (recommended)

    docker build -t drawing2cad .

This is the build an automated evaluation runs: the root `Dockerfile` is the only build file at the repo
root (no `docker-compose.yml` or `package.json`), it copies only `MCP/` and needs no API key. The build also runs the regression suite (see "What the build checks"), so a finished build means the
CAD kernel works in the image. Needs no secrets, no licensed software and no GPU; the first build
downloads about 1 GB of Python wheels (CadQuery / OpenCascade).

### Use it from Claude Desktop

Add to `claude_desktop_config.json` (Settings > Developer > Edit Config), adjust the two host folders,
then restart Claude Desktop:

    "mcpServers": {
      "drawing2cad": {
        "command": "docker",
        "args": ["run", "-i", "--rm", "--network", "none",
                 "--memory", "2g", "--cpus", "2",
                 "-v", "/Users/xxx/GitHub/2D_to_CAD_black-forest-hackathon/output:/output",
                 "-v", "/Users/xxx/Drawings:/input:ro",
                 "drawing2cad"]
      }
    }

- `-i` keeps stdin open: Claude talks to the server over stdio, exactly like the local Python setup.
- `--network none`: the server needs no network, so the container gets none.
- `/output`: STEP, `<name>_review.json`, preview PNG, the DXF of each loaded drawing, the model view DXFs
  (`<name>_views.dxf`, `_front/_top/_left/_iso.dxf`) and the request log `mcp_log.jsonl` land here.
- `/input` (read-only): put your drawing PDFs here. Ask Claude to load e.g. `Splint.pdf`; a full host path
  also works, the server then looks the file name up in `/input`.
- Linux hosts: the server runs as uid 1000. If `/output` belongs to another user, add `"--user", "<your uid>"`.

Then drop a drawing into the chat or name a PDF in `/input`, e.g. "Convert Splint.pdf to a STEP model".
Claude calls `load_drawing` -> `get_part_schema` -> `build_from_json` -> `confirm_items` -> `export_step`.

Quick check without Claude (prints the server info and the 7 tools as JSON):

    printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cli","version":"1"}}}' \
      '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
      '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | docker run -i --rm --network none drawing2cad

## Run without Docker

Python 3.12 or newer:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt   # mcp>=2 cadquery pydantic shapely pymupdf ezdxf
    cd MCP && python test_all.py      # same checks as the Docker build

Claude Desktop config:

    "mcpServers": {
      "drawing2cad": {
        "command": "/Users/xxx/GitHub/2D_to_CAD_black-forest-hackathon/.venv/bin/python",
        "args": ["/Users/xxx/GitHub/2D_to_CAD_black-forest-hackathon/MCP/server.py"],
        "env": {"OUTPUT_DIR": "/Users/xxx/GitHub/2D_to_CAD_black-forest-hackathon/output"}
      }
    }

Test a single part JSON without any AI: `cd MCP && python run_json.py examples/grundplatte.json`.

## What the build checks

`docker build` byte-compiles every module (`python -m compileall`) and runs `MCP/test_all.py`. The build
fails if any check fails:

1. Every `MCP/examples/*.json` part (plates, Grundplatte, Lagerschaft, Fuss, turned shafts, threads,
   side holes, fillets/chamfers...) is validated, built with CadQuery and passes the model check:
   one valid solid, bounding box and z range match the spec, every hole probed for diameter and depth,
   round features found; then exported to STEP and re-imported, and the model view DXFs are written.
2. More than 20 deliberately broken part JSONs (holes outside the part, too deep, wrong thread core
   diameter, floating pads, impossible fillets, typos in field names...) must be rejected with a
   clear message.
3. The tolerant input layer accepts typical AI formatting slips, and a 1:2 scale mix-up gets a hint.
4. PDF reading: Bezier arcs and flattened circles are recognised as arcs/circles. (The sample
   drawing PDFs are not in the repo, so the full `load_drawing` run is skipped in the build;
   set `DRAWINGS_DIR` to run it locally.)
5. The MCP tools end to end: tool schemas, build + export return the DXF paths, every call is in
   the request log.

What it does not check: the AI step itself (reading a drawing and writing the JSON) needs an LLM
client such as Claude Desktop and is shown in the demo.

## For AI agents reading this repo
- Start with the skill [skills/drawing2cad-convert/SKILL.md](skills/drawing2cad-convert/SKILL.md) (tool
  order, conventions, rules). The server sends the same text as its MCP instructions (`MCP/guide.py`), and
  `get_part_schema` returns the full JSON schema with a worked example.
- The part JSON format is `MCP/schema.py` (Pydantic, unknown keys rejected); examples in `MCP/examples/`.
- To check a change: `cd MCP && python run_json.py examples/<file>.json` for one part, `python test_all.py`
  for everything (the Docker build runs the same).

## Repository
- `Dockerfile`     image for the MCP server (stdio), runs the tests at build time
- `MCP/`           the server: `server.py` (MCP tools), `schema.py`, `normalize.py`, `builder.py`,
                   `validation.py`, `drawing.py` + `pdf2dxf.py` (PDF -> DXF + drawing JSON), `preview.py`, `views_dxf.py`,
                   `guide.py` (instructions for the AI), `run_json.py`, `test_all.py`, `examples/`
- `UI-Server/`     web review app: `server.py` (OpenRouter agent loop over the MCP server), `index.html`,
                   `config.json` (model, cost limit, how the MCP server is started), `vendor/` (three.js, occt-import-js)
- `skills/`        agent skill for the conversion workflow
- `output/`        default place for generated STEP / review files (git-ignored)

API keys: only the web UI needs one (`OPENROUTER_API_KEY`, from the shell or `UI-Server/.env`). `.env` files
are git-ignored and excluded from the Docker build context.
