# slo-burn-rate-alerts

Turn a ten-line SLO definition into Prometheus recording rules and multi-window, multi-burn-rate alerts.

> Lab project: a clean-room reference implementation of a pattern I use at work. It contains no employer code or data.

## The problem

Most teams start alerting on SLOs with something like `error_rate > 1% for 5m`. That pages on short blips that
barely touch the error budget, and stays silent through a slow leak that quietly spends the whole month's budget.
Hand-writing proper burn-rate alerts fixes that, but it means seven recording rules and four alert rules per SLO,
each with thresholds you have to work out by hand. Copy-pasted rules drift, and nobody can tell any more why a
threshold is `0.0144`.

This tool keeps the SLO as the source of truth (objective, window, SLI query) and derives everything else from it,
using the multi-window, multi-burn-rate approach from the Google SRE Workbook chapter "Alerting on SLOs".

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: py -3 -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"

sloburn budget --objective 99.9 --window 30d
sloburn generate examples/http-availability.yaml -o rules/checkout-availability.rules.yaml \
    --dashboard dashboards/checkout-availability.json
pytest -q
```

An SLO spec ([examples/http-availability.yaml](examples/http-availability.yaml)):

```yaml
service: checkout-api
slo: availability
objective: 99.9          # percent
window: 30d              # s, m, h, d or w
description: 99.9% of checkout-api HTTP requests succeed (non-5xx) over 30 days.
sli:                     # `total` plus exactly one of `errors` or `good`; {{window}} is filled in per rule
  errors: sum(rate(http_requests_total{job="checkout-api", code=~"5.."}[{{window}}]))
  total: sum(rate(http_requests_total{job="checkout-api"}[{{window}}]))
labels:                  # added to every alert
  team: payments
annotations:             # added to every alert
  runbook_url: https://runbooks.example.com/checkout-api/availability
```

A latency SLO uses `good` with a histogram bucket instead of `errors`; see
[examples/http-latency.yaml](examples/http-latency.yaml). Generated output for both examples is committed under
[examples/generated/](examples/generated/), and a test fails if it is out of date. Regenerate with:

```bash
for s in http-availability http-latency; do
  sloburn generate examples/$s.yaml -o examples/generated/$s.rules.yaml --dashboard examples/generated/$s.dashboard.json
done
```

## Example output

`sloburn budget examples/http-availability.yaml` (real run):

```
SLO 99.9% over 30d: error budget 0.1% of events
Allowed full-outage time: 43m12s per 30d (43.2 min)

period    allowed bad time
30d                 43m12s
7d                 10m4.8s
1d                 1m26.4s
1h                    3.6s

severity  long  short   budget spent  burn rate  error ratio >  budget gone in
page      1h    5m                2%      14.4x          1.44%            2d2h
page      6h    30m               5%         6x           0.6%              5d
ticket    1d    2h               10%         3x           0.3%             10d
ticket    3d    6h               10%         1x           0.1%             30d
```

One of the generated alerts ([full file](examples/generated/http-availability.rules.yaml)):

```yaml
  - alert: ErrorBudgetBurn
    expr: |-
      slo:sli_error:ratio_rate1h{service="checkout-api", slo="availability"} > (14.4 * 0.001)
      and
      slo:sli_error:ratio_rate5m{service="checkout-api", slo="availability"} > (14.4 * 0.001)
    labels:
      team: payments
      service: checkout-api
      slo: availability
      severity: page
      long_window: 1h
      short_window: 5m
    annotations:
      summary: checkout-api availability SLO is burning error budget at 14.4x
      description: Error ratio over 1h and 5m is above 14.4x the 0.1% budget of the 99.9%/30d SLO. 2% of the budget is spent per 1h at this rate; all of it in 2d2h.
      runbook_url: https://runbooks.example.com/checkout-api/availability
```

## How it works

**Error budget.** For objective `O` (percent) the budget is `B = 1 - O/100` of events. Over window `W` that is
`B * W` of full-outage time: 99.9% over 30d is `0.001 * 30 * 24 * 60 = 43.2` minutes.

**Burn rate.** The rate at which the budget is spent, relative to "exactly on budget". At burn rate 1 the error
ratio equals `B` and the budget lasts exactly `W`. At burn rate `r` it lasts `W / r`.

**Deriving the thresholds.** Each alert policy says "alert if fraction `f` of the whole budget is spent within
long window `L`". Spending `f` of the budget in `L` needs

```
burn_rate = f * W / L
threshold = burn_rate * B        (compared against the error ratio)
```

| severity | budget spent `f` | long `L` | short `L/12` | burn rate for W=30d | for W=28d |
|----------|------------------|----------|--------------|---------------------|-----------|
| page     | 2%               | 1h       | 5m           | 0.02 * 720h / 1h = **14.4** | 13.44 |
| page     | 5%               | 6h       | 30m          | 0.05 * 720h / 6h = **6**    | 5.6   |
| ticket   | 10%              | 1d       | 2h           | 0.10 * 30d / 1d = **3**     | 2.8   |
| ticket   | 10%              | 3d       | 6h           | 0.10 * 30d / 3d = **1**     | 0.933 |

Only `f`, `L` and the short window are fixed (in `sloburn/generator.py`, `POLICIES`); the burn rates are computed
from your SLO window, so a 28-day SLO gets correct thresholds without editing anything.

**Why two windows.** The long window makes the alert significant (enough budget really is being spent). The short
window, 1/12 of the long one, must also be over the threshold, so the alert stops firing within minutes of
recovery instead of staying on for the full hour or day. `tests/promtool/http-availability.test.yaml` demonstrates
this: ten minutes after a simulated outage ends, the 14.4x page has cleared while the 1h ratio is still far above
threshold, and the slower 6x/3x/1x alerts are still firing.

**Rules generated per SLO.**
- 7 recording rules `slo:sli_error:ratio_rate{5m,30m,1h,2h,6h,1d,3d}` labelled `service` and `slo`.
  With a `good` SLI the error ratio is `1 - good/total`.
- 4 `ErrorBudgetBurn` alerts, told apart by `severity`, `long_window` and `short_window` labels. Route `page`
  to your pager and `ticket` to a queue in Alertmanager. There is no `for:` clause; the short window already
  filters blips, as the Workbook recommends.
- Optional Grafana dashboard (`--dashboard`): SLI, error budget remaining over the SLO window, burn rate per window.

## Checks

- `pytest -q`: budget and burn-rate math, spec validation, rule structure, YAML round-trip, dashboard shape, CLI,
  and that the committed examples match a fresh generation.
- CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) also downloads the official Prometheus 3.5.0 release
  (checksum-verified) and runs `promtool check rules` on the generated files and `promtool test rules` on
  `tests/promtool/`, which feeds synthetic traffic to the rules and asserts which alerts fire.

## Limitations

- One SLO per spec file. Run the generator once per SLO and load each output file.
- The SLI expressions should aggregate to a single series (e.g. `sum(rate(...))`). Per-label SLOs (per route,
  per customer) need `sum by (...)` in both expressions and are not covered by the tests.
- Recording rules over 1d and 3d evaluate `rate()` over the raw counters for that whole range. That is simple and
  exact but costly for high-cardinality metrics; larger setups usually aggregate first.
- With no traffic the ratio is `0/0 = NaN`, so no alert fires. Pair this with a separate "no traffic" alert if
  silence is itself a failure for the service.
- The dashboard's "budget remaining" panel queries the raw SLI over the full SLO window. The dashboard JSON is
  generated and structure-tested but was not imported into a running Grafana as part of the tests.
