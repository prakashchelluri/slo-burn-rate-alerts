"""Minimal Grafana dashboard for one SLO, built on the generated recording rules."""

from __future__ import annotations

from .generator import SLOSpec, fmt_num

DS = {"type": "prometheus", "uid": "${datasource}"}


def _panel(pid: int, title: str, kind: str, targets: list[tuple[str, str]], grid: tuple, unit: str, **extra) -> dict:
    x, y, w, h = grid
    return {
        "id": pid,
        "type": kind,
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit, **extra}, "overrides": []},
        "targets": [
            {"refId": chr(65 + i), "datasource": DS, "expr": expr, "legendFormat": legend}
            for i, (expr, legend) in enumerate(targets)
        ],
    }


def build_dashboard(spec: SLOSpec) -> dict:
    sel, budget = spec.selector, fmt_num(spec.budget)
    # Budget remaining is computed from the raw SLI over the whole SLO window (accurate,
    # but a heavy query on a long window with many series).
    window_ratio = " ".join(spec.error_ratio_expr(spec.window).split())
    return {
        "title": f"SLO / {spec.service} / {spec.slo}",
        "uid": f"slo-{spec.service}-{spec.slo}"[:40],
        "description": spec.description,
        "tags": ["slo", spec.service],
        "schemaVersion": 39,
        "time": {"from": "now-7d", "to": "now"},
        "templating": {"list": [{"name": "datasource", "type": "datasource", "query": "prometheus"}]},
        "panels": [
            _panel(1, f"SLI (objective {spec.objective}%)", "timeseries",
                   [(f"1 - slo:sli_error:ratio_rate5m{sel}", "5m"), (f"1 - slo:sli_error:ratio_rate1h{sel}", "1h")],
                   (0, 0, 12, 8), "percentunit"),
            _panel(2, f"Error budget remaining ({spec.window})", "stat",
                   [(f"1 - ({window_ratio}) / {budget}", "remaining")],
                   (12, 0, 12, 8), "percentunit",
                   thresholds={"mode": "absolute", "steps": [
                       {"color": "red", "value": None}, {"color": "orange", "value": 0.25},
                       {"color": "green", "value": 0.5}]}),
            _panel(3, "Burn rate (1 = budget lasts exactly the SLO window)", "timeseries",
                   [(f"slo:sli_error:ratio_rate{w}{sel} / {budget}", w) for w in ("5m", "1h", "6h", "1d", "3d")],
                   (0, 8, 24, 8), "none"),
        ],
    }
