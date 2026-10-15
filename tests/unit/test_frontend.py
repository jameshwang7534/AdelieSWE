"""The walkthrough is available without credentials and preserves API routing."""

from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


class Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        path = values.get("src" if tag == "script" else "href")
        if tag in {"script", "link"} and path:
            self.paths.append(path)


def test_walkthrough_and_assets_without_services(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        database_url=None,
        redis_url=None,
        celery_broker_url=None,
        celery_result_backend=None,
        github_token=None,
        llm_api_key=None,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "not a live run" in response.text
        parser = Assets()
        parser.feed(response.text)
        assert len(parser.paths) == 2
        for path in parser.paths:
            assert path.startswith("/assets/")
            asset = client.get(path)
            assert asset.status_code == 200
            assert len(asset.content) > 100
        assert client.get("/docs").status_code == 200
        assert client.get("/health").status_code == 200
        assert "/workflows" in client.get("/openapi.json").json()["paths"]
        assert client.get("/assets/%2e%2e/core/config.py").status_code == 404
        assert client.get("/assets/.env").status_code == 404
