"""analyzers 단위 테스트."""

from __future__ import annotations

from dbaops_agent.analyzers.anomaly import changepoints, detect, ewma, zscore
from dbaops_agent.analyzers.correlate import bucketize, cross_source
from dbaops_agent.analyzers.log_classify import classify, top_n


def test_zscore_uniform():
    assert all(abs(z) < 1e-6 for z in zscore([1.0] * 10))


def test_ewma_initial_equals_first():
    out = ewma([10.0, 20.0, 30.0], alpha=0.5)
    assert out[0] == 10.0
    assert out[1] == 15.0  # 0.5*20 + 0.5*10
    assert out[2] == 22.5


def test_changepoint_detects_shift():
    series = [1.0] * 20 + [50.0] * 20
    cps = changepoints(series, window=5, ratio=2.0)
    assert any(15 <= i <= 25 for i in cps)


def test_detect_finds_spike():
    series = [(f"t{i}", 1.0) for i in range(40)]
    series[20] = ("t20", 100.0)
    anomalies = detect(series, z_threshold=2.5)
    ts_set = {a.ts for a in anomalies}
    assert "t20" in ts_set


def test_log_classify_groups():
    lines = [
        "ERROR connection refused host=10.0.0.1",
        "ERROR connection refused host=10.0.0.2",
        "ERROR connection refused host=10.0.0.3",
        "INFO startup complete",
    ]
    templates = classify(lines)
    items = top_n(templates, n=5)
    assert items[0].count >= 3


def test_bucketize_cross_source():
    by_source = {
        "os":  [{"ts": "2026-05-15T10:00:05+00:00"}],
        "db":  [{"ts": "2026-05-15T10:00:30+00:00"}],
        "log": [{"ts": "2026-05-15T11:00:00+00:00"}],
    }
    corr = bucketize(by_source, window_sec=60)
    cross = cross_source(corr, min_sources=2)
    assert len(cross) == 1
    assert set(cross[0].sources.keys()) == {"os", "db"}
