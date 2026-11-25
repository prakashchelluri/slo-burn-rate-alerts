"""Turn an SLO spec into Prometheus recording rules and multi-window, multi-burn-rate alerts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
WINDOW_PLACEHOLDER = "{{window}}"
RECORD_WINDOWS = ("5m", "30m", "1h", "2h", "6h", "1d", "3d")


@dataclass(frozen=True)
class AlertPolicy:
    """Alert when `budget_spent` of the whole error budget is consumed within `long_window`.

    `short_window` (long/12) must also be burning, so the alert resets quickly once the
    problem stops instead of firing for the full length of the long window.
    """

    severity: str
    budget_spent: float
    long_window: str
    short_window: str


# The first, second and fourth rows are the Google SRE Workbook recommendation
# ("Alerting on SLOs", multiwindow multi-burn-rate). The 1d/2h ticket is a common
# extra step between 6h and 3d. For a 30d window they come out at 14.4x, 6x, 3x, 1x.
POLICIES = (
    AlertPolicy("page", 0.02, "1h", "5m"),
    AlertPolicy("page", 0.05, "6h", "30m"),
    AlertPolicy("ticket", 0.10, "1d", "2h"),
    AlertPolicy("ticket", 0.10, "3d", "6h"),
)


def parse_duration(text: str) -> int:
    """'30d' -> seconds. Accepts a single Prometheus-style unit: s, m, h, d, w."""
    m = re.fullmatch(r"(\d+)([smhdw])", str(text).strip())
    if not m or int(m.group(1)) == 0:
        raise ValueError(f"invalid duration {text!r}, expected e.g. 30d, 4w, 6h")
    return int(m.group(1)) * UNITS[m.group(2)]


def format_duration(seconds: float) -> str:
    """2592 -> '43m12s'. Sub-second remainders are kept to one decimal."""
    seconds = round(seconds, 1)
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{int(n)}{unit}")
    if seconds or not parts:
        parts.append(f"{seconds:g}s")
    return "".join(parts)


def error_budget(objective: float) -> float:
    """99.9 -> 0.001 (fraction of events allowed to be bad)."""
    return round(1 - objective / 100, 10)


def allowed_bad_seconds(objective: float, window: str) -> float:
    """Full-outage time the budget allows over `window`: 99.9 over 30d -> 2592s (43.2 min)."""
    return error_budget(objective) * parse_duration(window)


def burn_rate(policy: AlertPolicy, slo_window: str) -> float:
    """Burn rate at which `policy.budget_spent` of the budget is gone within `policy.long_window`.

    A burn rate of 1 spends exactly the whole budget over the SLO window, so spending
    fraction f of it within long window L needs a rate of f * slo_window / L.
    """
    return round(policy.budget_spent * parse_duration(slo_window) / parse_duration(policy.long_window), 4)


def fmt_num(x: float) -> str:
    return f"{x:g}"


@dataclass
class SLOSpec:
    service: str
    slo: str
    objective: float
    window: str
    sli: dict
    description: str = ""
    labels: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "SLOSpec":
        if not isinstance(d, dict):
            raise ValueError("SLO spec must be a mapping")
        missing = {"service", "slo", "objective", "window", "sli"} - d.keys()
        if missing:
            raise ValueError(f"missing keys: {', '.join(sorted(missing))}")
        unknown = d.keys() - {f for f in cls.__dataclass_fields__}
        if unknown:
            raise ValueError(f"unknown keys: {', '.join(sorted(unknown))}")
        spec = cls(**d)
        spec.validate()
        return spec

    def validate(self) -> None:
        for key in ("service", "slo"):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(getattr(self, key))):
                raise ValueError(f"{key} must match [A-Za-z0-9_.-]+")
        if not 0 < float(self.objective) < 100:
            raise ValueError("objective must be a percentage between 0 and 100, e.g. 99.9")
        parse_duration(self.window)
        sli = self.sli or {}
        if "total" not in sli or len({"good", "errors"} & sli.keys()) != 1:
            raise ValueError("sli needs 'total' and exactly one of 'good' or 'errors'")
        if sli.keys() - {"good", "errors", "total"}:
            raise ValueError("sli accepts only 'good', 'errors' and 'total'")
        for k, q in sli.items():
            if WINDOW_PLACEHOLDER not in str(q):
                raise ValueError(f"sli.{k} must contain {WINDOW_PLACEHOLDER} as the range, e.g. rate(x[{{{{window}}}}])")
        for k, v in {**self.labels, **self.annotations}.items():
            if not isinstance(v, str):
                raise ValueError(f"label/annotation {k!r} must be a string")

    @property
    def budget(self) -> float:
        return error_budget(float(self.objective))

    @property
    def selector(self) -> str:
        return f'{{service="{self.service}", slo="{self.slo}"}}'

    def error_ratio_expr(self, window: str) -> str:
        """Raw PromQL for the fraction of bad events over `window`."""
        q = {k: " ".join(str(v).split()).replace(WINDOW_PLACEHOLDER, window) for k, v in self.sli.items()}
        if "errors" in q:
            return f"({q['errors']})\n/\n({q['total']})"
        return f"1 - (\n  ({q['good']})\n  /\n  ({q['total']})\n)"


def build_rules(spec: SLOSpec) -> dict:
    """Return a Prometheus rule file (as a dict) with recording and alerting groups."""
    ids = {"service": spec.service, "slo": spec.slo}
    records = [
        {"record": f"slo:sli_error:ratio_rate{w}", "expr": spec.error_ratio_expr(w), "labels": dict(ids)}
        for w in RECORD_WINDOWS
    ]
    budget = spec.budget
    alerts = []
    for p in POLICIES:
        rate = burn_rate(p, spec.window)
        threshold = f"({fmt_num(rate)} * {fmt_num(budget)})"
        exhaust = format_duration(parse_duration(spec.window) / rate)
        alerts.append({
            "alert": "ErrorBudgetBurn",
            "expr": (
                f"slo:sli_error:ratio_rate{p.long_window}{spec.selector} > {threshold}\n"
                f"and\n"
                f"slo:sli_error:ratio_rate{p.short_window}{spec.selector} > {threshold}"
            ),
            "labels": {**spec.labels, **ids, "severity": p.severity,
                       "long_window": p.long_window, "short_window": p.short_window},
            "annotations": {
                "summary": f"{spec.service} {spec.slo} SLO is burning error budget at {fmt_num(rate)}x",
                "description": (
                    f"Error ratio over {p.long_window} and {p.short_window} is above {fmt_num(rate)}x the "
                    f"{fmt_num(budget * 100)}% budget of the {spec.objective}%/{spec.window} SLO. "
                    f"{fmt_num(p.budget_spent * 100)}% of the budget is spent per {p.long_window} at this rate; "
                    f"all of it in {exhaust}."
                ),
                **spec.annotations,
            },
        })
    prefix = f"slo-{spec.service}-{spec.slo}"
    return {"groups": [
        {"name": f"{prefix}-recordings", "rules": records},
        {"name": f"{prefix}-alerts", "rules": alerts},
    ]}


def budget_report(objective: float, window: str) -> str:
    """Human-readable error-budget and alert-threshold summary."""
    if not 0 < objective < 100:
        raise ValueError("objective must be a percentage between 0 and 100, e.g. 99.9")
    budget = error_budget(objective)
    slo_s = parse_duration(window)
    lines = [
        f"SLO {fmt_num(objective)}% over {window}: error budget {fmt_num(budget * 100)}% of events",
        f"Allowed full-outage time: {format_duration(allowed_bad_seconds(objective, window))} per {window} "
        f"({fmt_num(round(allowed_bad_seconds(objective, window) / 60, 2))} min)",
        "",
        f"{'period':<8}{'allowed bad time':>18}",
    ]
    for period in dict.fromkeys((window, "7d", "1d", "1h")):
        lines.append(f"{period:<8}{format_duration(budget * parse_duration(period)):>18}")
    lines += ["", f"{'severity':<10}{'long':<6}{'short':<7}{'budget spent':>13}{'burn rate':>11}"
                  f"{'error ratio >':>15}{'budget gone in':>16}"]
    for p in POLICIES:
        rate = burn_rate(p, window)
        lines.append(
            f"{p.severity:<10}{p.long_window:<6}{p.short_window:<7}{fmt_num(p.budget_spent * 100) + '%':>13}"
            f"{fmt_num(rate) + 'x':>11}{fmt_num(round(rate * budget * 100, 6)) + '%':>15}"
            f"{format_duration(slo_s / rate):>16}"
        )
    return "\n".join(lines)
