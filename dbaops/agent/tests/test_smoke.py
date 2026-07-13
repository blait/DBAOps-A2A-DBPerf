"""기본 smoke test — graph 컴파일 / 라우트 / 리포트 형태."""

from __future__ import annotations

from dbaops_agent.graph import compile_graph


def _invoke(monkeypatch, lens: str = "os"):
    monkeypatch.setenv("DBAOPS_OFFLINE", "1")
    monkeypatch.setenv("GATEWAY_ENDPOINT", "")
    graph = compile_graph()
    return graph.invoke(
        {
            "request": {"free_text": "smoke", "lens": lens},
            "raw_signals": {},
            "messages": [],
            "tool_budget": 16,
        }
    )


def test_graph_runs_os(monkeypatch):
    out = _invoke(monkeypatch, "os")
    assert "report" in out
    md = out["report"]["markdown"]
    assert md.startswith("# DBAOps Analysis Report")


def test_graph_runs_multi(monkeypatch):
    out = _invoke(monkeypatch, "multi")
    assert out["report"]["request"]["lens"] == "multi"
    # multi 라우트에서는 hypothesis 가 빈 리스트라도 키 자체는 존재해야 한다
    assert "hypotheses" in out["report"]


def test_router_keyword():
    from dbaops_agent.nodes.router import run as router_run

    out = router_run({"request": {"free_text": "CPU spike on host"}})
    assert out["route"] in {"os", "multi"}  # offline 폴백은 'os'

    out2 = router_run({"request": {"lens": "db"}})
    assert out2["route"] == "db"
