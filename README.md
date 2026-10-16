Need python 3.12 or greater for MCP

`
python3 -m venv .venv #create new ev
source .venv/bin/activate  #activate 
pip install cadquery "mcp>=2"
`

update the path according claude installation.


`
"mcpServers": {
    "drawing2cad": {
      "command": "/Users/xxx/GitHub/2D_to_CAD_black-forest-hackathon/.venv/bin/python",
      "args": ["/Users/xxx/GitHub/2D_to_CAD_black-forest-hackathon/MCP/server.py"],
      "env": {
        "OUTPUT_DIR": "/Users/xxx/GitHub/2D_to_CAD_black-forest-hackathon/output"
      }
    }
  }
`