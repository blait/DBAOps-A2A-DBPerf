"""시나리오 생성기 라이브 모니터 — task 진행 카드 + CloudWatch Logs tail."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import streamlit as st

import ecs_client


_STATUS_BADGE = {
    "PROVISIONING": "🟨",
    "PENDING":      "🟨",
    "ACTIVATING":   "🟨",
    "RUNNING":      "🟩",
    "DEACTIVATING": "🟧",
    "STOPPING":     "🟧",
    "DEPROVISIONING": "🟧",
    "STOPPED":      "⬛",
}


def _status_chip(status: str | None) -> str:
    if not status:
        return "❔ unknown"
    return f"{_STATUS_BADGE.get(status, '•')} `{status}`"


def _scenario_for_family(family: str | None) -> dict | None:
    if not family:
        return None
    return next((s for s in ecs_client.SCENARIOS if s.get("task_def") == family), None)


def _scenario_title(family: str | None) -> str:
    """ECS task family → 시나리오 카드 title (없으면 family 그대로)."""
    sc = _scenario_for_family(family)
    if not sc:
        return family or "(unknown)"
    icon = sc.get("icon")
    title = sc.get("title") or family
    return f"{icon} {title}" if icon else title


def _refresh_log_window(group: str, stream: str, key: str, max_lines: int = 300) -> None:
    """session_state 의 log buffer 를 업데이트하고 화면에 그린다."""
    state_key = f"loglines:{key}"
    token_key = f"logtoken:{key}"
    lines: list[dict] = st.session_state.get(state_key, [])
    next_token = st.session_state.get(token_key)

    # 한 번 호출 — 새 chunk
    out = ecs_client.tail_log_events(group, stream, next_token=next_token, limit=200)
    new_events = out.get("events") or []
    if new_events:
        lines.extend(new_events)
        # 끝에서 max_lines 만 유지
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        st.session_state[state_key] = lines
    if out.get("next_token"):
        st.session_state[token_key] = out["next_token"]

    if not lines:
        st.caption("(아직 로그 없음)")
        return
    text = "\n".join(f"{(ev.get('ts') or '')[:19]}  {ev.get('message','')}" for ev in lines[-max_lines:])
    st.code(text, language="text", wrap_lines=False)


def _render_task_card(task_id: str) -> None:
    info = ecs_client.describe_task(task_id)
    if not info:
        st.warning(f"task `{task_id}` describe 결과 없음 (이미 사라졌거나 권한 문제).")
        if st.button("🗑 추적 해제", key=f"untrack-{task_id}"):
            tracked = st.session_state.get("tracked_tasks") or []
            st.session_state["tracked_tasks"] = [t for t in tracked if t != task_id]
            owners = st.session_state.get("task_owner") or {}
            owners.pop(task_id, None)
            st.session_state["task_owner"] = owners
            st.rerun()
        return

    status = info.get("last_status")
    container = info.get("container_status")
    family = info.get("family")
    stopped_reason = info.get("stopped_reason")

    title = _scenario_title(family)
    with st.container(border=True):
        cols = st.columns([2, 1, 1, 1])
        cols[0].markdown(f"### {title}")
        cols[0].caption(f"`{family}` · task `{task_id[:12]}…`")
        cols[1].metric("task", _status_chip(status), label_visibility="collapsed")
        cols[1].caption("task")
        cols[2].metric("container", _status_chip(container), label_visibility="collapsed")
        cols[2].caption("container")

        with cols[3]:
            if status not in {"STOPPED", None}:
                if st.button("⏹ 중지", key=f"stop-{task_id}", use_container_width=True):
                    try:
                        ecs_client.stop_task(task_id)
                        st.toast("stop_task 호출됨")
                    except Exception as e:  # noqa: BLE001
                        st.error(f"stop error: {e}")
            else:
                if st.button("🗑 추적 해제", key=f"untrack-{task_id}", use_container_width=True):
                    tracked = st.session_state.get("tracked_tasks") or []
                    st.session_state["tracked_tasks"] = [t for t in tracked if t != task_id]
                    owners = st.session_state.get("task_owner") or {}
                    owners.pop(task_id, None)
                    st.session_state["task_owner"] = owners
                    # 로그 버퍼도 정리
                    for k in list(st.session_state.keys()):
                        if k.endswith(f":{task_id}"):
                            del st.session_state[k]
                    st.rerun()

        # timing
        meta_cols = st.columns(4)
        meta_cols[0].caption(f"created  \n`{info.get('created_at') or '—'}`")
        meta_cols[1].caption(f"started  \n`{info.get('started_at') or '—'}`")
        meta_cols[2].caption(f"stopped  \n`{info.get('stopped_at') or '—'}`")
        ec = info.get("exit_code")
        meta_cols[3].caption(f"exit code  \n`{ec if ec is not None else '—'}`")

        if stopped_reason:
            st.error(f"stopped: {stopped_reason}")

        # 로그
        log_group = info.get("log_group")
        log_stream = info.get("log_stream")
        if log_group and log_stream:
            with st.expander(f"📜 CloudWatch Logs — `{log_group}` / `{log_stream[-60:]}`", expanded=True):
                _refresh_log_window(log_group, log_stream, key=task_id)
        else:
            st.caption("로그 stream 정보 없음 (task definition logConfiguration 확인).")


_CATEGORY_LABEL = {
    "data": ("⚡ 데이터 부하 시나리오", "PG / MySQL / Kafka 에 직접 부하를 주입합니다."),
    "log":  ("📒 로그 burst 시나리오",  "S3 에 의도된 패턴의 에러/슬로우 로그를 빠르게 적재합니다."),
}


# lens → supervisor 매핑 (3 supervisor 구조)
def _supervisor_for_lens(lens: str | None) -> str:
    if lens == "log":
        return "log"
    if lens == "os":
        return "os_metric"
    return "db_metric"


_SUPERVISOR_LABEL = {
    "os_metric": "🖥️ OS·인프라 메트릭 분석",
    "db_metric": "🗄️ DB 성능 메트릭 분석",
    "log":       "📜 로그 분석",
}


def _render_scenario_card(sc: dict, *, subnets: list[str], sgs: list[str]) -> None:
    """단일 시나리오 카드 — 설명 + 영향 + 신호 + 추천 prompt + 실행 버튼."""
    with st.container(border=True):
        # 헤더
        head = st.columns([6, 2])
        head[0].markdown(f"### {sc.get('icon','▶')} {sc.get('title') or sc['label']}")
        head[1].caption(
            f"⏱ `{sc.get('duration', '?')}s` · "
            f"task `{(sc.get('task_def') or '').rsplit('-',1)[-1] or '?'}`"
        )

        # 한 줄 요약
        summary = sc.get("summary")
        if summary:
            st.markdown(summary)

        # 영향 / 감지 신호
        cols = st.columns(2)
        impact = sc.get("impact") or []
        signals = sc.get("signals") or []
        with cols[0]:
            st.markdown("**🎯 영향 (실제 발생 사항)**")
            if impact:
                for it in impact:
                    st.markdown(f"- {it}")
            else:
                st.caption("—")
        with cols[1]:
            st.markdown("**📡 감지 신호 (메트릭/로그)**")
            if signals:
                for it in signals:
                    st.markdown(f"- {it}")
            else:
                st.caption("—")

        # 추천 분석 prompt
        prompt = sc.get("suggested_prompt")
        lens = sc.get("suggested_lens") or "multi"
        sup = _supervisor_for_lens(lens)
        sup_label = _SUPERVISOR_LABEL.get(sup, sup)
        if prompt:
            st.markdown(f"**💬 추천 분석 요청** · 추천 supervisor: **{sup_label}**")
            st.code(prompt, language="text")

        # 액션 버튼
        btn_cols = st.columns([1, 1, 4])
        with btn_cols[0]:
            run_clicked = st.button(
                "▶ 시나리오 실행",
                key=f"scn-run-{sc['key']}",
                type="primary",
                use_container_width=True,
                disabled=not subnets,
            )
        with btn_cols[1]:
            ask_clicked = st.button(
                "💬 채팅에 보내기",
                key=f"scn-ask-{sc['key']}",
                use_container_width=True,
                disabled=not prompt,
                help="추천 prompt를 분석 채팅 탭에 prefill 하고 즉시 실행 가능한 상태로 둡니다.",
            )

        if run_clicked:
            try:
                res = ecs_client.trigger_scenario(
                    sc["key"], subnets=subnets, security_groups=sgs or None
                )
                if res.get("ok"):
                    tasks = st.session_state.get("tracked_tasks") or []
                    if res["task_id"] not in tasks:
                        tasks.append(res["task_id"])
                        st.session_state["tracked_tasks"] = tasks
                    # task → scenario 소유 매핑 — 카드 안에서 라이브 모니터 그릴 때 사용
                    owners = st.session_state.get("task_owner") or {}
                    owners[res["task_id"]] = sc["key"]
                    st.session_state["task_owner"] = owners
                    st.toast(
                        f"started {res['family']} · task {res['task_id'][:10]}",
                        icon="▶",
                    )
                    st.rerun()
                else:
                    st.error(f"failed: {res.get('failures')}")
            except Exception as e:  # noqa: BLE001
                st.error(f"RunTask error: {e}")

        if ask_clicked and prompt:
            st.session_state["chat_prefill"] = {
                "free_text": prompt,
                "lens": lens,
                "supervisor": sup,
            }
            st.toast(f"{sup_label} 탭에 prefill 했습니다.", icon="💬")

        # 이 시나리오가 trigger 한 task 들의 라이브 모니터 — 카드 안에 인라인 표시
        owners = st.session_state.get("task_owner") or {}
        tracked = st.session_state.get("tracked_tasks") or []
        owned = [t for t in tracked if owners.get(t) == sc["key"]]
        if owned:
            st.markdown("---")
            st.markdown("**📺 이 시나리오의 실행 모니터**")
            for tid in owned:
                _render_task_card(tid)


def _render_trigger_panel() -> None:
    """시나리오 카드 그리드 — Generators 탭 상단."""
    subnets = ecs_client.default_subnets()
    sgs = ecs_client.default_security_groups()
    if not subnets:
        st.warning("ECS_SUBNETS 환경변수 비어있음. 시나리오 실행이 비활성화됩니다.")

    # 카테고리별 그룹핑
    by_cat: dict[str, list[dict]] = {}
    for sc in ecs_client.SCENARIOS:
        by_cat.setdefault(sc.get("category", "data"), []).append(sc)

    for cat in ("data", "log"):
        items = by_cat.get(cat) or []
        if not items:
            continue
        title, hint = _CATEGORY_LABEL.get(cat, (cat, ""))
        st.markdown(f"### {title}")
        if hint:
            st.caption(hint)
        # 1열 — 한 행에 하나씩 좌우로 길게
        for sc in items:
            _render_scenario_card(sc, subnets=subnets, sgs=sgs)


def render(autorefresh_sec: int = 5) -> None:
    """Generators 탭 메인."""
    tracked: list[str] = st.session_state.get("tracked_tasks") or []
    owners: dict[str, str] = st.session_state.get("task_owner") or {}

    # ── 트리거 패널 (탭 상단) ──
    _render_trigger_panel()

    st.divider()

    # ── 라이브 모니터 헤더 ──
    head_cols = st.columns([3, 1, 1])
    head_cols[0].markdown("#### 📺 외부 trigger / 미매핑 task 모니터")
    auto = head_cols[1].toggle("자동 새로고침", value=True, key="gen-auto")
    if head_cols[2].button("🔄 새로고침", use_container_width=True):
        st.rerun()
    st.caption(
        "시나리오 버튼으로 띄운 task 는 해당 시나리오 카드 안에서 모니터링됩니다. "
        "이 영역은 외부 trigger 또는 직접 추적한 task 만 보여집니다."
    )

    # owner 가 있는 task 는 카드 안에서 이미 그렸으니 여기선 제외
    unowned = [t for t in tracked if t not in owners]

    if not unowned:
        st.caption("추적 중인 미매핑 task 없음.")

    # 사용자가 직접 task_id 추적 (수동 외부 trigger 케이스)
    with st.expander("➕ 다른 task_id 직접 추적"):
        manual = st.text_input("task_id", key="manual-track-input", placeholder="ECS task UUID")
        if st.button("추적 시작", key="manual-track-btn") and manual.strip():
            tasks = list(tracked)
            if manual.strip() not in tasks:
                tasks.append(manual.strip())
            st.session_state["tracked_tasks"] = tasks
            st.session_state["manual-track-input"] = ""
            st.rerun()

    # unowned 카드들
    for tid in unowned:
        _render_task_card(tid)

    # 그 외 RUNNING task 도 보여주기 (정보용)
    st.divider()
    st.markdown("#### 그 외 RUNNING task")
    try:
        running = ecs_client.list_running_tasks()
    except Exception as e:  # noqa: BLE001
        st.error(f"describe_tasks error: {e}")
        running = []
    others = [r for r in running if r.get("task_id") not in set(tracked)]
    if others:
        for r in others:
            r["scenario"] = _scenario_title(r.get("family"))
        st.dataframe(others, use_container_width=True, hide_index=True)
        st.caption("👆 위 목록의 task_id 를 복사해 위 expander 로 추적할 수 있습니다.")
    else:
        st.caption("그 외 실행 중인 task 없음.")

    # 자동 새로고침 — 추적 중인 task 가 1개라도 있을 때만
    if auto and tracked:
        # 모두 STOPPED 면 폴링 중단
        any_active = any(
            (ecs_client.describe_task(tid) or {}).get("last_status") not in {"STOPPED", None}
            for tid in tracked
        )
        if any_active:
            time.sleep(autorefresh_sec)
            st.rerun()