from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes import api

app = FastAPI(title="Elephant Edge ABM System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # same shared dashboard frontend as Synefi
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api.router, prefix="/api")

# No Base.metadata.create_all() here -- the shared schema is owned and migrated by Synefi's
# backend (synefi/app/main.py + the manual migrations run against the shared database).
# This backend only ever reads/writes rows within tables that already exist.


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "elephant-edge-backend"}
