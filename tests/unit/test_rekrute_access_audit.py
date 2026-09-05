"""Offline tests for the ReKrute public-access audit (Phase 7C.3A).

The audit's network edge is exercised through `httpx.MockTransport`, so these
tests open no socket and reach no third party. The HTML below is minimal and
synthetic: it exists to pin *this repository's* behaviour (obey robots, stop at
a wall, report structure not content), never to describe ReKrute's real markup,
which 7C.3A was unable to observe.
"""

from __future__ import annotations

import json

import httpx
import pytest

from evaluation.morocco_pfe.cli import rekrute_access_audit as cli
from evaluation.morocco_pfe.rekrute_access import (
    AUTOMATION_TERM_MARKERS,
    MAX_DETAIL_LIMIT,
    ROBOTS_ABSENT,
    ROBOTS_BARRIER,
    ROBOTS_OBEY,
    ROBOTS_UNRESOLVED,
    TERMS_URL,
    automation_term_indicators,
    classify_robots_response,
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
        # The disallowed target is never requested. The terms page lives on a
        # different, separately-allowed path, so checking it is not a breach.
        assert "/list" not in fetched

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


def _robots_only_handler(
    robots: httpx.Response, page: httpx.Response | None = None
):
    """Serve robots.txt, the terms page and everything else, recording paths."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return robots
        return page if page is not None else httpx.Response(200, text=_PAGE)

    return handler, seen


class TestRobotsStatusPolicy:
    """A robots.txt we were refused tells us nothing, and silence is not consent."""

    def test_200_is_parsed_and_obeyed(self) -> None:
        assert classify_robots_response(
            200, "https://www.rekrute.com/robots.txt", _ROBOTS_OPEN
        ).disposition == ROBOTS_OBEY

    @pytest.mark.parametrize("status", [404, 410])
    def test_absent_robots_permits_the_audit_to_continue(self, status: int) -> None:
        disposition = classify_robots_response(status, "https://x/robots.txt", "")
        assert disposition.disposition == ROBOTS_ABSENT
        assert disposition.may_proceed is True

    @pytest.mark.parametrize("status", [401, 403, 407, 429])
    def test_refused_robots_is_a_barrier(self, status: int) -> None:
        disposition = classify_robots_response(status, "https://x/robots.txt", "")
        assert disposition.disposition == ROBOTS_BARRIER
        assert disposition.may_proceed is False

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_server_error_robots_is_unresolved(self, status: int) -> None:
        disposition = classify_robots_response(status, "https://x/robots.txt", "")
        assert disposition.disposition == ROBOTS_UNRESOLVED
        assert disposition.may_proceed is False

    def test_a_short_valid_robots_file_is_not_mistaken_for_a_wall(self) -> None:
        # Real robots.txt files are often a couple of lines; the generic
        # short-body heuristic used for HTML pages must not apply here.
        assert classify_robots_response(
            200, "https://x/robots.txt", "User-agent: *\nDisallow:\n"
        ).disposition == ROBOTS_OBEY

    def test_a_challenge_page_served_as_robots_is_a_barrier(self) -> None:
        assert classify_robots_response(
            200, "https://x/robots.txt", "<html>Please complete the captcha</html>"
        ).disposition == ROBOTS_BARRIER

    def test_robots_redirected_to_login_is_a_barrier(self) -> None:
        assert classify_robots_response(
            200, "https://www.rekrute.com/login", "User-agent: *"
        ).disposition == ROBOTS_BARRIER


class TestRobotsPolicyStopsTheAudit:
    """Behavioural proof: the target page is never fetched when robots is unusable."""

    @pytest.mark.parametrize("status", [401, 403, 407, 429])
    def test_refused_robots_never_fetches_the_target(self, status: int) -> None:
        handler, seen = _robots_only_handler(httpx.Response(status, text="no"))
        report = cli.run("https://www.rekrute.com/list", 5, client=_client(handler))
        assert report["outcome"] == "ROBOTS_BARRIER"
        assert report["exit_code"] == cli.EXIT_BARRIER
        assert seen == ["/robots.txt"]

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_server_error_robots_never_fetches_the_target(self, status: int) -> None:
        handler, seen = _robots_only_handler(httpx.Response(status, text="err"))
        report = cli.run("https://www.rekrute.com/list", 5, client=_client(handler))
        assert report["outcome"] == "ROBOTS_UNRESOLVED"
        assert report["exit_code"] == cli.EXIT_BARRIER
        assert seen == ["/robots.txt"]

    def test_absent_robots_allows_the_target_to_be_audited(self) -> None:
        handler, seen = _robots_only_handler(httpx.Response(404, text="not found"))
        report = cli.run("https://www.rekrute.com/list", 5, client=_client(handler))
        assert report["outcome"] == "COMPLETED"
        assert report["robots"]["disposition"] == ROBOTS_ABSENT
        assert report["robots"]["available"] is False
        assert "/list" in seen
        # Absence of a rule is never reported as permission.
        assert "not permission" in report["robots"]["note"]

    def test_a_stopped_audit_does_not_fetch_the_terms_page_either(self) -> None:
        handler, seen = _robots_only_handler(httpx.Response(403, text="no"))
        report = cli.run("https://www.rekrute.com/list", 5, client=_client(handler))
        assert "/conditions-utilisation.html" not in seen
        assert report["terms"]["manual_review_required"] is True


class TestSingleFetch:
    """The target page is fetched exactly once per audit."""

    def test_target_is_requested_exactly_once_without_following_links(self) -> None:
        handler, seen = _robots_only_handler(httpx.Response(200, text=_ROBOTS_OPEN))
        report = cli.run(
            "https://www.rekrute.com/list", 5, follow_links=False, client=_client(handler)
        )
        assert report["outcome"] == "COMPLETED"
        assert seen.count("/list") == 1
        # Structural findings still come out of that single response.
        assert report["discovered_links"] == 1
        assert report["path_shapes"]

    def test_target_is_requested_once_even_while_following_links(self) -> None:
        page = "<html><body>" + "contenu " * 100 + '<a href="/o/1">o</a></body></html>'
        handler, seen = _robots_only_handler(
            httpx.Response(200, text=_ROBOTS_OPEN), httpx.Response(200, text=page)
        )
        cli.run(
            "https://www.rekrute.com/list", 5, follow_links=True,
            delay=0.0, client=_client(handler),
        )
        assert seen.count("/list") == 1

    def test_the_transient_body_never_reaches_the_report(self) -> None:
        page = (
            "<html><body>" + "contenu " * 100
            + '<a href="/o/1">o</a>SECRET_BODY_MARKER</body></html>'
        )
        handler, _ = _robots_only_handler(
            httpx.Response(200, text=_ROBOTS_OPEN), httpx.Response(200, text=page)
        )
        report = cli.run("https://www.rekrute.com/list", 5, client=_client(handler))
        assert "SECRET_BODY_MARKER" not in json.dumps(report)


class TestTermsAudit:
    def test_terms_page_is_checked_once_and_reported_structurally(self) -> None:
        # Padded to a realistic length: a sub-200-character page is correctly
        # flagged as empty/JS-only by the generic barrier heuristic.
        terms_html = (
            "<html><body>" + "Conditions generales. " * 30
            + "Il est interdit d'utiliser un robot.</body></html>"
        )
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=_ROBOTS_OPEN)
            if request.url.path == "/conditions-utilisation.html":
                return httpx.Response(200, text=terms_html)
            return httpx.Response(200, text=_PAGE)

        report = cli.run("https://www.rekrute.com/list", 5, client=_client(handler))
        terms = report["terms"]
        assert terms["url"] == TERMS_URL
        assert terms["status_code"] == 200
        assert terms["available"] is True
        assert terms["barrier"] is None
        assert terms["manual_review_required"] is True
        assert seen.count("/conditions-utilisation.html") == 1
        assert terms["automation_terms_present"]["robot"] is True

    def test_terms_report_carries_no_page_text(self) -> None:
        terms_html = (
            "<html><body>" + "Conditions generales. " * 30
            + "CLAUSE_TEXT_MARKER robot</body></html>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=_ROBOTS_OPEN)
            if request.url.path == "/conditions-utilisation.html":
                return httpx.Response(200, text=terms_html)
            return httpx.Response(200, text=_PAGE)

        report = cli.run("https://www.rekrute.com/list", 5, client=_client(handler))
        assert "CLAUSE_TEXT_MARKER" not in json.dumps(report)

    def test_absent_markers_are_never_reported_as_permission(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=_ROBOTS_OPEN)
            if request.url.path == "/conditions-utilisation.html":
                return httpx.Response(
                    200,
                    text="<html><body>" + "Conditions generales. " * 30 + "</body></html>",
                )
            return httpx.Response(200, text=_PAGE)

        terms = cli.run(
            "https://www.rekrute.com/list", 5, client=_client(handler)
        )["terms"]
        assert all(value is False for value in terms["automation_terms_present"].values())
        # Still requires a human, and says so.
        assert terms["manual_review_required"] is True
        assert "NOT permission" in terms["note"]

    def test_a_barrier_on_the_terms_page_is_reported_honestly(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=_ROBOTS_OPEN)
            if request.url.path == "/conditions-utilisation.html":
                return httpx.Response(403, text="denied")
            return httpx.Response(200, text=_PAGE)

        terms = cli.run(
            "https://www.rekrute.com/list", 5, client=_client(handler)
        )["terms"]
        assert terms["barrier"] == "HTTP_403_FORBIDDEN"
        assert terms["available"] is False
        assert terms["manual_review_required"] is True

    def test_terms_disallowed_by_robots_is_not_fetched(self) -> None:
        robots = "User-agent: *\nDisallow: /conditions-utilisation.html\n"
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=robots)
            return httpx.Response(200, text=_PAGE)

        terms = cli.run(
            "https://www.rekrute.com/list", 5, client=_client(handler)
        )["terms"]
        assert terms["barrier"] == "ROBOTS_DISALLOWED"
        assert "/conditions-utilisation.html" not in seen

    def test_markers_ignore_markup_and_read_only_visible_text(self) -> None:
        # A `<meta name="robots">` tag is markup, not a statement in the terms.
        indicators = automation_term_indicators(
            '<meta name="robots" content="index"><body>Bonjour.</body>'
        )
        assert indicators["robot"] is False
        assert set(indicators) == set(AUTOMATION_TERM_MARKERS)
