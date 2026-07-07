# sim 페이퍼 트레이딩 설계

> 상태: **설계 확정.** 결정이 바뀌거나 새로 생기면 아래 **결정**에 한 줄 추가.

## 목표
LLM 자동 스크리닝이 실제로 오르는 종목을 고르는지를, 브로커 없이 **로컬 페이퍼**
(실제 KRX 가격 + 가상 체결 + 현실 비용)로 **전방 검증**한다. 판단은 정직한 순자산 기준 수익률/MDD로.

## 결정 (왜 이렇게 — 방향키)
- **손잡이 2개, 각 뜻 하나 (특수케이스 없음):**
  - `default_mode` = **어디서** (`real` 실계좌 / `demo` 모의서버 / **`sim`** 로컬) — sim 값만 추가
  - `auto_trading` = **매매 실행할지** (모든 모드 동일한 뜻)
  - 페이퍼 하려면 → `auto_trading=true` + `default_mode=sim` / `auto_trading=false` = 분석만
- 실행은 **매수/매도 디스패치 1곳**만 분기. sim이면 **KIS 미접촉**(저수준 안 건드림).
- 가격은 **KRX**(task B 폴백) → sim은 KIS 계좌 불필요.
- 비용 = **틱(호가단위) 슬리피지 + 수수료 0.015%/편도 + 매도세 0.2%** → `sim_broker.py` (완성, 테스트 8/8).
- 자본 **1천만 / 최대 10슬롯 균등 = 종목당 100만**.
- **가상 sim 계좌 1개**(`account_key="sim:..."`)로 기존 계좌-DB 로직 재사용.
- **equity 계산 = 기존 테이블에서 직접** (별도 현금·수량 테이블 안 만듦):
  `equity = 1천만 + 청산손익(trading_history sim 합) + 미청산 평가손익(stock_holdings sim: 100만 × (현재가/매수가 − 1))`.
  종목당 100만 균등 + 최대 10슬롯(=1천만)이라 이 근사가 자기일관적. → `equity_tracker` 스냅샷 → 정직한 수익률/MDD.
- 스크리닝 v1 = **자동(LLM)**. 하이브리드(사람 승인)는 후속.

### 모드 조합
| `auto_trading` | `default_mode` | 결과 |
|---|---|---|
| `false` | (무엇이든) | 분석만, 매매 0 |
| `true` | `sim` | 로컬 페이퍼 |
| `true` | `demo` | KIS 모의 |
| `true` | `real` | 실계좌 |

## 안 하는 것
정밀 체결 재현(고정 틱=근사) · 눌림목 진입(별도 KRX 백테스트) · 실주문(sim은 KIS 미접촉).

## 할 일 (단계별 — 1차 목표 = 신호 알림, 페이퍼 포지션은 그 다음)
- [x] `sim_broker.py` (비용·체결, 테스트 8/8)
- [x] **전역 sim 봉인** — 공유 `kis_auth.get_configured_accounts`가 sim이면 계좌 해석을 거부한다(모든 `resolve_account`가 이 함수를 경유). → **KR·US·weekly·대시보드·브로커 생성자 어디서도 실계좌 미접촉.** 보조로 `TradingMode` VO + `_get_trading_accounts` sim 합성계좌 + `db_schema` 마이그레이션 거부(친절한 메시지). (codex R1~5: base경로·마이그레이션 prod누수·직접 생성자·전역 flag 순으로 다 봉인)
- [x] **Phase-1 (1차 목표): 매수 신호 알림** — sim이면 매수 디스패치(base+enhanced)가 **슬롯 게이트 통과 후** `_emit_buy_signal`로 텔레그램 신호 + watchlist 기록(주문·홀딩 X). `_snapshot_equity_for_kill`도 sim에서 KIS 스킵.
- [ ] **Phase-1.5: 페이퍼 포지션 + equity** — sim 매수 시 `stock_holdings`/`trading_history`에 `sim_broker` 체결 기록 → 매도·equity·MDD(상단 equity 설계). *현재는 알림만이라 미실현 손익 없음.*
- [ ] equity 계산 + `equity_tracker` 스냅샷 (Phase-1.5에 포함)
- [ ] sim 1사이클 스모크 (Phase-1: 신호 알림 / Phase-1.5: 원장·equity)
