# Drawing review local app

Upload a PDF drawing; the page shows the PDF, the DXF, the STEP model and the review list.

    Browser ──► server.py ──► OpenRouter (model with tool calling + vision)
                    └──────► drawing2cad MCP server (Docker, stdio)

`server.py` runs the agent loop: it gives the model the drawing2cad tools, executes every tool call
on the MCP server (load_drawing → get_part_schema → build_from_json → export_step) and sends the
STEP, the drawing/model DXFs and the review JSON back to the page. The API key never reaches the browser.

## Run

    # once, in the repo: build the MCP image (rebuild after MCP changes)
    cd ~/GitHub/2D_to_CAD_black-forest-hackathon && docker build -t drawing2cad .

    cd ~/Desktop/Hackathon/drawing-review-files
    # put the key in .env once (OPENROUTER_API_KEY=sk-or-...), or export it in this shell
    python3 server.py            # open http://127.0.0.1:8765

Uploaded PDFs go to `work/input`, STEP / review JSON / DXF / previews to `work/output`
(`work/output/mcp_stderr.log` has the MCP server's log). Progress and model cost show under
"Model features & review JSON" while a run is going.

## config.json

- `openrouter.model`: default `google/gemini-2.5-flash` (vision + tool calling, a run costs about
  $0.05–0.15). The model field on the page overrides it per run. `fallback_models` are tried by
  OpenRouter when the main model is unavailable.
- `openrouter.max_cost_usd`: cost limit per run (default 0.30). Checked before every model call with
  OpenRouter's reported cost; the run stops when the next call would exceed it. If a model was already
  built, it is still exported.
- `mcp.command`: how the MCP server is started. Without Docker, use the repo venv instead:

      "command": ["/Users/snow/GitHub/2D_to_CAD_black-forest-hackathon/.venv/bin/python",
                  "/Users/snow/GitHub/2D_to_CAD_black-forest-hackathon/MCP/server.py"],
      "env": {"OUTPUT_DIR": "{output_dir}", "INPUT_DIR": "{input_dir}", "DRAWING2CAD_NOTIFY": "0"},
      "server_output_dir": "", "server_input_dir": ""

- `mcp.transport: "http"` with `mcp.url` talks to a Streamable HTTP MCP server instead.

## Review and rebuild

The first run never confirms items: the STEP is saved as `<name>_draft.step` and the open items are
listed. Click **Edit JSON**, change values in `part`, set `"confirmed": true` on the items in
`confirm` you accept, then **Save & regenerate STEP**. The edited part JSON is built as written
(no model call, no cost); only when it fails validation is the model asked to fix it.
