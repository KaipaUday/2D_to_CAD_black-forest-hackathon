#!/usr/bin/env python3
"""Local web/API server for the drawing review workflow.

Configure MCP_URL, MCP_DXF_TOOL and MCP_STEP_TOOL, plus either
ANTHROPIC_API_KEY or OPENROUTER_API_KEY in the environment before launch.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import json, mimetypes, os, re, uuid, urllib.request, urllib.error

ROOT = Path(__file__).resolve().parent
SKILL = ROOT / "drawing-review.skill.md"
MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:3000/mcp")

def json_response(handler, status, value):
    data=json.dumps(value).encode()
    handler.send_response(status); handler.send_header("Content-Type","application/json; charset=utf-8")
    handler.send_header("Content-Length",str(len(data))); handler.send_header("Cache-Control","no-store")
    handler.end_headers(); handler.wfile.write(data)

def http_json(url, payload, headers=None):
    data=json.dumps(payload).encode()
    req=urllib.request.Request(url,data=data,headers={"Content-Type":"application/json",**(headers or {})},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=180) as response:
            raw=response.read(); return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail=exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:1000]}") from exc

class McpClient:
    def __init__(self): self.session=None; self.request_id=0
    def rpc(self, method, params=None):
        self.request_id+=1
        headers={"Accept":"application/json, text/event-stream"}
        if self.session: headers["Mcp-Session-Id"]=self.session
        req=urllib.request.Request(MCP_URL,data=json.dumps({"jsonrpc":"2.0","id":self.request_id,"method":method,"params":params or {}}).encode(),headers={"Content-Type":"application/json",**headers},method="POST")
        try:
            with urllib.request.urlopen(req,timeout=180) as response:
                self.session=response.headers.get("Mcp-Session-Id",self.session)
                body=response.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            detail=exc.read().decode(errors="replace")
            if exc.code==401: raise RuntimeError("MCP authentication failed (401).")
            raise RuntimeError(f"MCP {method} failed ({exc.code}): {detail[:1000]}") from exc
        # Streamable HTTP servers may return a single JSON-RPC message as SSE.
        for line in body.splitlines():
            if line.startswith("data:"):
                try: body=line[5:].strip(); break
                except Exception: pass
        try: result=json.loads(body)
        except Exception as exc: raise RuntimeError(f"MCP returned an unreadable response: {body[:500]}") from exc
        if result.get("error"): raise RuntimeError(result["error"].get("message","MCP tool error"))
        return result.get("result",{})
    def initialize(self):
        if self.session is None:
            self.rpc("initialize",{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"drawing-review-ui","version":"1.0"}})
            self.rpc("notifications/initialized")
    def tools(self):
        self.initialize(); return self.rpc("tools/list").get("tools",[])
    def call(self,name,args):
        self.initialize(); result=self.rpc("tools/call",{"name":name,"arguments":args})
        if result.get("isError"): raise RuntimeError("MCP tool error: "+"; ".join(x.get("text","") for x in result.get("content",[])))
        return result

def choose_tool(tools, configured, keywords):
    if configured:
        item=next((t for t in tools if t["name"]==configured),None)
        if not item: raise RuntimeError(f"Configured MCP tool '{configured}' was not found. Available: {', '.join(t['name'] for t in tools)}")
        return item
    for tool in tools:
        name=tool["name"].lower()
        if all(any(k in name for k in group) for group in keywords): return tool
    raise RuntimeError(f"Could not identify MCP tool. Set the tool name explicitly. Available: {', '.join(t['name'] for t in tools)}")

def tool_args(tool, values):
    schema=tool.get("inputSchema",{}); props=schema.get("properties",{}); args={}
    for key, spec in props.items():
        low=key.lower()
        if low in values: args[key]=values[low]; continue
        for alias,value in values.items():
            if alias in low or low in alias: args[key]=value; break
    missing=[x for x in schema.get("required",[]) if x not in args]
    if missing: raise RuntimeError(f"MCP tool {tool['name']} requires arguments that could not be mapped: {', '.join(missing)}. Tool schema: {json.dumps(schema)}")
    return args

def unpack_tool(result):
    structured=result.get("structuredContent")
    if structured is not None: return structured
    texts=[x.get("text","") for x in result.get("content",[]) if x.get("type")=="text"]
    joined="\n".join(texts)
    try: return json.loads(joined)
    except Exception: return {"text":joined}

def provider_json(pdf_name,pdf_bytes,dxf_files,provider,key,model=None,previous=None):
    instructions=SKILL.read_text(encoding="utf-8") if SKILL.exists() else "Extract a structured review JSON from the engineering drawing."
    dxf_summary="\n\n".join(f"FILE {f['name']}\n{f['content']}" for f in dxf_files)
    user_content=f"Skill instructions:\n{instructions}\n\nUploaded PDF: {pdf_name}\nThe PDF is attached as base64 separately.\n\nDXF files from MCP:\n{dxf_summary}\n\nReturn only a JSON object containing extracted features and a confidence field per feature."
    if previous is not None: user_content+=f"\n\nUpdated user-edited JSON to honor when regenerating the CAD model:\n{json.dumps(previous)}"
    if provider == "anthropic":
        model=model or "claude-sonnet-4-20250514"
        payload={"model":model,"max_tokens":8192,"system":"You are an engineering drawing extraction assistant. Return valid JSON only.","messages":[{"role":"user","content":[{"type":"document","source":{"type":"base64","media_type":"application/pdf","data":__import__('base64').b64encode(pdf_bytes).decode()}},{"type":"text","text":user_content}]}]}
        out=http_json("https://api.anthropic.com/v1/messages",payload,{"x-api-key":key,"anthropic-version":"2023-06-01"})
        raw="".join(c.get("text","") for c in out.get("content",[]) if c.get("type")=="text")
    elif provider == "openrouter":
        import base64
        model=model or "anthropic/claude-sonnet-4"
        payload={"model":model,"response_format":{"type":"json_object"},"messages":[{"role":"system","content":"You are an engineering drawing extraction assistant. Return valid JSON only."},{"role":"user","content":[{"type":"text","text":user_content},{"type":"image_url","image_url":{"url":"data:application/pdf;base64,"+base64.b64encode(pdf_bytes).decode()}}]}]}
        out=http_json("https://openrouter.ai/api/v1/chat/completions",payload,{"Authorization":"Bearer "+key,"HTTP-Referer":"http://127.0.0.1:8765","X-Title":"Drawing Review"})
        raw=out.get("choices",[{}])[0].get("message",{}).get("content","")
    else: raise RuntimeError("Choose Anthropic or OpenRouter in the provider selector.")
    raw=re.sub(r"^```(?:json)?\s*|\s*```$","",raw.strip(),flags=re.I)
    try: return json.loads(raw)
    except Exception as exc: raise RuntimeError("The model response was not valid JSON: "+raw[:500]) from exc

def field_values(pdf_name,pdf_bytes,dxf_files,review_json):
    import base64
    # Tool arguments are matched to common schema names and aliases.
    return {"pdf":base64.b64encode(pdf_bytes).decode(),"pdf_base64":base64.b64encode(pdf_bytes).decode(),"pdf_data":base64.b64encode(pdf_bytes).decode(),"pdf_name":pdf_name,"filename":pdf_name,"file_name":pdf_name,"dxf":dxf_files,"dxfs":dxf_files,"dxf_files":dxf_files,"json":review_json,"features":review_json,"review_json":review_json,"data":review_json}

class Handler(BaseHTTPRequestHandler):
    def send_json(self,status,value): json_response(self,status,value)
    def read_body(self):
        n=int(self.headers.get("Content-Length","0"))
        if n>80*1024*1024: raise ValueError("Request is larger than 80 MB")
        return self.rfile.read(n)
    def llm_settings(self):
        provider=self.headers.get("X-LLM-Provider","").strip().lower()
        key=self.headers.get("X-LLM-API-Key","").strip()
        model=self.headers.get("X-LLM-Model","").strip() or None
        if provider not in ("anthropic","openrouter"): raise ValueError("Select Anthropic or OpenRouter.")
        if not key: raise ValueError("Enter the API key for the selected provider.")
        return provider,key,model
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/api/config":
            self.send_json(200,{"provider":"Anthropic" if os.getenv("ANTHROPIC_API_KEY") else ("OpenRouter" if os.getenv("OPENROUTER_API_KEY") else None),"mcp_url":MCP_URL}); return
        file=ROOT/ ("index.html" if path in ("/","/index.html") else path.lstrip("/"))
        if not file.resolve().is_relative_to(ROOT) or not file.is_file(): self.send_error(404); return
        data=file.read_bytes(); ct=mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self.send_response(200); self.send_header("Content-Type",ct+ ("; charset=utf-8" if ct.startswith("text/") or ct=="application/javascript" else "")); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        path=urlparse(self.path).path
        try:
            if path=="/api/process":
                provider,key,model=self.llm_settings()
                from email.parser import BytesParser
                from email import policy
                raw=self.read_body(); msg=BytesParser(policy=policy.default).parsebytes((f"Content-Type: {self.headers.get('Content-Type')}\r\nMIME-Version: 1.0\r\n\r\n".encode())+raw)
                parts={p.get_param('name',header='content-disposition'):p for p in msg.iter_parts()}
                pdfpart=parts.get('pdf')
                if not pdfpart or not pdfpart.get_filename().lower().endswith('.pdf'): raise ValueError("Upload a PDF drawing")
                pdf_name=pdfpart.get_filename(); pdf_bytes=pdfpart.get_payload(decode=True)
                client=McpClient(); tools=client.tools()
                dxf_tool=choose_tool(tools,os.getenv("MCP_DXF_TOOL"),[("dxf",),("convert","extract","generate","create","export")])
                dxf_result=unpack_tool(client.call(dxf_tool["name"],tool_args(dxf_tool,field_values(pdf_name,pdf_bytes,[],None))))
                dxf_files=[]
                if isinstance(dxf_result,list): rows=dxf_result
                elif isinstance(dxf_result,dict): rows=dxf_result.get("files",dxf_result.get("dxfs",[dxf_result]))
                else: rows=[]
                for i,row in enumerate(rows):
                    if isinstance(row,str): dxf_files.append({"name":f"drawing-{i+1}.dxf","content":row})
                    elif isinstance(row,dict):
                        name=row.get("name",row.get("filename",f"drawing-{i+1}.dxf")); content=row.get("content",row.get("text",row.get("data","")))
                        dxf_files.append({"name":name,"content":content if isinstance(content,str) else json.dumps(content)})
                if not dxf_files and isinstance(dxf_result,dict) and dxf_result.get("text"): dxf_files=[{"name":"drawing.dxf","content":dxf_result["text"]}]
                if not dxf_files: raise RuntimeError("The DXF MCP tool returned no DXF file content. Return files: [{name, content}].")
                review=provider_json(pdf_name,pdf_bytes,dxf_files,provider,key,model)
                step_tool=choose_tool(tools,os.getenv("MCP_STEP_TOOL"),[("step","stp","cad","model"),("generate","create","render","build","export")])
                step_result=unpack_tool(client.call(step_tool["name"],tool_args(step_tool,field_values(pdf_name,pdf_bytes,dxf_files,review))))
                self.send_json(200,{"json":review,"dxf":dxf_files,"step":step_result,"step_tool":step_tool["name"]}); return
            if path=="/api/regenerate":
                provider,key,model=self.llm_settings()
                body=json.loads(self.read_body()); name=body.get("pdf_name"); pdf=__import__('base64').b64decode(body.get("pdf_base64","")); dxfs=body.get("dxf",[]); review=body.get("json")
                if not pdf or not isinstance(review,dict): raise ValueError("PDF and edited JSON are required")
                # Ask the selected model to apply the edited JSON in the context of
                # the original PDF and DXFs, then pass that result into the CAD tool.
                model_json=provider_json(name,pdf,dxfs,provider,key,model,previous=review)
                client=McpClient(); tools=client.tools(); step_tool=choose_tool(tools,os.getenv("MCP_STEP_TOOL"),[("step","stp","cad","model"),("generate","create","render","build","export")])
                result=unpack_tool(client.call(step_tool["name"],tool_args(step_tool,field_values(name,pdf,dxfs,model_json))))
                self.send_json(200,{"json":model_json,"step":result,"step_tool":step_tool["name"]}); return
            self.send_error(404)
        except Exception as exc: self.send_json(502,{"error":str(exc)})
    def log_message(self,fmt,*args): print("[review-ui] "+fmt%args)

if __name__=="__main__":
    address=("127.0.0.1",8765); print(f"Drawing review UI: http://{address[0]}:{address[1]}"); print("Configure provider and MCP environment variables before launch."); ThreadingHTTPServer(address,Handler).serve_forever()
