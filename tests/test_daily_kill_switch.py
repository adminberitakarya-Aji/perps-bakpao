"""Test kill switch daily PnL (Fase 2): trigger, blokir entry, reset harian UTC, persist, equity-fallback.

Jalankan: python tests/test_daily_kill_switch.py
Konvensi sama dengan suite lain: script + assert, tanpa framework.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from types import SimpleNamespace

import src.engine as engine_mod
from src.engine import TradingEngine
from src.strategy.base import MarketSnapshot, Signal, SignalResult, Strategy
from src.risk.manager import RiskManager, RiskLimits
from src.utils.logger import get_logger

log = get_logger("test")

MID_PRICE = 50000.0
STUB_ATR = 50.0


class StubStrategy(Strategy):
    """Strategi deterministik: selalu BUY conf=0.8, ATR konstan via _to_df/_compute_indicators."""

    def generate_signal(self, snapshot: MarketSnapshot) -> SignalResult:
        return SignalResult(signal=Signal.BUY, confidence=0.8, reason="stub")

    def required_bars(self) -> int:
        return 60

    def _to_df(self, candles: list) -> pd.DataFrame:
        return pd.DataFrame(candles)

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["atr"] = STUB_ATR
        return df


class MockClient:
    def __init__(self, equity: float):
        self.equity = equity  # 0 -> wallet kosong (fallback)
        self.config = SimpleNamespace(use_testnet=True)  # dipakai heartbeat engine

    def get_account_state(self):
        return {"marginSummary": {"accountValue": self.equity}}

    def get_position(self, symbol):
        return None

    def get_mid_price(self, symbol):
        return MID_PRICE

    def cancel_all_trigger_orders(self, symbol):
        pass

    def place_market_order(self, symbol, is_buy, size, sl=None, tp=None):
        return {"status": "ok"}


class MockExecutor:
    def __init__(self, client=None):
        self.calls = []

    def execute(self, symbol, signal, size_usd, price, sl=None, tp=None):
        self.calls.append({"symbol": symbol, "signal": signal, "size_usd": size_usd})
        return {"status": "ok"}


# snapshot sintetis: isi engine tidak penting, hanya dilewat ke stub
def stub_fetch_snapshot(client, symbol, interval="1h", lookback_candles=120):
    candles = [[i, 100.0, 101.0, 99.0, 100.5, 10.0] for i in range(70)]
    return MarketSnapshot(symbol=symbol, mid_price=MID_PRICE, candles=candles)


engine_mod.fetch_snapshot = stub_fetch_snapshot

TMPDIR = tempfile.mkdtemp(prefix="hlbot_test_")
POS_FILE = os.path.join(TMPDIR, "live_positions.json")
DAILY_FILE = os.path.join(TMPDIR, "daily_state.json")


def read_daily_file():
    with open(DAILY_FILE) as f:
        return json.load(f)


def write_daily_file(state):
    with open(DAILY_FILE, "w") as f:
        json.dump(state, f, indent=1)


def build_engine(equity):
    """Engine baru (simulasi restart proses) dengan path state di tmpdir."""
    client = MockClient(equity)
    engine = TradingEngine(
        client=client,
        strategy=StubStrategy(),
        risk_manager=RiskManager(RiskLimits()),
        executor=MockExecutor(),
        symbols=["BTC"],
        interval="1h",
    )
    engine.state_path = POS_FILE
    engine.live_positions = {}
    engine.daily_state_path = DAILY_FILE
    engine._load_daily_state()
    return engine


def test_kill_switch_unit():
    print("=== Test 0 (unit): check_and_size return 0 saat daily_pnl <= -max_daily_loss ===")
    rm = RiskManager(RiskLimits())
    rm.daily_pnl_pct = -0.06
    size = rm.check_and_size(1000.0, Signal.BUY, confidence=0.9, sl_distance_pct=0.002)
    assert size == 0.0, f"kill switch harus blokir, dapat {size}"
    print("[OK] -6% -> size=0\n")


def test_trigger_and_block():
    print("=== Test 1: equity -6% dari baseline -> KILL SWITCH terpicu + entry diblokir ===")
    if os.path.exists(DAILY_FILE):
        os.remove(DAILY_FILE)

    engine = build_engine(1000.0)
    engine.run_once()
    assert len(engine.executor.calls) == 1, "hari normal harus menghasilkan 1 entry"
    daily = read_daily_file()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert daily["date_utc"] == today, "date harus hari ini (UTC)"
    assert abs(daily["day_start_equity"] - 1000.0) < 1e-9
    assert daily["kill_triggered"] is False
    print(f"[OK] baseline tercatat: {daily}")

    engine.client.equity = 940.0  # -6% dari baseline
    engine.run_once()
    assert len(engine.executor.calls) == 1, "entry baru WAJIB diblokir kill switch"
    assert engine.risk_manager.daily_pnl_pct <= -0.05, "daily_pnl_pct harus ter-injeksi (-6%)"
    daily = read_daily_file()
    assert daily["kill_triggered"] is True, "flag kill harus ter-persist"
    print(f"[OK] -6% -> entry diblokir, state: {daily}\n")


def test_restart_consistency():
    print("=== Test 2: restart proses di hari sama -> kill switch tetap aktif ===")
    engine = build_engine(940.0)  # engine baru, state dimuat dari file
    engine.run_once()
    assert len(engine.executor.calls) == 0, "setelah restart, kill harus tetap blokir"
    assert engine.risk_manager.daily_pnl_pct <= -0.05
    print("[OK] kill konsisten setelah restart\n")


def test_daily_reset():
    print("=== Test 3: ganti hari UTC -> baseline reset, kill switch terbuka ===")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    write_daily_file({"date_utc": yesterday, "day_start_equity": 1000.0, "kill_triggered": True})

    engine = build_engine(940.0)  # -6% dari baseline LAMA, tapi hari baru
    engine.run_once()
    assert len(engine.executor.calls) == 1, "hari baru (pnl 0%) harus boleh entry"
    daily = read_daily_file()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert daily["date_utc"] == today, "date harus berganti ke hari ini"
    assert abs(daily["day_start_equity"] - 940.0) < 1e-9, "baseline baru = equity saat rollover"
    assert daily["kill_triggered"] is False, "kill harus ter-reset"
    assert abs(engine.risk_manager.daily_pnl_pct) < 1e-9
    print(f"[OK] rollover -> baseline 940, kill ter-reset: {daily}\n")


def test_equity_fallback():
    print("=== Test 4: equity fallback (wallet kosong) -> tracker TIDAK di-update ===")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    write_daily_file({"date_utc": today, "day_start_equity": 1000.0, "kill_triggered": False})

    engine = build_engine(0.0)  # wallet kosong -> equity None
    engine.run_once()
    daily = read_daily_file()
    assert daily["date_utc"] == today, "tanggal tidak boleh berubah saat fallback"
    assert abs(daily["day_start_equity"] - 1000.0) < 1e-9, "baseline tidak boleh berubah saat fallback"
    assert daily["kill_triggered"] is False, "fallback tidak boleh memicu kill (PnL palsu)"
    assert engine.risk_manager.daily_pnl_pct == 0.0, "tracker tidak boleh di-update saat fallback"
    print("[OK] fallback -> daily state utuh, tidak ada PnL palsu")

    # wallet "kembali" dengan equity jauh di bawah baseline -> barulah kill valid
    engine.client.equity = 930.0  # -7%
    engine.run_once()
    daily = read_daily_file()
    assert daily["kill_triggered"] is True, "equity asli -7% harus memicu kill"
    print("[OK] equity asli kembali -7% -> kill terpicu\n")


if __name__ == "__main__":
    print("=== Test kill switch daily PnL (Fase 2) ===\n")
    test_kill_switch_unit()
    test_trigger_and_block()
    test_restart_consistency()
    test_daily_reset()
    test_equity_fallback()
    print("Semua test selesai -> trigger + reset harian + persist + fallback aman.")
