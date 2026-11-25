import json
from pathlib import Path

import pytest
import yaml

from sloburn.cli import dump_rules, load_spec, main
from sloburn.dashboard import build_dashboard
from sloburn.generator import (
    POLICIES,
    RECORD_WINDOWS,
    SLOSpec,
    allowed_bad_seconds,
    budget_report,
    build_rules,
    burn_rate,
    error_budget,
    format_duration,
    parse_duration,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / "examples").glob("*.yaml"))

SPEC = {
    "service": "api",
    "slo": "availability",
    "objective": 99.9,
    "window": "30d",
    "sli": {
        "errors": 'sum(rate(http_requests_total{code=~"5.."}[{{window}}]))',
        "total": "sum(rate(http_requests_total[{{window}}]))",
    },
    "labels": {"team": "sre"},
    "annotations": {"runbook_url": "https://runbooks.example.com/api"},
}


# --- math -------------------------------------------------------------------

@pytest.mark.parametrize("text,seconds", [("5m", 300), ("1h", 3600), ("30d", 2592000), ("4w", 2419200)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["", "30", "1h30m", "0d", "-1d", "1y"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_budget_99_9_over_30d_is_43_2_minutes():
    assert error_budget(99.9) == 0.001
    assert allowed_bad_seconds(99.9, "30d") == pytest.approx(43.2 * 60)
    assert format_duration(allowed_bad_seconds(99.9, "30d")) == "43m12s"


@pytest.mark.parametrize("objective,window,expected", [
    (99.0, "30d", "7h12m"),
    (99.5, "7d", "50m24s"),
    (99.99, "30d", "4m19.2s"),
    (99.95, "28d", "20m9.6s"),
])
def test_allowed_bad_time(objective, window, expected):
    assert format_duration(allowed_bad_seconds(objective, window)) == expected


def test_burn_rates_for_30d_match_sre_workbook():
    assert [burn_rate(p, "30d") for p in POLICIES] == [14.4, 6, 3, 1]


def test_burn_rates_are_derived_from_window():
    # Same budget-consumption policy, shorter SLO window -> lower burn rate needed.
    assert [burn_rate(p, "28d") for p in POLICIES] == [13.44, 5.6, 2.8, 0.9333]


def test_burn_rate_spends_the_policy_share_of_budget():
    for p in POLICIES:
        spent = burn_rate(p, "30d") * parse_duration(p.long_window) / parse_duration("30d")
        assert spent == pytest.approx(p.budget_spent, rel=1e-3)
        assert parse_duration(p.long_window) == 12 * parse_duration(p.short_window)


def test_budget_report():
    report = budget_report(99.9, "30d")
    assert "43m12s per 30d (43.2 min)" in report
    assert "14.4x" in report and "1.44%" in report and "2d2h" in report


# --- spec validation --------------------------------------------------------

@pytest.mark.parametrize("change,msg", [
    ({"objective": 100}, "objective"),
    ({"objective": 0}, "objective"),
    ({"window": "month"}, "duration"),
    ({"service": "bad name"}, "service"),
    ({"sli": {"total": "x[{{window}}]"}}, "exactly one"),
    ({"sli": {"good": "x[{{window}}]", "errors": "x[{{window}}]", "total": "x[{{window}}]"}}, "exactly one"),
    ({"sli": {"errors": "x[5m]", "total": "x[{{window}}]"}}, "{{window}}"),
    ({"extra": 1}, "unknown"),
])
def test_spec_validation(change, msg):
    with pytest.raises(ValueError, match=msg.replace("{", r"\{").replace("}", r"\}")):
        SLOSpec.from_dict({**SPEC, **change})


def test_spec_missing_keys():
    with pytest.raises(ValueError, match="missing keys: objective"):
        SLOSpec.from_dict({k: v for k, v in SPEC.items() if k != "objective"})


# --- rule structure ---------------------------------------------------------

def test_recording_rules():
    rec = build_rules(SLOSpec.from_dict(SPEC))["groups"][0]["rules"]
    assert [r["record"] for r in rec] == [f"slo:sli_error:ratio_rate{w}" for w in RECORD_WINDOWS]
    one_hour = rec[RECORD_WINDOWS.index("1h")]
    assert "[1h]" in one_hour["expr"] and "{{window}}" not in one_hour["expr"]
    assert one_hour["labels"] == {"service": "api", "slo": "availability"}


def test_good_events_sli_is_inverted():
    spec = SLOSpec.from_dict({**SPEC, "sli": {"good": "g[{{window}}]", "total": "t[{{window}}]"}})
    assert spec.error_ratio_expr("5m").startswith("1 - (")


def test_alert_rules():
    alerts = build_rules(SLOSpec.from_dict(SPEC))["groups"][1]["rules"]
    assert len(alerts) == 4
    assert [a["labels"]["severity"] for a in alerts] == ["page", "page", "ticket", "ticket"]
    first = alerts[0]
    assert first["expr"] == (
        'slo:sli_error:ratio_rate1h{service="api", slo="availability"} > (14.4 * 0.001)\n'
        "and\n"
        'slo:sli_error:ratio_rate5m{service="api", slo="availability"} > (14.4 * 0.001)'
    )
    assert first["labels"]["team"] == "sre"
    assert first["annotations"]["runbook_url"] == "https://runbooks.example.com/api"
    # Every window an alert reads has a recording rule behind it.
    for p in POLICIES:
        assert {p.long_window, p.short_window} <= set(RECORD_WINDOWS)


def test_spec_labels_cannot_override_identity_labels():
    spec = SLOSpec.from_dict({**SPEC, "labels": {"severity": "info", "service": "other"}})
    alert = build_rules(spec)["groups"][1]["rules"][0]
    assert alert["labels"]["severity"] == "page" and alert["labels"]["service"] == "api"


def test_yaml_round_trip():
    rules = build_rules(SLOSpec.from_dict(SPEC))
    assert yaml.safe_load(dump_rules(rules, "spec.yaml")) == rules


def test_dashboard():
    dash = build_dashboard(SLOSpec.from_dict(SPEC))
    assert [p["type"] for p in dash["panels"]] == ["timeseries", "stat", "timeseries"]
    assert "[30d]" in dash["panels"][1]["targets"][0]["expr"]
    json.dumps(dash)


# --- examples and CLI -------------------------------------------------------

@pytest.mark.parametrize("spec_path", EXAMPLES, ids=lambda p: p.stem)
def test_committed_examples_are_up_to_date(spec_path, tmp_path):
    out, dash = tmp_path / "rules.yaml", tmp_path / "dash.json"
    assert main(["generate", str(spec_path), "-o", str(out), "--dashboard", str(dash)]) == 0
    generated = ROOT / "examples" / "generated"
    assert out.read_text() == (generated / f"{spec_path.stem}.rules.yaml").read_text(), "regenerate examples (see README)"
    assert dash.read_text() == (generated / f"{spec_path.stem}.dashboard.json").read_text()


def test_cli_budget(capsys):
    assert main(["budget", "--objective", "99.9", "--window", "30d"]) == 0
    assert "43m12s" in capsys.readouterr().out


def test_cli_reports_bad_spec(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("service: x\n")
    assert main(["generate", str(bad)]) == 1
    assert "missing keys" in capsys.readouterr().err


def test_examples_load():
    for path in EXAMPLES:
        load_spec(str(path))
