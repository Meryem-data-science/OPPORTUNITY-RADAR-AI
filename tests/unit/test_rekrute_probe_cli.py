import json

from services.collector.cli.rekrute_probe import main
from tests.unit.test_rekrute_collector import DETAIL_HTML, OFFER_1, FakeClient, robots


def test_probe_outputs_only_safe_typed_summary(capsys) -> None:
    search_url = "https://www.rekrute.com/offres.html?keyword=data-engineer"
    client = FakeClient({
        "https://www.rekrute.com/robots.txt": robots("Allow: /"),
        search_url: f'<a href="{OFFER_1}">offer</a>',
        OFFER_1: DETAIL_HTML,
    })
    assert main(["--query", "data-engineer", "--limit", "1", "--pause", "0"], client=client) == 0
    lines = capsys.readouterr().out.splitlines()
    status, summary = map(json.loads, lines)
    assert status["status"] == "success"
    assert summary["source_external_id"] == "184203"
    assert summary["description_length"] > 0
    assert "description" not in summary
    assert "pipelines" not in "\n".join(lines)
    assert "<html" not in "\n".join(lines)


def test_probe_reports_robots_denied_safely(capsys) -> None:
    client = FakeClient({"https://www.rekrute.com/robots.txt": robots("Disallow: /")})
    assert main(["--limit", "1"], client=client) == 1
    output = capsys.readouterr().out
    assert '"status": "robots_denied"' in output
    assert "Disallow" not in output
