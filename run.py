#!/usr/bin/env python3
"""
Production Validator - Application Entry Point

Run with:
    python run.py

Or directly:
    uvicorn backend.api.main:app --reload --port 8000

Then open: http://localhost:8000/ui
"""
import os
import sys
import uvicorn
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

# Mount static frontend
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from backend.api.main import app

# Serve frontend
frontend_path = Path(__file__).parent / "frontend"
if frontend_path.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")

    @app.get("/ui")
    async def serve_ui():
        return FileResponse(str(frontend_path / "index.html"))
    
    @app.get("/")
    async def root():
        return FileResponse(str(frontend_path / "index.html"))


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"""
╔══════════════════════════════════════════════╗
║     Production Validator · Visa Systems      ║
╠══════════════════════════════════════════════╣
║  Web UI:  http://localhost:{port}/ui           ║
║  API:     http://localhost:{port}/api          ║
║  Docs:    http://localhost:{port}/docs         ║
╚══════════════════════════════════════════════╝
    """)
    uvicorn.run(
        "run:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        reload_dirs=["backend"]
    )
