# 📈 Stock Trading Bot (Automated, Paper-first)

ระบบเทรดหุ้นอัตโนมัติแบบครบวงจรสำหรับ **การศึกษาและการวิจัย**: ดึงราคา → บันทึกลง Database (พร้อม % ขึ้น/ลง)
→ สร้างสัญญาณจากกลยุทธ์ → ตรวจความเสี่ยง → ส่งคำสั่ง (Paper/Real) → บันทึกทุกอย่าง → แจ้งเตือน → Dashboard

> ## ⚠️ DISCLAIMER / ข้อจำกัดความรับผิดชอบ
> ซอฟต์แวร์นี้จัดทำเพื่อ **การศึกษาและการวิจัยเท่านั้น ไม่ใช่คำแนะนำการลงทุน** (not investment advice)
> การเทรดหลักทรัพย์ โดยเฉพาะด้วยเงินจริงและระบบอัตโนมัติ **มีความเสี่ยงสูงและอาจขาดทุนเกินกว่าที่คาดไว้**
> ผลการ backtest ในอดีตไม่ได้รับประกันผลในอนาคต ผู้ใช้รับผิดชอบต่อการตัดสินใจและผลที่เกิดขึ้นทั้งหมดด้วยตนเอง
> ซอฟต์แวร์ให้มา "ตามสภาพ" (AS IS) โดยไม่มีการรับประกันใด ๆ — ค่าเริ่มต้นคือ **Paper Trading** เสมอ

---

