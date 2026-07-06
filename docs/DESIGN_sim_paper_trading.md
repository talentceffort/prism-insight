# 설계: 로컬 페이퍼 트레이딩 (`default_mode: sim`)

> 상태: **설계 확정 대기** (구현 전). 구현은 이 문서를 스펙으로 삼는다.
> 관련: [`sim_broker.py`](../sim_broker.py)(완성), [`tracking/equity_tracker.py`](../tracking/equity_tracker.py)(재사용).

## 1. 목적 / 배경
LLM(자동) 스크리닝이 **실제로 오르는 종목을 고르는지 전방 검증**한다. 브로커·모의계좌 없이.
- **왜 페이퍼(전방)인가**: LLM 판단은 학습 컷오프 룩어헤드 때문에 백테스트가 불가 → 앞으로 지켜보는 게 유일한 정직한 검증.
- **왜 로컬 sim인가**: KIS 모의투자(demo) 계좌 생성 불가. 그래서 실제 가격(KRX) + 가상 체결로 로컬에서 시뮬레이션.
- **정직한 수익률** 측정이 목표(부풀린 SUM% 아님) → `equity_tracker`의 순자산 곡선 재사용.

## 2. 트레이딩 모드 — 단일 축, 새 플래그 없음
개념 하나(`default_mode`), 값 3개. `SIM_TRADING` 같은 별도 플래그는 **만들지 않는다**(개념 중복·충돌 방지).

| `default_mode` | 실행 | 브로커 | 돈 |
|---|---|---|---|
| `real` | KIS 실계좌 주문 | KIS 실전 | 실제 |
| `demo` | KIS 모의투자 서버 주문 | KIS vps | 가상(브로커측) |
| **`sim`** (신규) | **로컬 가상체결** | **없음** | 가상(로컬 명목) |

`auto_trading`(on/off) 게이트는 기존 그대로 3모드 공통 적용.

## 3. 실행 설계 — 디스패치 "한 곳"에서만 분기
sim 조건을 KIS 저수준에 흩뿌리지 않는다. 매매 실행 지점에서만 갈린다.

```
매수: stock_tracking(_enhanced)_agent.process_reports 주문 스텝
매도: stock_tracking_agent.update_holdings 주문 스텝
    if mode == "sim":  sim_broker로 가상 체결 → 명목원장 반영 → DB 기록   (KIS 미접촉)
    else:              기존 KIS 경로 (AsyncTradingContext.async_buy/sell_stock)
```
- 저수준의 `if self.mode == "real"` 분기들은 **sim에서 도달하지 않음**(KIS를 안 부르므로) → sim 조건 전파 0.
- **가격**: sim도 현재가 필요 → 이미 KRX에서 받음(task B의 KRX/DB 폴백). KIS 계좌 불필요.
- **order-before-record 유지**: sim도 "체결(가상) 성공 → 기록" 순서로, 실 경로와 일관.

## 4. 비용 모델 — `sim_broker.py` (완성, 테스트 8/8)
- **슬리피지 = 틱(호가단위) 기반**: 매수 = 기준가 +`SIM_SLIPPAGE_TICKS`틱, 매도 = −틱. (%가 아니라 틱 → 가격대별 현실적)
- **위탁수수료** `SIM_COMMISSION_RATE` 기본 0.015%/편도.
- **증권거래세** `SIM_TAX_RATE` 기본 0.15%(매도만) — **⚠️ 2026 기준 정확한지 확인 필요**.
- 검증: 제자리 왕복 **−0.38%**, +10% 상승 → net **+9.59%**.
- env: `SIM_CAPITAL`(1천만), `SIM_SLIPPAGE_TICKS`(1), `SIM_COMMISSION_RATE`, `SIM_TAX_RATE`.

## 5. 명목 회계 — 구현 대상
- 시작자본 **1천만원**, 사이징 **1천만 ÷ 10슬롯 = 종목당 ~100만원 균등**(MAX_SLOTS=10).
- **현금 + 포지션(수량)** 원장 필요 (stock_holdings엔 수량 컬럼 없음 → sim 전용 회계 얹음).
  - 매수: `qty = floor(포지션예산 / buy_fill)`; `cash -= buy_cost(buy_fill, qty)`.
  - 매도: `cash += sell_proceeds(sell_fill, qty)`.
- **일별 equity = cash + Σ(포지션 수량 × 현재가)** → `equity_tracker.record_equity_snapshot` → 정직한 누적수익률/MDD.
- **합성 sim 계좌**: `_get_trading_accounts`가 sim 모드에선 가상 계좌 1개(`account_key="sim:sim:01"` 등)를 반환 → 기존 계좌-스코프 DB 로직(`stock_holdings WHERE account_key=?`, `_account_scope`)을 그대로 재사용.

## 6. 스크리닝
- **v1: 자동**(LLM 픽만). 하이브리드(사람 승인 게이트)는 **후속**.

## 7. 구현 상태
- ✅ `sim_broker.py` — 틱 슬리피지 + 수수료/세금 + net 수익률 (테스트 8/8)
- ⬜ 실행 디스패치 통합 (매수/매도 sim 분기)
- ⬜ 명목 회계(현금/수량) + equity 스냅샷
- ⬜ 합성 sim 계좌 배관

## 8. 열린 결정 (확정 필요)
1. **증권거래세 0.15%** — 2026 현재 정확한 값?
2. **사이징** — 10슬롯 균등(종목당 100만) OK? (아니면 다른 비중)
3. **sim에서 `auto_trading`** — 실모드와 동일하게 게이트 적용(=off면 페이퍼도 안 함)? (일관성상 권장)

## 9. Non-goals / 한계 (정직하게)
- **고정 틱 슬리피지 = 근사.** 정밀 체결 재현 아님 → 목표는 **스크리닝 검증**(승자 고르나?), 정밀 집행은 아님.
- **눌림목 진입은 별개 트랙** — 결정론이라 KRX 백테스트로 검증(이 문서 범위 밖).
- **실주문 절대 없음** — `sim`은 KIS를 어떤 경로로도 호출하지 않음.
- 외부 입출금 미반영, 첫 스냅샷 기준 forward-only (equity_tracker 카비앗 동일).
