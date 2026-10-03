"""
parsers router — OTA parser manifest and download.

Endpoints
─────────
GET  /parsers/manifest              authenticated — returns active parser catalog
GET  /parsers/download/{source}     authenticated — streams the .py file
POST /admin/parsers/register        admin only    — register a manually-dropped parser

Parser file workflow
────────────────────
1. Drop the updated .py file on the server at:
       $PARSERS_PATH/{source}.py          (default: data/parsers/{source}.py)
2. Call POST /admin/parsers/register with { source, version }
   The server reads the file, computes SHA256, and upserts the parsers table.
3. Desktop apps fetch /parsers/manifest on next startup and pull the new version.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import text
from arthdesk_db import Database

from app.auth_db import get_auth_db
from app.routers.deps import get_current_user, require_admin

router = APIRouter()

_PARSERS_PATH = Path(os.getenv("PARSERS_PATH", "data/parsers"))


# ── Schemas ───────────────────────────────────────────────────────────────────

class RegisterParserRequest(BaseModel):
    source:  str   # e.g. "angel_one"
    version: str   # e.g. "1.2.0"


# ── Authenticated endpoints ───────────────────────────────────────────────────

@router.get("/manifest")
def get_manifest(
    user: dict = Depends(get_current_user),
    auth_db: Database = Depends(get_auth_db),
):
    """
    Return the list of active OTA parsers with version and checksum.
    Desktop app diffs this against its local ota_parsers table.
    """
    rows = auth_db.execute(
        text("""
            SELECT source, version, checksum_sha256
            FROM parsers
            WHERE is_active = 1
            ORDER BY source
        """)
    ).fetchall()

    return {
        "parsers": [
            {
                "source":          r[0],
                "version":         r[1],
                "checksum_sha256": r[2],
            }
            for r in rows
        ]
    }


@router.get("/download/{source}")
def download_parser(
    source: str,
    user: dict = Depends(get_current_user),
    auth_db: Database = Depends(get_auth_db),
):
    """
    Stream the .py parser file for the given source.
    Returns 404 if not registered or not active.
    """
    row = auth_db.execute(
        text("""
            SELECT file_name FROM parsers
            WHERE source = :source AND is_active = 1
        """),
        {"source": source},
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"Parser '{source}' not found")

    file_path = _PARSERS_PATH / row[0]
    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Parser file for '{source}' not found on server",
        )

    return FileResponse(
        path=str(file_path),
        media_type="text/plain",
        filename=row[0],
    )


# ── Admin endpoints ───────────────────────────────────────────────────────────

@router.post("/register", dependencies=[Depends(require_admin)])
def register_parser(
    body: RegisterParserRequest,
    auth_db: Database = Depends(get_auth_db),
):
    """
    Register a parser version that has already been dropped on the server at
    $PARSERS_PATH/{source}.py

    Computes SHA256 from the file, then upserts the parsers table.
    Marks the new version active and deactivates any old version for the same source.
    """
    file_name = f"{body.source}.py"
    file_path = _PARSERS_PATH / file_name

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"File not found at {file_path}. Drop the .py file there first.",
        )

    checksum = hashlib.sha256(file_path.read_bytes()).hexdigest()

    auth_db.execute(
        text("""
            INSERT INTO parsers (source, version, checksum_sha256, file_name, is_active, updated_at)
            VALUES (:source, :version, :checksum, :file_name, 1, datetime('now'))
            ON CONFLICT(source) DO UPDATE SET
                version         = excluded.version,
                checksum_sha256 = excluded.checksum_sha256,
                file_name       = excluded.file_name,
                is_active       = 1,
                updated_at      = excluded.updated_at
        """),
        {
            "source":    body.source,
            "version":   body.version,
            "checksum":  checksum,
            "file_name": file_name,
        },
    )
    auth_db.commit()

    return {
        "source":          body.source,
        "version":         body.version,
        "checksum_sha256": checksum,
        "file_name":       file_name,
    }


@router.delete("/{source}", dependencies=[Depends(require_admin)])
def deactivate_parser(
    source: str,
    auth_db: Database = Depends(get_auth_db),
):
    """Deactivate a parser so it no longer appears in the manifest."""
    result = auth_db.execute(
        text("UPDATE parsers SET is_active = 0 WHERE source = :source"),
        {"source": source},
    )
    auth_db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"Parser '{source}' not found")
    return {"source": source, "is_active": False}
