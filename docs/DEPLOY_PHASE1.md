# PRISM-INSIGHT — Phase 1 배포 런북 (구형맥, KR 분석 관측 전용)

> **범위**: KR 주식 분석 + 텔레그램 알림만. **매매 없음**(KIS 계좌·US·계좌 대시보드 제외).
> ChatGPT OAuth 구독 모드(OpenAI API 키 불필요). cron: 07:00 데이터갱신 / 09:30 오전분석 / 15:40 오후분석 (KST).

배포 오버레이: `docker-compose.phase1.yml` (base `docker-compose.yml` 위에 얹음).

---

## 0. 구형맥 사전 준비
- Docker Desktop 설치 + 실행 중
- git 설치
- 인터넷 (텔레그램/KRX/ChatGPT 접속)

## 1. 레포 가져오기
```bash
git clone git@github.com:talentceffort/prism-insight.git
cd prism-insight
git checkout feat/honest-perf-metrics     # 배포 파일 + 이번 개선분이 이 브랜치에 있음
```

## 2. 비밀 설정 파일 복사 (git에 없음 — 기존 맥에서 가져오기)
아래 4개는 `.gitignore` 대상이라 clone에 안 딸려옵니다. **개발용 맥에서 구형맥으로 복사**하세요 (scp / AirDrop / USB):

| 원본 (개발맥) | 대상 (구형맥) |
|---|---|
| `prism-insight/.env` | `prism-insight/.env` |
| `prism-insight/mcp_agent.config.yaml` | 동일 |
| `prism-insight/mcp_agent.secrets.yaml` | 동일 |
| `~/.config/prism-insight/chatgpt_auth.json` | `~/.config/prism-insight/chatgpt_auth.json` |

> **ChatGPT 토큰 복사가 핵심 지름길**입니다. 이게 되면 구형맥에서 OAuth 재로그인이 필요 없습니다(§6은 실패했을 때만). 토큰은 refresh 방식이라 보통 기기 이동해도 동작합니다.

`~/.config/prism-insight/`가 구형맥에 없으면 먼저:
```bash
mkdir -p ~/.config/prism-insight
```

## 3. `.env` 확인 (구형맥에서 열어서)
- `PRISM_OPENAI_AUTH_MODE=chatgpt_oauth` — **주석 해제되어 있어야 함**
- `KRX_ID` = KRX **회원 아이디** (❗이메일 아님 — 이메일 넣으면 세션 무효화됨)
- `KRX_PW`, `KRX_LOGIN_METHOD=krx` (kakao는 2FA라 헤드리스 불가)
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_AI_BOT_TOKEN` = okx-trading 봇 토큰 재사용
- `TELEGRAM_CHANNEL_ID` = 알림 받을 채널

## 4. 파일 마운트 대상 미리 생성 (Docker 파일마운트 함정 방지)
DB는 **파일 단위 마운트**라, 파일이 없으면 Docker가 *디렉터리*를 만들어버려 깨집니다. 먼저 빈 파일/디렉터리를 만들어 두세요:
```bash
touch stock_tracking_db.sqlite
mkdir -p logs reports pdf_reports html_reports charts telegram_messages
```

## 5. 빌드 + 기동 (Phase-1 오버레이)
```bash
# 병합 결과 먼저 확인 (crontab.phase1이 이겼는지, OAuth 볼륨 붙었는지)
docker compose -f docker-compose.yml -f docker-compose.phase1.yml config

# 기동
docker compose -f docker-compose.yml -f docker-compose.phase1.yml up -d --build
```

## 6. (오늘 밤) 스모크 테스트 — 강제 1회 실행해서 텔레그램 확인
내일 09:30까지 기다리지 말고 오늘 밤 강제 실행해 파이프라인을 검증하세요:
```bash
docker compose -f docker-compose.yml -f docker-compose.phase1.yml \
  exec prism-insight python3 stock_analysis_orchestrator.py --mode morning
```
- 로그 보면서 진행 확인, **텔레그램 도착하면 성공**.
- `ChatGPT` 인증 401/무반응이면 → 토큰 이동 실패 → 아래 §OAuth 폴백.

## 7. cron 확인 + 방치
```bash
docker compose -f docker-compose.yml -f docker-compose.phase1.yml exec prism-insight crontab -l
# 07:00 / 09:30 / 15:40 (KST) 잡이 보여야 함. TZ=Asia/Seoul는 compose에 설정됨.
```
`restart: unless-stopped`라 맥 재부팅에도 살아납니다. 내일 09:30 KST에 첫 알림.

---

## OAuth 폴백 (§2 토큰 복사가 안 먹혔을 때만)
컨테이너 안에서 재로그인 (브라우저가 콜백 포트에 닿아야 함):
```bash
# 콜백 포트(1455)를 호스트에 노출하고, 컨테이너 바인드를 0.0.0.0으로:
docker compose -f docker-compose.yml -f docker-compose.phase1.yml \
  run --rm -p 1455:1455 -e OAUTH_CALLBACK_HOST=0.0.0.0 \
  prism-insight python -m cores.chatgpt_proxy.oauth_login
```
- 출력된 인증 URL을 구형맥 브라우저에서 열기 → 승인 → `localhost:1455`로 콜백 → 토큰이 `~/.config/prism-insight/chatgpt_auth.json`에 저장(볼륨 마운트로 호스트에 영속).
- 저장 확인: `ls -la ~/.config/prism-insight/`

## 트러블슈팅
| 증상 | 원인/해결 |
|---|---|
| KRX "세션 무효화" 반복 | `KRX_ID`가 이메일 → **회원 아이디**로 교체 |
| DB 마운트가 디렉터리로 생성됨 | 기동 전 `touch stock_tracking_db.sqlite` |
| ChatGPT 401 / 무반응 | 토큰 만료·미이동 → OAuth 폴백 재로그인 |
| 텔레그램 안 옴 | `.env`의 토큰/채널 ID, `docker ... logs` 확인 |
| 이미지 빌드 느림/실패 | 구형 Intel 맥이면 빌드 오래 걸림(정상). Docker Desktop 메모리 넉넉히 |

> 매매로 전환(Phase-2)할 때는 이 오버레이를 빼고 `docker-compose.yml` 단독 + `kis_devlp.yaml` 설정. **그 전까지는 실주문 안 나갑니다.**
