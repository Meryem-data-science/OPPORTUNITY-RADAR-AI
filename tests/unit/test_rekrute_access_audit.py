"""Offline tests for the ReKrute public-access audit (Phase 7C.3A).

The audit's network edge is exercised through `httpx.MockTransport`, so these
tests open no socket and reach no third party. The HTML below is minimal and
synthetic: it exists to pin *this repository's* behaviour (obey robots, stop at
a wall, report structure not content), never to describe ReKrute's real markup,
which 7C.3A was unable to observe.
"""

from __future__ import annotations

import httpx
import pytest

from evaluation.morocco_pfe.cli import rekrute_access_audit as cli
from evaluation.morocco_pfe.rekrute_access import (
    MAX_DETAIL_LIMIT,
    RekruteAccessError,
    detect_access_barrier,
    extract_rekrute_links,
    parse_robots_txt,
    path_shapes,
    robots_verdict,
    summarize_jsonld,
    validate_detail_limit,
)


class TestDetailLimit:
    def test_default_is_bounded_and_within_the_hard_maximum(self) -> None:
        assert cli.DEFAULT_AUDIT_URL.startswith("https://www.rekrute.com")
        assert 1 <= cli.build_parser().parse_args([]).limit <= MAX_DETAIL_LIMIT
        assert MAX_DETAIL_LIMIT <= 10

    @pytest.mark.parametrize("value", [1, 5, MAX_DETAIL_LIMIT])
    def test_accepts_values_inside_the_bound(self, value: int) -> None:
        assert validate_detail_limit(value) == value

    @pytest.mark.parametrize("value", [0, -1, MAX_DETAIL_LIMIT + 1, 100])
    def test_rejects_values_outside_the_bound(self, value: int) -> None:
        with pytest.raises(RekruteAccessError):
            validate_detail_limit(value)

    @pytest.mark.parametrize("value", [True, False, 1.5, "5", None])
    def test_rejects_bool_and_non_integers(self, value: object) -> None:
        # `True` is an `int`; a flag must never quietly mean "fetch one page".
        with pytest.raises(RekruteAccessError):
            validate_detail_limit(value)


class TestRobots:
    def test_disallow_blocks_a_matching_path(self) -> None:
        groups = parse_robots_txt("User-agent: *\nDisallow: /private\n")
        assert robots_verdict(groups, "/private/x").allowed is False

    def test_longest_match_wins_and_allow_breaks_a_tie(self) -> None:
        groups = parse_robots_txt(
            "User-agent: *\nDisallow: /a\nAllow: /a/public\n"
        )
        assert robots_verdict(groups, "/a/secret").allowed is False
        assert robots_verdict(groups, "/a/public/1").allowed is True

    def test_empty_disallow_allows_everything(self) -> None:
        groups = parse_robots_txt("User-agent: *\nDisallow:\n")
        assert robots_verdict(groups, "/anything").allowed is True

    def test_wildcard_and_end_anchor_are_honoured(self) -> None:
        groups = parse_robots_txt("User-agent: *\nDisallow: /*.pdf$\n")
        assert robots_verdict(groups, "/a/b.pdf").allowed is False
        assert robots_verdict(groups, "/a/b.pdf.html").allowed is True

    def test_comments_and_blank_lines_are_tolerated(self) -> None:
        groups = parse_robots_txt("# c\n\nUser-agent: *\nDisallow: /x # trailing\n")
        assert robots_verdict(groups, "/x").allowed is False

    def test_absent_rules_default_to_allowed(self) -> None:
        assert robots_verdict((), "/anything").allowed is True

    def test_verdict_names_the_deciding_rule(self) -> None:
        groups = parse_robots_txt("User-agent: *\nDisallow: /private\n")
        verdict = robots_verdict(groups, "/private")
        assert verdict.matched_rule == "Disallow: /private"