## สารบัญ
1. [ภาพรวมสถาปัตยกรรม](#1-ภาพรวมสถาปัตยกรรม)
2. [โครงสร้างโปรเจกต์](#2-โครงสร้างโปรเจกต์)
3. [การติดตั้ง](#3-การติดตั้ง)
4. [Quick start แบบ offline (ไม่ต้องใช้ API key)](#4-quick-start-แบบ-offline)
5. [วิธีรัน Paper Trading แบบ step-by-step](#5-วิธีรัน-paper-trading-แบบ-step-by-step)
6. [Backtesting](#6-backtesting)
7. [กลยุทธ์ (Strategies)](#7-กลยุทธ์)
8. [Risk Management](#8-risk-management)
9. [Execution: Paper vs Real](#9-execution-paper-vs-real)
10. [Monitoring, Alerts, Logging, Dashboard](#10-monitoring-alerts-logging-dashboard)
11. [Configuration](#11-configuration)
12. [Database](#12-database)
13. [หุ้นไทย vs หุ้นต่างประเทศ](#13-หุ้นไทย-vs-หุ้นต่างประเทศ)
14. [Tests](#14-tests)
15. [ข้อจำกัดและข้อควรระวัง (สำคัญ)](#15-ข้อจำกัดและข้อควรระวัง)

---

## 1. ภาพรวมสถาปัตยกรรม

```
             ┌──────────────┐   retry/backoff + fallback
 Scheduler ─▶│ DataProvider │  (Yahoo / Alpha Vantage / Synthetic)
 (APScheduler)└──────┬───────┘
      │              ▼
      │       PriceCollector ──▶ price_history (OHLCV + change, % change, UP/DOWN)
      ▼              │
 TradingEngine ◀─────┘
   │ 1. pending limit orders (fill / expire)
   │ 2. stop-loss / take-profit check
   │ 3. Strategy.generate_signals() ──▶ signals
   │ 4. RiskManager (size, max positions, daily loss, drawdown)
   │ 5. Broker (Paper: slippage+commission | Alpaca live) ──▶ orders, trades
   │ 6. revalue portfolio ──▶ portfolios, portfolio_snapshots
   ▼
 AlertManager ──▶ Console / Telegram / Discord / LINE  ──▶ alert_logs
 Dashboard (FastAPI + HTML/Chart.js) reads the same DB
```

**การตัดสินใจหลักด้านเทคโนโลยี**

| ส่วน | เลือกใช้ | เหตุผล |
|---|---|---|
| ภาษา | Python 3.11+ | ระบบนิเวศด้าน data/finance |
| Database | SQLite (เริ่มต้น) / PostgreSQL + SQLAlchemy 2.0 + Alembic | schema เดียวกันทั้งสองแบบ |
| Data | `yfinance` (ฟรี, รองรับ `.BK`), Alpha Vantage (fallback) | หุ้นไทย + ต่างประเทศ + crypto (`BTC-USD`) |
| Backtest | engine เขียนเอง (event-driven) | ใช้ **RiskManager + CostModel ตัวเดียวกับระบบจริง** ไม่มี look-ahead; ไม่ผูกกับ backtrader/vectorbt |
| Scheduler | APScheduler | ไม่ต้องมี broker/Redis แบบ Celery |
| Dashboard | FastAPI + HTML (Chart.js) | ไม่ต้อง build frontend |
| Notification | Telegram / Discord / LINE Messaging API | *LINE Notify ปิดบริการแล้วเมื่อ 31 มี.ค. 2025* |
| Logging | loguru | แยกไฟล์ `stock_bot.log`, `errors.log`, `trades.log` พร้อม rotation |

## 2. โครงสร้างโปรเจกต์

```
stock_bot/
├── config/
│   ├── settings.py                # Pydantic settings + ${ENV} expansion
│   ├── config.example.yaml        # US stocks (Yahoo) - คัดลอกเป็น config.yaml
│   ├── config.thai.example.yaml   # หุ้นไทย SET (.BK, lot 100, VAT ค่าคอม)
│   └── config.demo.yaml           # offline demo (synthetic data)
├── data/
│   ├── collector.py               # fetch -> upsert -> % change
│   └── providers/                 # yahoo.py, alpha_vantage.py, synthetic.py, base.py, fallback
├── database/
│   ├── models.py                  # schema (10 tables)
│   ├── session.py                 # engine/session, SQLite pragmas
│   ├── repository.py              # data-access helpers
│   └── migrations/                # Alembic (versions/0001_initial_schema.py)
├── strategies/                    # ma_crossover, rsi, bollinger, breakout, momentum
├── backtesting/                   # engine.py (Backtester), runner.py (compare, save)
├── risk/manager.py                # position sizing + limits + SL/TP
├── execution/
│   ├── engine.py                  # TradingEngine (the main loop)
│   ├── broker.py                  # BaseBroker + PaperBroker
│   ├── live_brokers.py            # AlpacaBroker (US), SettradeBroker (skeleton)
│   ├── costs.py                   # commission / VAT / slippage
│   └── safety.py                  # live-trading confirmation guard
├── analysis/                      # indicators, metrics, price_change, daily_summary
├── monitoring/                    # notifier.py (channels), alerts.py (rules, cooldown)
├── scheduler/jobs.py              # intraday + end-of-day jobs
├── dashboard/                     # app.py (FastAPI) + static/index.html
├── utils/                         # logger, retry, market_hours
├── examples/                      # backtest_example.py, paper_trading_replay.py
├── tests/                         # pytest (69 tests)
├── docs/DATABASE.md               # schema details, ER diagram, SQL examples
├── main.py                        # CLI
├── alembic.ini, requirements.txt, pyproject.toml, docker-compose.yml, .env.example
```

## 3. การติดตั้ง

```bash
cd stock_bot
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp config/config.example.yaml config/config.yaml   # แก้ watchlist / risk / strategy
cp .env.example .env                               # ใส่ API keys / tokens (ไม่ต้องมีก็รัน paper ได้)
```

ใช้ PostgreSQL (แนะนำเมื่อรันจริงจังระยะยาว):

```bash
docker compose up -d
pip install "psycopg[binary]"
echo 'DATABASE_URL=postgresql+psycopg://stockbot:stockbot@localhost:5432/stock_bot' >> .env
alembic upgrade head
```

## 4. Quick start แบบ offline

ใช้ข้อมูลจำลอง (synthetic — **ไม่ใช่ราคาจริง**) เพื่อลองระบบทั้งหมดโดยไม่ต้องใช้อินเทอร์เน็ต:

```bash
python main.py -c config/config.demo.yaml init-db
python main.py -c config/config.demo.yaml backfill          # 2 ปี
python main.py -c config/config.demo.yaml compare --symbol AAPL
python main.py -c config/config.demo.yaml run-once
python examples/paper_trading_replay.py --days 120          # เดินระบบจริงย้อนหลังทีละวัน
python examples/backtest_example.py
```

## 5. วิธีรัน Paper Trading แบบ step-by-step

**ขั้นที่ 1 — ตั้งค่า** `config/config.yaml`
```yaml
data:      {provider: yahoo, interval: 1d, history_period: 2y}
watchlist:
  - {symbol: AAPL, market: US}
  - {symbol: MSFT, market: US}
strategy:  {name: ma_crossover}
execution: {mode: paper, broker: paper, portfolio_name: us-paper, initial_capital: 100000}
```

**ขั้นที่ 2 — สร้างฐานข้อมูล**
```bash
python main.py init-db            # หรือ: alembic upgrade head
```

**ขั้นที่ 3 — ดึงข้อมูลย้อนหลัง** (จำเป็นเพื่อให้ indicator มีข้อมูลพอ)
```bash
python main.py backfill --period 2y
python main.py fetch              # แสดงราคาล่าสุด + % ขึ้น/ลง
```

**ขั้นที่ 4 — Backtest ก่อนเสมอ**
```bash
python main.py compare --symbol AAPL
python main.py backtest --symbol AAPL --strategy ma_crossover --trades --save
```

**ขั้นที่ 5 — ทดสอบแจ้งเตือน** (optional; ตั้ง `alerts.channels: [console, telegram]` + token ใน `.env`)
```bash
python main.py test-alert
```

**ขั้นที่ 6 — รัน 1 รอบด้วยมือ**
```bash
python main.py run-once
```
ผลลัพธ์ต่อหุ้น: signal (BUY/SELL/HOLD), ราคา, และ action (BOUGHT / SOLD / blocked: …).
การรันซ้ำบนแท่งเดิม **จะไม่ส่งคำสั่งซ้ำ** (หนึ่ง signal ต่อหนึ่งแท่ง)

**ขั้นที่ 7 — ดูสถานะ**
```bash
python main.py status             # equity, cash, open positions, SL/TP, daily PnL
python main.py summary            # หุ้นตัวไหนขึ้น/ลงเท่าไหร่วันนี้
python main.py dashboard          # http://127.0.0.1:8000
```

**ขั้นที่ 8 — รันอัตโนมัติตามเวลา**
```bash
python main.py schedule           # end-of-day job ตาม scheduler.end_of_day
python main.py schedule --run-now # รัน EOD ทันที 1 ครั้งแล้วรอรอบถัดไป
```
- **End of day**: หลังตลาดปิด → ดึงแท่งรายวัน → สัญญาณ → order → daily summary → ส่งรายงาน
- **Intraday**: เปิด `scheduler.intraday.enabled: true` (เช่นทุก 5 นาที, interval `5m`) — ทำงานเฉพาะเวลาตลาดเปิด
- ให้รันตลอดเวลาได้ด้วย `systemd`, `tmux`, `supervisor` หรือ cron (`run-once` ทุกวันหลังตลาดปิด):
  ```cron
  30 16 * * 1-5  cd /path/stock_bot && .venv/bin/python main.py run-once >> logs/cron.log 2>&1
  ```

**ขั้นที่ 9 — ปิดสถานะด้วยมือ (ถ้าต้องการ)**
```bash
python main.py close AAPL
```

แนะนำให้ Paper trade **อย่างน้อยหลายเดือน** และเทียบผลกับ backtest ก่อนคิดเรื่องเงินจริง

## 6. Backtesting

```bash
python main.py backtest --symbol AAPL --strategy rsi --param period=10 --param oversold=25 --trades
python main.py backtest --symbol PTT.BK --strategy breakout --start 2024-01-01 --end 2025-12-31 --csv ptt.csv
python main.py compare  --symbol NVDA
```

โมเดลการจำลอง (ป้องกัน look-ahead bias):
- สัญญาณจากราคาปิดแท่ง *t* → **เข้า/ออกที่ราคาเปิดแท่ง t+1**
- Stop-loss/Take-profit ตรวจ intrabar ด้วย high/low; ถ้า gap ทะลุ → fill ที่ราคาเปิด; ถ้าโดนทั้งคู่ในแท่งเดียว → สมมติว่าโดน stop ก่อน (conservative)
- คิด slippage + commission (+ VAT) ทุกครั้ง, ปัดจำนวนหุ้นตาม board lot
- ใช้ RiskManager เดียวกับระบบจริง (position sizing, drawdown limit)

**Metrics**: Total return, CAGR, **Win rate, Profit factor, Max drawdown, Sharpe**, Sortino, volatility, expectancy, exposure, เทียบ Buy & Hold

ตัวอย่างผล (`examples/backtest_example.py`, ข้อมูล synthetic):
```
              return_%  cagr_%  max_dd_%  sharpe  trades  win_rate_%  profit_factor  buy_hold_%
ma_crossover     -0.21   -0.10      1.49   -0.12       4        25.0           0.82      -10.27
rsi              -0.31   -0.15      1.57   -0.18       5        40.0           0.80      -10.27
bollinger        -0.41   -0.20      1.22   -0.24      12        66.7           0.80      -10.27
breakout          0.26    0.12      1.26    0.15       5        40.0           1.24      -10.27
momentum          0.02    0.01      1.18    0.01       6        33.3           1.01      -10.27
```
(ตัวเลขเหล่านี้มาจากข้อมูลสุ่ม ใช้แสดงรูปแบบผลลัพธ์เท่านั้น — กลยุทธ์ "ชนะ" ในตารางไม่ได้แปลว่าจะชนะในตลาดจริง)

## 7. กลยุทธ์

| ชื่อ | แนวคิด | BUY | SELL | Parameters (default) |
|---|---|---|---|---|
| `ma_crossover` | Trend | fast MA ตัดขึ้น slow MA | ตัดลง | `fast_period=20, slow_period=50, ma_type=ema` |
| `rsi` | Mean reversion | RSI ตัดขึ้นเหนือ oversold | RSI ตัดขึ้นเหนือ overbought | `period=14, oversold=30, overbought=70` |
| `bollinger` | Mean reversion | close กลับขึ้นเหนือ lower band | close ตัดขึ้น middle/upper | `period=20, std_dev=2.0, exit_at=middle` |
| `breakout` | Trend (Donchian) | close > high สูงสุด N แท่งก่อนหน้า (+volume filter) | close < low ต่ำสุด M แท่ง | `entry_period=20, exit_period=10, volume_factor=1.0` |
| `momentum` | Momentum | ROC > threshold และราคาเหนือ SMA ระยะยาว | ROC < exit threshold | `period=20, entry_threshold=5, exit_threshold=0, trend_period=100` |

- เลือกกลยุทธ์หลักที่ `strategy.name` หรือกำหนดรายหุ้นด้วย `watchlist[].strategy`
- ทุกกลยุทธ์เป็น **Long-only** (ไม่ short)
- เพิ่มกลยุทธ์ใหม่: สืบทอด `BaseStrategy` (กำหนด `default_params`, `min_bars`, `compute_indicators`, `_signals`) แล้วเพิ่มใน `strategies/__init__.py`

## 8. Risk Management

| Parameter | ความหมาย |
|---|---|
| `position_size_pct` | มูลค่าสูงสุดต่อสถานะ (% ของ equity) |
| `risk_per_trade_pct` | ความเสี่ยงต่อเทรด = equity × % ÷ (entry − stop) — ใช้ค่าที่น้อยกว่ากับข้อแรก |
| `stop_loss_pct` / `take_profit_pct` | ตั้ง SL/TP อัตโนมัติจากราคา fill จริง |
| `max_daily_loss_pct` | ขาดทุนวันนี้เกินกำหนด → หยุดเปิดสถานะใหม่ |
| `max_open_positions` | จำนวนสถานะสูงสุด (นับ pending entry orders ด้วย) |
| `max_drawdown_pct` | drawdown จาก peak เกินกำหนด → หยุดเปิดสถานะใหม่ + alert |
| `min_cash_reserve_pct` | เงินสดขั้นต่ำที่กันไว้ |

การเปิดสถานะที่ถูกบล็อกจะถูกบันทึกใน `signals.action_note` และแจ้งเตือน (มี cooldown)

## 9. Execution: Paper vs Real

**Paper (ค่าเริ่มต้น)** — `PaperBroker`
- Market order: fill ที่ราคาล่าสุด ± `slippage_pct`
- Limit order: fill ถ้าราคาแตะ limit, ไม่เช่นนั้นเป็น `PENDING` และตรวจใหม่ทุกรอบจนหมดอายุ (`limit_order_ttl_minutes`)
- คิด commission (`commission_pct`, `min_commission`, `commission_vat_pct`)
- ทุก order (รวมที่ถูก reject) อยู่ในตาราง `orders`, ทุกสถานะใน `trades`, log แยกใน `logs/trades.log`

**Real trading** — ต้องผ่าน 3 ชั้นยืนยัน (`execution/safety.py`):
1. `execution.mode: live` และ `broker: alpaca` (config validation ห้าม live + paper broker)
2. ตัวแปรแวดล้อม `LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_THE_RISKS`
3. พิมพ์ชื่อ portfolio ยืนยันใน terminal แบบ interactive (รันแบบไม่มี terminal จะถูกปฏิเสธ)

และมี hard cap `live_max_order_value` ต่อ order

- **US**: `AlpacaBroker` (REST v2). ค่าเริ่มต้นชี้ไป `paper-api.alpaca.markets` — ทดสอบที่นั่นก่อน
- **TH**: `SettradeBroker` เป็นโครงไว้เท่านั้น ต้องสมัคร SETTRADE Open API ผ่านโบรกเกอร์และใช้ `settrade-v2` SDK
- ใช้ `portfolio_name` แยกกันระหว่าง paper/live (ระบบจะ error ถ้าใช้ชื่อเดียวกันข้ามโหมด)
- ⚠️ โค้ดฝั่ง live broker **ยังไม่ได้ทดสอบกับบัญชีจริง** — ใช้เป็นจุดเริ่มต้นและตรวจสอบเองอย่างละเอียด

## 10. Monitoring, Alerts, Logging, Dashboard

**Alerts** (`alerts:` ใน config) — แจ้งเมื่อ:
- มีสัญญาณ BUY/SELL, มีการซื้อ/ขาย (พร้อม PnL)
- หุ้นขึ้น/ลงเกิน `price_move_pct` %
- Drawdown เกิน `drawdown_alert_pct`
- Error (ดึงข้อมูลล้มเหลว, broker error, exception)
- รายงานสรุปหลังตลาดปิด (end-of-day job)

ช่องทาง: `console`, `telegram` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), `discord` (`DISCORD_WEBHOOK_URL`),
`line` (LINE Messaging API: `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_TO`) — alert ที่ส่งไม่สำเร็จจะไม่ทำให้ระบบล่ม และถูกบันทึกใน `alert_logs`

**Logging** (`logs/`): `stock_bot.log` (ทั้งหมด), `errors.log` (ERROR + traceback), `trades.log` (เฉพาะ order/trade), หมุนไฟล์ตาม `rotation`/`retention`

**Error handling**: retry + exponential backoff สำหรับ API, fallback provider, transaction แยกต่อหุ้น (หุ้นหนึ่งพังไม่กระทบตัวอื่น), ถ้าดึงข้อมูลไม่ได้จะใช้ข้อมูลล่าสุดใน DB ต่อ

**Dashboard** (`python main.py dashboard`): KPI พอร์ต, equity curve, PnL รายวัน/รายเดือน, open positions (unrealized PnL, SL/TP), signals, daily summary ขึ้น/ลง, trades, alerts — refresh ทุก 30 วินาที
JSON API: `/api/portfolio`, `/api/positions`, `/api/trades`, `/api/orders`, `/api/signals`, `/api/equity`,
`/api/pnl/daily`, `/api/pnl/monthly`, `/api/daily-summary`, `/api/prices/{symbol}`, `/api/alerts`
(Dashboard ไม่มีระบบ login — เปิดเฉพาะ `127.0.0.1` หรือวางหลัง reverse proxy ที่มี auth)

## 11. Configuration

- YAML: `config/config.yaml` (ถ้าไม่มีจะใช้ `config.example.yaml`) หรือระบุ `-c path.yaml`
- Secrets: `.env` แล้วอ้างอิงใน YAML ด้วย `${VAR}` หรือ `${VAR:-default}` — **อย่า commit `.env`**
- ทุกค่าถูก validate ด้วย Pydantic (เช่น `fast_period < slow_period`, `0 < oversold < overbought < 100`)
- ดูตัวอย่างครบทุก key ใน [`config/config.example.yaml`](config/config.example.yaml)

## 12. Database

10 ตาราง: `stocks`, `price_history`, `signals`, `orders`, `trades`, `portfolios`, `portfolio_snapshots`,
`daily_summary`, `backtest_runs`, `alert_logs` — รายละเอียด, ER diagram, สูตร % change และตัวอย่าง SQL ที่ [docs/DATABASE.md](docs/DATABASE.md)

`price_history` บันทึก `prev_close`, `change`, `percent_change` และ `direction` = `UP` / `DOWN` / `UNCHANGED` ทุกแท่ง
และคำนวณใหม่อัตโนมัติเมื่อมีการเติมข้อมูลย้อนหลัง

## 13. หุ้นไทย vs หุ้นต่างประเทศ

| | หุ้นไทย (SET) | หุ้นสหรัฐ |
|---|---|---|
| Symbol (Yahoo) | `PTT.BK`, `CPALL.BK` | `AAPL`, `SPY` |
| เวลาซื้อขาย | 10:00–12:30, 14:30–16:30 (Asia/Bangkok) | 09:30–16:00 (America/New_York) |
| Board lot | 100 หุ้น (`lot_size: 100`) | 1 |
| ค่าคอม | ~0.15% + VAT 7% (`commission_vat_pct: 0.07`) | มักเป็น 0 (+ ค่าธรรมเนียมกำกับ) |
| Live execution | SETTRADE Open API (ยังไม่ implement) | Alpaca |

- ใช้ `config/config.thai.example.yaml` เป็นจุดเริ่มต้น
- **หนึ่ง portfolio = หนึ่งสกุลเงิน** ระบบไม่แปลงค่าเงิน แยก config/DB/portfolio ต่อตลาด
- วันหยุดตลาดใส่ใน `markets.<TH|US>.holidays` (ระบบไม่มีปฏิทินวันหยุดในตัว)
- Ceiling/floor (±30%), price spread (tick size), ช่วง ATO/ATC ของ SET ยังไม่ได้จำลอง

## 14. Tests

```bash
cd stock_bot
pytest                       # 69 tests, offline, ~15s
```
ครอบคลุม: indicators, % change/direction, ทุกกลยุทธ์ (รวม test ว่าไม่มี look-ahead), risk manager, cost model,
paper broker (market/limit), backtester + metrics, DB upsert/back-fill, fallback provider, collector,
daily summary, engine end-to-end (เปิด/ปิด trade, stop-loss, idempotency, max positions, error handling,
limit orders, file-based SQLite locking), live-mode confirmation, config/env, market hours, retry, alerts, dashboard API

## 15. ข้อจำกัดและข้อควรระวัง

### ด้านการเงินและความเสี่ยง
- **คุณอาจสูญเสียเงินทั้งหมดที่ลงทุน** ระบบอัตโนมัติอาจทำผิดพลาดซ้ำ ๆ ได้เร็วกว่ามนุษย์
- Backtest มีอคติหลายอย่าง: overfitting (ปรับ parameter จนพอดีอดีต), survivorship bias (watchlist ที่เลือกจากหุ้นที่ยังอยู่วันนี้), look-ahead ในข้อมูลที่ถูกปรับย้อนหลัง, ไม่จำลองสภาพคล่อง/market impact/การหยุดพักซื้อขาย (SP/halt)/ceiling-floor
- ข้อมูล Yahoo Finance **ไม่เป็นทางการ** อาจล่าช้า (~15 นาที), ขาดหาย, ผิดพลาด, ถูก rate limit หรือเลิกใช้งานได้; ไม่เหมาะกับ intraday ความถี่สูง
- Stop-loss ในระบบนี้ตรวจตอนที่ engine รัน (เช่นวันละครั้งสำหรับ `1d`) **ไม่ใช่คำสั่ง stop ที่วางไว้ที่โบรกเกอร์** — ราคาอาจทะลุไปไกลก่อนถูกปิด หากใช้จริงควรใช้ bracket/stop order ของโบรกเกอร์
- การเชื่อมต่อขาด, ไฟดับ, API เปลี่ยน, bug — ต้องมีการเฝ้าดูตลอด และมีแผนปิดสถานะด้วยมือ
- Partial fills, corporate actions (ปันผล/แตกพาร์), short selling, margin ไม่ได้รองรับเต็มรูปแบบ
- ใช้ `FLOAT` เก็บราคา (เหมาะกับงานวิจัย ไม่ใช่ระบบบัญชี)

### ด้านกฎหมายและข้อกำหนด (ไม่ใช่คำแนะนำทางกฎหมาย — ตรวจสอบกับผู้เชี่ยวชาญ)
- **ประเทศไทย**: การให้คำแนะนำการลงทุนหรือบริหารเงินให้ผู้อื่นต้องได้รับใบอนุญาตจาก **ก.ล.ต.** — ห้ามนำระบบนี้ไปให้บริการ/ขายสัญญาณ/บริหารพอร์ตให้ผู้อื่นโดยไม่มีใบอนุญาต; การส่งคำสั่งอัตโนมัติต้องผ่านช่องทางที่โบรกเกอร์อนุญาต (เช่น SETTRADE Open API) และปฏิบัติตามเงื่อนไขของโบรกเกอร์/ตลาดหลักทรัพย์ (รวมถึงกฎเกี่ยวกับ program trading / HFT และการห้ามสร้างราคา/ปั่นหุ้น)
- **สหรัฐฯ**: Pattern Day Trader rule (บัญชี margin < $25,000 เทรดในวันเดียวได้จำกัด), กฎ wash sale ทางภาษี, เงื่อนไขการใช้ API ของโบรกเกอร์
- **ภาษี**: กำไรจากการลงทุน/เงินปันผล/ภาษีต่างประเทศ เป็นความรับผิดชอบของผู้ใช้
- **ข้อมูล**: ตรวจสอบเงื่อนไขการใช้งานของผู้ให้ข้อมูล (Yahoo, Alpha Vantage) โดยเฉพาะการใช้เชิงพาณิชย์/การเผยแพร่ซ้ำ
- **ความปลอดภัย**: เก็บ API key ใน `.env` เท่านั้น, ใช้สิทธิ์ key ต่ำสุดที่จำเป็น (ปิดสิทธิ์ถอนเงิน), อย่าเปิด dashboard สู่สาธารณะ

---
*Educational project. Not affiliated with SET, SETTRADE, Yahoo, Alpaca, or any broker.*
