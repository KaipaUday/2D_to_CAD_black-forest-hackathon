# Drawing review local app

The UI runs at `http://127.0.0.1:8765` and expects a Docker-hosted MCP server using the MCP Streamable HTTP transport.

Start the server, then choose Anthropic or OpenRouter and enter its API key in the controls at the top of the page. The key stays in the current browser tab and is sent to the local server only with processing requests; it is not written to disk.

Set the MCP connection before starting the server:

```sh
export MCP_URL=http://127.0.0.1:3000/mcp
# Optional provider defaults are available for command-line integrations,
# but the UI uses the provider, key, and model entered in its controls.
export MCP_DXF_TOOL=your_pdf_to_dxf_tool
export MCP_STEP_TOOL=your_json_to_step_tool
python3 server.py
```

The server also auto-detects tools from names containing DXF plus a conversion verb, and STEP/STP/CAD/model plus a generation verb. Configure explicit names when your Docker MCP uses different naming. Tool schemas should expose PDF bytes as a base64 string (or a PDF-named argument), DXF file output as `files: [{name, content}]`, and CAD output as STEP base64 or a downloadable URL. The CAD tool's JSON argument should be named `json`, `features`, `review_json`, or `data` (or contain one of those terms).

The extraction prompt is in `drawing-review.skill.md`. The provider selector supports Anthropic and OpenRouter; model names are optional. For Anthropic, use a key from the [Anthropic Console](https://console.anthropic.com/). For OpenRouter, use a key from [OpenRouter](https://openrouter.ai/).