class TestStructureDiscovery:
    def test_reports_jsonld_types_and_job_posting_keys_only(self) -> None:
        html = (
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","title":"T","datePosted":"2026-01-01"}'
            "</script>"
        )
        summary = summarize_jsonld(html)
        assert summary.job_posting_count == 1
        assert summary.types == ("JobPosting",)
        # Key names are evidence; values are third-party content and stay out.
        assert summary.job_posting_keys == ("@type", "datePosted", "title")

    def test_follows_graph_containers(self) -> None:
        html = (
            '<script type="application/ld+json">'
            '{"@graph":[{"@type":"JobPosting","title":"T"}]}</script>'
        )
        assert summarize_jsonld(html).job_posting_count == 1

    def test_counts_invalid_blocks_without_raising(self) -> None:
        summary = summarize_jsonld('<script type="application/ld+json">{ oops </script>')
        assert summary.invalid_block_count == 1
        assert summary.job_posting_count == 0

    def test_a_page_without_jsonld_reports_nothing_rather_than_guessing(self) -> None:
        summary = summarize_jsonld("<html><body><h1>x</h1></body></html>")
        assert summary.block_count == 0
        assert summary.job_posting_count == 0
        assert summary.job_posting_keys == ()

    def test_extracts_deduplicates_and_rejects_foreign_links(self) -> None:
        html = (
            '<a href="/o/1">a</a>'
            '<a href="https://www.rekrute.com/o/1#frag">dup</a>'
            '<a href="https://www.example.com/o/2">foreign</a>'
            '<a href="javascript:void(0)">bad</a>'
        )
        links = extract_rekrute_links(html, "https://www.rekrute.com/list")
        assert links == ("https://www.rekrute.com/o/1",)

    def test_malformed_html_does_not_raise(self) -> None:
        assert extract_rekrute_links("<a href=<<>>", "https://www.rekrute.com/") == ()
        assert extract_rekrute_links("<<not html at all", "https://www.rekrute.com/") == ()

    def test_path_shapes_group_numeric_paths(self) -> None:
        shapes = dict(
            path_shapes(
                ("https://www.rekrute.com/o/1", "https://www.rekrute.com/o/2")
            )
        )
        assert shapes == {"/o/#": 2}


class TestBarrierDetection:
    @pytest.mark.parametrize(
        "status,expected",
        [(403, "HTTP_403_FORBIDDEN"), (429, "HTTP_429_RATE_LIMITED"),
         (401, "AUTHENTICATION_REQUIRED"), (503, "HTTP_503_SERVER_ERROR")],
    )
    def test_named_http_barriers(self, status: int, expected: str) -> None:
        assert detect_access_barrier(status, "https://www.rekrute.com/", "x" * 500) == expected

    def test_captcha_body_is_reported_not_bypassed(self) -> None:
        body = "<html><body>Please complete the reCAPTCHA " + "x" * 400 + "</body></html>"
        assert detect_access_barrier(200, "https://www.rekrute.com/", body) == (
            "BOT_CHALLENGE_OR_CAPTCHA"
        )

    def test_login_redirect_is_reported(self) -> None:
        assert detect_access_barrier(
            200, "https://www.rekrute.com/login", "x" * 500
        ) == "REDIRECTED_TO_LOGIN"

    def test_empty_body_is_flagged_as_possibly_javascript_only(self) -> None:
        assert detect_access_barrier(200, "https://www.rekrute.com/", "") == (
            "EMPTY_OR_JAVASCRIPT_ONLY_BODY"
        )

    def test_an_ordinary_public_page_has_no_barrier(self) -> None:
        body = "<html><body>" + "contenu " * 100 + "</body></html>"
        assert detect_access_barrier(200, "https://www.rekrute.com/o/1", body) is None


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


_ROBOTS_OPEN = "User-agent: *\nDisallow: /admin\n"
_PAGE = "<html><body>" + "contenu " * 100 + '<a href="/o/1">o</a></body></html>'


