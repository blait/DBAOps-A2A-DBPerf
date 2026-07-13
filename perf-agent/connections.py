"""
connections.py - 연동 서비스 관리.

이 에이전트 스택이 연결되는 외부 서비스들의 상태 확인 / 설정 / 테스트를 한 곳에서 처리:
  - RDS SQL Server (Secrets Manager 자격증명)
  - Slack (Bot Token — 기존 DBAOps slack-bot과 동일한 토큰 방식)
  - DBAOps Agent (vanilla systemd, 127.0.0.1:8080 HTTP)
  - A2A peer 서버들 (performance :9100 / dbaops native A2A :9102)

Slack은 webhook이 아니라 **Bot Token(xoxb-…) + chat.postMessage** 방식.
토큰은 환경변수 SLACK_BOT_TOKEN(권장, .env → compose) 또는
SSM SecureString(SLACK_BOT_TOKEN_PARAM)에서 읽는다.

CLI:
  python3 connections.py status        # 전체 연동 상태
  python3 connections.py test-slack    # Slack 테스트 메시지 (SLACK_CHANNEL로)
  python3 connections.py test-db       # DB 연결 테스트
  python3 connections.py test-a2a      # A2A 서버 카드 확인
"""
import json
import os
import sys
import urllib.request

import boto3

AWS_REGION = os.environ.get('AWS_REGION', 'ap-northeast-2')
DB_SECRET_ID = os.environ.get('DB_SECRET_ID', 'dbops-sqlserver-secret')

# Slack — bot token 방식 (DBAOps slack_bot과 동일한 토큰을 재사용 가능)
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')
SLACK_BOT_TOKEN_PARAM = os.environ.get('SLACK_BOT_TOKEN_PARAM', '/dbops/slack/bot_token')
SLACK_CHANNEL = os.environ.get('SLACK_CHANNEL', '')  # 알림 기본 채널 (예: #dbops-alerts 또는 C0123…)

# DBAOps agent (vanilla systemd, HTTP)
DBAOPS_AGENT_URL = os.environ.get('DBAOPS_AGENT_URL', 'http://127.0.0.1:8080/invocations')

PERF_A2A_URL = os.environ.get('PERF_A2A_URL', 'http://127.0.0.1:9100')
OPS_A2A_URL = os.environ.get('OPS_A2A_URL', 'http://127.0.0.1:9102')


# ───────────────────────── Slack (Bot Token) ─────────────────────────

def get_slack_token() -> str | None:
    """SLACK_BOT_TOKEN env 우선, 없으면 SSM SecureString에서 조회."""
    if SLACK_BOT_TOKEN:
        return SLACK_BOT_TOKEN
    try:
        ssm = boto3.client('ssm', region_name=AWS_REGION)
        return ssm.get_parameter(Name=SLACK_BOT_TOKEN_PARAM, WithDecryption=True)['Parameter']['Value']
    except Exception:
        return None


def _slack_api(method: str, payload: dict) -> dict:
    token = get_slack_token()
    if not token:
        return {'ok': False, 'error': 'SLACK_BOT_TOKEN 미설정 (.env 또는 SSM)'}
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json; charset=utf-8',
                 'Authorization': f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def send_slack(text: str, severity: str = "INFO", channel: str = "") -> dict:
    """chat.postMessage로 메시지 발송. channel 미지정 시 SLACK_CHANNEL 사용."""
    target = channel or SLACK_CHANNEL
    if not target:
        return {'status': 'error', 'error': 'SLACK_CHANNEL 미설정 — 발송할 채널을 지정하세요'}
    emoji = {'INFO': ':information_source:', 'WARNING': ':warning:', 'CRITICAL': ':rotating_light:'}.get(severity, ':bell:')
    resp = _slack_api('chat.postMessage', {
        'channel': target,
        'text': f"{emoji} *[{severity}] SQL Server DBPerf*\n{text}",
    })
    if resp.get('ok'):
        return {'status': 'success', 'channel': target, 'ts': resp.get('ts')}
    return {'status': 'error', 'error': resp.get('error', 'unknown')}


