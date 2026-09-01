"""The vault landing page and the navigation shared by all human pages."""

import asyncio

from starlette.requests import Request
from starlette.routing import Route

from app.vault import landing_page as landing
from app.vault.console_page import CONSOLE_HEADERS
from app.vault.templating import TEMPLATE_DIRECTORY, render


def _source(name: str) -> str:
    return (TEMPLATE_DIRECTORY / name).read_text(encoding="utf-8")


def _page() -> str:
    return render("landing.html")


def test_the_landing_route_is_registered_at_vault() -> None:
    routes = landing.build_vault_landing_routes()

    assert [route.path for route in routes if isinstance(route, Route)] == [
        landing.LANDING_PATH
    ]
    assert routes[0].methods == {"GET", "HEAD"}


def test_the_landing_page_carries_the_shared_browser_policy() -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": landing.LANDING_PATH,
        "headers": [],
        "query_string": b"",
    }

    response = asyncio.run(landing.vault_landing(Request(scope)))

    for header, value in CONSOLE_HEADERS.items():
        assert response.headers[header] == value


def test_the_landing_page_summarizes_the_governed_content() -> None:
    page = _page()

    for content_type in ("Notes", "Wiki pages", "Review queues"):
        assert content_type in page
    assert "focused notes and compiled wiki pages" in page


def test_the_landing_page_points_to_browse_and_review() -> None:
    page = _page()

    assert 'href="/vault/browse"' in page
    assert 'href="/vault/review"' in page
    assert "Open Browse" in page
    assert "Open Review" in page


def test_all_three_pages_include_one_shared_header() -> None:
    for template in ("landing.html", "browse.html", "review.html"):
        source = _source(template)
        assert '{% include "_vault_header.html" %}' in source
        assert "<header" not in source

    header = _source("_vault_header.html")
    assert header.count("<header") == 1
    for path in ("/vault", "/vault/browse", "/vault/review"):
        assert f'href="{path}"' in header


def test_the_landing_header_marks_home_current_and_holds_no_session() -> None:
    page = _page()

    assert 'href="/vault" aria-current="page"' in page
    for control in ('id="who"', 'id="signin"', 'id="signout"'):
        assert control not in page
