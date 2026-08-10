#!/usr/bin/env python3
"""GitHub webhook receiver — triggers deploy on push to main"""
import subprocess, hmac, hashlib, os
from http.server import HTTPServer, BaseHTTPRequestHandler

SECRET = os.environ.get("WEBHOOK_SECRET", "sparkart-deploy-2026")
PORT = int(os.environ.get("WEBHOOK_PORT", "9000"))

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        sig = self.headers.get("X-Hub-Signature-256", "")
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        
        # Verify signature
        expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            self.send_response(403)
            self.end_headers()
            return
        
        self.send_response(200)
        self.end_headers()
        
        # Deploy in background
        subprocess.Popen(["bash", "/var/www/star/deploy.sh"],
                         stdout=open("/var/log/star-deploy.log", "a"),
                         stderr=subprocess.STDOUT)
    
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"star webhook OK")

HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
