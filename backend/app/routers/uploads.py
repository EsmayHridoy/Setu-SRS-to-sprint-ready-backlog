"""Document upload and text extraction.

The client sends a PDF or DOCX; this returns its plain text so the caller can
fold it into a chat message. Nothing is stored: extraction is stateless, the
bytes live only for the duration of the request. Authentication is still
required so anonymous callers cannot use the box as a free conversion service.

The actual parsing lives in `app.extraction`, shared with the chat upload
stream.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile

from .. import extraction
from ..models import User
from ..schemas import ExtractResult
from ..security import current_user

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


@router.post("/extract", response_model=ExtractResult)
async def extract(file: UploadFile = File(...),
                  _: User = Depends(current_user)):
    """Extract plain text from an uploaded PDF or DOCX."""
    data = await file.read()
    kind, text, truncated = extraction.extract(file.filename, file.content_type, data)
    return ExtractResult(
        filename=file.filename or f"document.{kind}",
        kind=kind,
        chars=len(text),
        truncated=truncated,
        text=text,
    )
