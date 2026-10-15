"""Local educational interface, independent of external services."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

ASSET_DIRECTORY = Path(__file__).resolve().parents[1] / "web"
router = APIRouter()


@router.get("/", include_in_schema=False)
def walkthrough() -> FileResponse:
    return FileResponse(ASSET_DIRECTORY / "index.html", media_type="text/html")
