from http.server import BaseHTTPRequestHandler
import json
import sys
import os

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        diag = {
            "status": "ok",
            "python_version": sys.version,
            "cwd": os.getcwd(),
            "sys_path": sys.path,
        }
        
        # Test imports one by one
        for mod in ["fastapi", "pydantic", "sqlalchemy", "psycopg2", "cryptography", "jwt", "langchain", "langgraph"]:
            try:
                __import__(mod)
                diag[mod] = "OK"
            except Exception as e:
                diag[mod] = f"ERROR: {e}"
        
        # Test importing backend.main
        try:
            parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if parent not in sys.path:
                sys.path.insert(0, parent)
            import backend.main
            diag["backend.main"] = "OK"
        except Exception as e:
            import traceback
            diag["backend.main"] = f"ERROR: {e}\n{traceback.format_exc()}"

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(diag, indent=2).encode("utf-8"))
        return