class TestAuditRun:
    def test_reports_structure_and_completes(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=_ROBOTS_OPEN)
            return httpx.Response(200, text=_PAGE)

        report = cli.run("https://www.rekrute.com/list", 5, client=_client(handler))
        assert report["outcome"] == "COMPLETED"
        assert report["exit_code"] == cli.EXIT_OK
        assert report["robots"]["verdict"]["allowed"] is True
        assert report["discovered_links"] == 1
        assert report["authenticated"] is False
        assert report["wrote_database"] is False

    def test_robots_disallow_stops_the_audit_before_fetching(self) -> None:
        fetched: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            fetched.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /list\n")
            return httpx.Response(200, text=_PAGE)

        report = cli.run("https://www.rekrute.com/list", 5, client=_client(handler))
        assert report["outcome"] == "ROBOTS_DISALLOWED"
        assert report["exit_code"] == cli.EXIT_ROBOTS_DISALLOWED
        assert fetched == ["/robots.txt"]

    def test_a_403_wall_is_reported_and_not_worked_around(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=_ROBOTS_OPEN)
            return httpx.Response(403, text="Access Denied")

        report = cli.run("https://www.rekrute.com/list", 5, client=_client(handler))
        assert report["outcome"] == "BARRIER"
        assert report["exit_code"] == cli.EXIT_BARRIER
        assert report["pages"][0]["barrier"] == "HTTP_403_FORBIDDEN"

    def test_a_network_failure_is_named_not_retried(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            raise httpx.ConnectError("blocked", request=request)

        report = cli.run("https://www.rekrute.com/list", 5, client=_client(handler))
        assert report["outcome"] == "ROBOTS_UNREACHABLE"
        assert report["robots"]["error"].startswith("NETWORK_ERROR")
        assert calls == ["/robots.txt"]

    def test_a_timeout_is_reported_explicitly(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        report = cli.run("https://www.rekrute.com/", 5, client=_client(handler))
        assert report["robots"]["error"] == "TIMEOUT"

    def test_following_links_respects_the_limit_and_stays_sequential(self) -> None:
        many = "".join(f'<a href="/o/{index}">o</a>' for index in range(30))
        page = "<html><body>" + "contenu " * 100 + many + "</body></html>"
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=_ROBOTS_OPEN)
            return httpx.Response(200, text=page)

        report = cli.run(
            "https://www.rekrute.com/list", 3, follow_links=True,
            delay=0.0, client=_client(handler),
        )
        # One listing observation plus at most `limit` sampled pages.
        assert report["pages_fetched"] == 4
        assert len([path for path in requested if path.startswith("/o/")]) == 3

    def test_links_disallowed_by_robots_are_skipped_while_following(self) -> None:
        page = "<html><body>" + "contenu " * 100 + '<a href="/admin/1">a</a></body></html>'

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=_ROBOTS_OPEN)
            assert not request.url.path.startswith("/admin")
            return httpx.Response(200, text=page)

        report = cli.run(
            "https://www.rekrute.com/list", 5, follow_links=True,
            delay=0.0, client=_client(handler),
        )
        assert report["pages_fetched"] == 1

    def test_a_foreign_url_is_refused_before_any_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("no request should be made")

        with pytest.raises(RekruteAccessError):
            cli.run("https://www.example.com/list", 5, client=_client(handler))

    def test_an_out_of_range_limit_is_refused_before_any_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("no request should be made")

        with pytest.raises(RekruteAccessError):
            cli.run("https://www.rekrute.com/", 99, client=_client(handler))


class TestAuditSafety:
    def test_the_audit_never_sends_credentials_or_drives_a_browser(self) -> None:
        source = cli.__file__
        assert source is not None
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        for forbidden in (
            "selenium", "playwright", "webdriver", "Authorization",
            "cookies=", "sqlite3", "libsql", "proxies=",
        ):
            assert forbidden not in text

    def test_only_get_requests_are_issued(self) -> None:
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=_ROBOTS_OPEN)
            return httpx.Response(200, text=_PAGE)

        cli.run(
            "https://www.rekrute.com/list", 2, follow_links=True,
            delay=0.0, client=_client(handler),
        )
        assert set(methods) == {"GET"}

    def test_the_report_carries_no_page_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=_ROBOTS_OPEN)
            return httpx.Response(200, text=_PAGE + "SECRET_DESCRIPTION_TEXT")

        report = cli.run("https://www.rekrute.com/list", 5, client=_client(handler))
        assert "SECRET_DESCRIPTION_TEXT" not in repr(report)