# ───────────────────────── 상태 확인 ─────────────────────────

def check_db() -> dict:
    """Secrets Manager 자격증명으로 SQL Server 접속 확인."""
    try:
        import pymssql
        sm = boto3.client('secretsmanager', region_name=AWS_REGION)
        creds = json.loads(sm.get_secret_value(SecretId=DB_SECRET_ID)['SecretString'])
        if 'CHANGE_ME' in creds.get('password', ''):
            return {'ok': False, 'detail': f"시크릿 {DB_SECRET_ID}의 password가 placeholder 상태"}
        conn = pymssql.connect(server=creds['host'], user=creds['username'],
                               password=creds['password'], port=creds.get('port', 1433),
                               database=os.environ.get('DB_NAME', 'master'), timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT @@VERSION")
        ver = cur.fetchone()[0].split('\n')[0]
        conn.close()
        return {'ok': True, 'detail': ver, 'host': creds['host']}
    except Exception as e:
        return {'ok': False, 'detail': str(e)[:300]}


def check_slack() -> dict:
    """auth.test로 토큰 유효성 확인."""
    resp = _slack_api('auth.test', {})
    if resp.get('ok'):
        ch = f", 기본 채널: {SLACK_CHANNEL}" if SLACK_CHANNEL else ", ⚠️ SLACK_CHANNEL 미설정"
        return {'ok': True, 'detail': f"봇 @{resp.get('user')} ({resp.get('team')}){ch}"}
    return {'ok': False, 'detail': resp.get('error', 'SLACK_BOT_TOKEN 미설정')}


def check_dbaops_agent() -> dict:
    """DBAOps agent 헬스 확인 (/ping)."""
    base = DBAOPS_AGENT_URL.rsplit('/', 1)[0]  # …:8080
    for path in ('/ping', '/healthz'):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                if r.status == 200:
                    return {'ok': True, 'detail': f"agent 응답 정상 ({base})", 'url': DBAOPS_AGENT_URL}
        except Exception:
            continue
    return {'ok': False, 'detail': f"{base} 응답 없음 (agent 컨테이너 미기동?)"}


def check_a2a(url: str) -> dict:
    """A2A 서버의 agent card 조회."""
    for path in ('/.well-known/agent-card.json', '/.well-known/agent.json'):
        try:
            with urllib.request.urlopen(url.rstrip('/') + path, timeout=5) as r:
                card = json.loads(r.read())
                return {'ok': True, 'detail': f"{card.get('name')} — {card.get('description', '')[:80]}",
                        'skills': [s.get('name') for s in card.get('skills', [])]}
        except Exception:
            continue
    return {'ok': False, 'detail': f"{url} 응답 없음 (서버 미기동?)"}


def check_all() -> dict:
    """모든 연동 상태를 한 번에 확인 (Streamlit 연동 관리 탭에서 사용)."""
    return {
        'db_sqlserver': check_db(),
        'slack': check_slack(),
        'dbaops_agent': check_dbaops_agent(),
        'a2a_performance_agent': check_a2a(PERF_A2A_URL),
        'a2a_dbaops': check_a2a(OPS_A2A_URL),
    }


# ───────────────────────── CLI ─────────────────────────

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd == 'status':
        for name, st in check_all().items():
            mark = '✅' if st.get('ok') else '❌'
            print(f"{mark} {name:26s} {st.get('detail','')}")
    elif cmd == 'test-slack':
        print(json.dumps(send_slack('연동 테스트 메시지입니다.', 'INFO'), ensure_ascii=False))
    elif cmd == 'test-db':
        print(json.dumps(check_db(), ensure_ascii=False, indent=2))
    elif cmd == 'test-a2a':
        print('perf:', json.dumps(check_a2a(PERF_A2A_URL), ensure_ascii=False))
        print('ops :', json.dumps(check_a2a(OPS_A2A_URL), ensure_ascii=False))
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
