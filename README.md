# Drawing2CAD (Black Forest Hackathon, team MindfulAutomation-3D)

2D technical drawing (PDF) -> 3D model (STEP), driven by an AI through a local MCP server.
The AI reads the drawing and writes a part JSON; the server is deterministic: it validates the JSON,
builds the solid with CadQuery, checks the model against the spec and exports STEP plus a review JSON
that lists every assumption. No dimension is invented silently.

    drawing PDF ──► load_drawing (PDF -> DXF + drawing JSON + page image)
                ──► AI (Claude Desktop, or the web UI via OpenRouter) writes the part JSON
                ──► validate_part_json ──► build_from_json (CadQuery) ──► model check + preview
                ──► confirm_items (user answers the short confirm list) ──► export_step
                ──► <part>.step + <part>_review.json + model view DXFs

| Folder | What | Read |
|---|---|---|
| `Dockerfile` | the MCP server image; the build runs all tests | [What the build checks](#what-the-build-checks) |
| `MCP/` | the MCP server (tools, schema, builder, checks, PDF reader) | [MCP/README.md](MCP/README.md) |
| `UI-Server/` | local web review app (stdlib Python + three.js STEP viewer) | [UI-Server/README.md](UI-Server/README.md) |
| `skills/drawing2cad-convert/` | agent skill: how an AI uses the tools | [SKILL.md](skills/drawing2cad-convert/SKILL.md) |

## 1. Build (this is also the evaluation build)

Needs only Docker: no API key, no secrets, no GPU, no licensed software.

    git clone https://github.com/KaipaUday/2D_to_CAD_black-forest-hackathon.git
    cd 2D_to_CAD_black-forest-hackathon
    docker build -t drawing2cad .

The root `Dockerfile` is the only build file at the repo root (no `docker-compose.yml`, no `package.json`).
It copies only `MCP/`, installs CadQuery / OpenCascade (about 1 GB of wheels on the first build) and runs
the regression suite, so a finished build means the server and the CAD kernel work in the image.

Check that the server answers (prints the server info and its 7 tools as JSON):

    printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cli","version":"1"}}}' \
      '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
      '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | docker run -i --rm --network none drawing2cad

## 2. Test with Claude Desktop

1. Make two folders, one for results and one for drawings:

       mkdir -p output drawings          # in the repo folder; copy a drawing PDF into drawings/

2. In Claude Desktop open Settings > Developer > Edit Config and add this to `claude_desktop_config.json`
   (replace `/ABSOLUTE/PATH/TO/REPO` with the output of `pwd`), then restart Claude Desktop:

       {
         "mcpServers": {
           "drawing2cad": {
             "command": "docker",
             "args": ["run", "-i", "--rm", "--network", "none", "--memory", "2g", "--cpus", "2",
                      "-v", "/ABSOLUTE/PATH/TO/REPO/output:/output",
                      "-v", "/ABSOLUTE/PATH/TO/REPO/drawings:/input:ro",
                      "drawing2cad"]
           }
         }
       }

3. In a new chat, check that `drawing2cad` shows up under the tools icon, then try:
   - without a drawing: *"Use drawing2cad: call get_part_schema, then build its worked example (Fuss) and
     export it as fuss.step."*
   - with a drawing: *"Convert MyPart.pdf to a STEP model."* (the file name in `drawings/`)

   Claude calls `load_drawing` -> `get_part_schema` -> `validate_part_json` -> `build_from_json`, asks you the
   short confirm list, then `confirm_items` -> `export_step`.

4. Results land in `output/`: `<part>.step` (or `<part>_draft.step` while items are unconfirmed),
   `<part>_review.json`, `<part>_preview.png`, the drawing and model view DXFs and `mcp_log.jsonl`.

Notes: `-i` is the stdio connection; `--network none` because the server needs no network. On Linux the
server runs as uid 1000; if `output/` belongs to another user, add `"--user", "<your uid>"` to `args`.

## 3. Web review UI (needs an OpenRouter API key)

Upload a PDF in the browser and see the PDF, the DXF, the 3D STEP model and the review list side by side;
edit the JSON and rebuild. The UI server is plain Python (no `pip install`) and starts the `drawing2cad`
image from step 1 by itself for every run.

    cd UI-Server
    export OPENROUTER_API_KEY=sk-or-...                   # key from https://openrouter.ai/keys
    # or once: echo "OPENROUTER_API_KEY=sk-or-..." > .env (git-ignored, never commit it)
    python3 server.py                                     # open http://127.0.0.1:8765

Model and cost limit per run are in `UI-Server/config.json`; details in [UI-Server/README.md](UI-Server/README.md).

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

## Run without Docker

Python 3.10 or newer:

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt   # mcp>=2 cadquery pydantic shapely pymupdf ezdxf
    cd MCP && python test_all.py      # same checks as the Docker build
    python run_json.py examples/grundplatte.json   # build one part JSON without any AI

For Claude Desktop use `"command": "/ABSOLUTE/PATH/TO/REPO/.venv/bin/python"`,
`"args": ["/ABSOLUTE/PATH/TO/REPO/MCP/server.py"]` and `"env": {"OUTPUT_DIR": "/ABSOLUTE/PATH/TO/REPO/output",
"INPUT_DIR": "/ABSOLUTE/PATH/TO/REPO/drawings"}`. For the web UI see `mcp.command` in [UI-Server/README.md](UI-Server/README.md).

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
