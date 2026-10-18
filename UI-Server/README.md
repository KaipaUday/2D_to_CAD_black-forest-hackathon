# Drawing review local app

Upload a PDF drawing; the page shows the PDF, the DXF, the STEP model and the review list.

    Browser ──► server.py ──► OpenRouter (model with tool calling + vision)
                    └──────► drawing2cad MCP server (Docker, stdio)

`server.py` runs the agent loop: it gives the model the drawing2cad tools, executes every tool call
on the MCP server (load_drawing → get_part_schema → build_from_json → export_step) and sends the
STEP, the drawing/model DXFs and the review JSON back to the page. The API key never reaches the browser.

## Run

Needs Docker and Python 3.10+ (standard library only, no `pip install`). From the repo root:

    # 1. build the MCP image once (rebuild after changes in MCP/); this also runs the tests
    docker build -t drawing2cad .

    # 2. go to the UI server folder
    cd UI-Server

    # 3. give it your OpenRouter key (https://openrouter.ai/keys), either for this shell ...
    export OPENROUTER_API_KEY=sk-or-...
    #    ... or once in a .env file next to server.py (git-ignored, never commit it)
    echo "OPENROUTER_API_KEY=sk-or-..." > .env

    # 4. start it and open http://127.0.0.1:8765
    python3 server.py

Upload a PDF drawing on the page. The server starts the `drawing2cad` container itself for each run
(`docker run -i --network none ...`, see `mcp.command` in `config.json`), so no container needs to be
running beforehand. Without a key the page still opens, the server prints a warning, and you can paste
a key into the page instead (sent to the local server only, not stored).

Uploaded PDFs go to `work/input`, STEP / review JSON / DXF / previews to `work/output`
(`work/output/mcp_stderr.log` has the MCP server's log, `work/output/mcp_log.jsonl` every tool call).
Progress and model cost show under "Model features & review JSON" while a run is going.

## config.json

- `openrouter.model`: the model for the agent loop; it needs vision + tool calling. The committed config
  uses `anthropic/claude-opus-5.5` with `google/gemini-2.5-flash` as fallback; Gemini Flash alone is much
  cheaper (about $0.05–0.15 per run). The model field on the page overrides it per run. `fallback_models` are tried by
  OpenRouter when the main model is unavailable.
- `openrouter.max_cost_usd`: cost limit per run (3.0 in the committed config, 0.30 when
  unset). Checked before every model call with
  OpenRouter's reported cost; the run stops when the next call would exceed it. If a model was already
  built, it is still exported.
- `mcp.command`: how the MCP server is started. Without Docker, use the repo venv instead:

      "command": ["{root}/../.venv/bin/python", "{root}/../MCP/server.py"],
      "env": {"OUTPUT_DIR": "{output_dir}", "INPUT_DIR": "{input_dir}", "DRAWING2CAD_NOTIFY": "0"},
      "server_output_dir": "", "server_input_dir": ""

- `mcp.transport: "http"` with `mcp.url` talks to a Streamable HTTP MCP server instead.

## Review and rebuild

The first run never confirms items: the STEP is saved as `<name>_draft.step` and the open items are
listed. Click **Edit JSON**, change values in `part`, set `"confirmed": true` on the items in
`confirm` you accept, then **Save & regenerate STEP**. The edited part JSON is built as written
(no model call, no cost); only when it fails validation is the model asked to fix it.
