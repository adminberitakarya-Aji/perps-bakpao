"""Test perbaikan P2 (audit.md):
  P2-9   trailing via modify (tanpa cancel->replace) + fallback aman
  P2-10  monitor_kill_switch di luar siklus poll utama
  P2-11  heartbeat membaca kill_triggered aktual (bukan hardcoded False)
  P2-16  alert saat rekonstruksi state posisi (trailing nonaktif)

Semua stub, tanpa jaringan.
Jalankan: .venv\\Scripts\\python.exe tests\\test_p2_fixes.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

from src.engine import TradingEngine
from src.risk.manager import RiskManager, RiskLimits


# --- stub bersama -----------------------------------------------------------

class StubNotifier:
    def __init__(self):
        self.errors = []
        self.trailing = []
        self.force_close_trailing = []
        self.heartbeats = []
        self.kill_switches = []

    def notify_error(self, context, error):
        self.errors.append(context)

    def notify_trailing(self, symbol, old_sl, new_sl, px):
        self.trailing.append((symbol, old_sl, new_sl, px))

    def notify_force_close_trailing(self, symbol, sl, px):
        self.force_close_trailing.append((symbol, sl, px))

    def notify_heartbeat(self, date, equity, pnl, positions, kill, testnet):
        self.heartbeats.append((date, equity, pnl, positions, kill, testnet))

    def notify_kill_switch(self, *a):
        self.kill_switches.append(a)

    def notify_closed(self, *a, **k):
        pass


def make_engine(client, notifier, use_trailing=False):
    rm = RiskManager(RiskLimits())
    rm.limits.use_trailing = use_trailing
    engine = TradingEngine(
        client=client,
        strategy=object(),
        risk_manager=rm,
        executor=object(),
        symbols=["BTC"],
        notifier=notifier,
    )
    engine.state_path = tempfile.mkstemp(suffix=".json")[1]
    engine.daily_state_path = tempfile.mkstemp(suffix=".json")[1]
    return engine


# --- P2-9: trailing via modify ----------------------------------------------

class TrailingStubClient:
    config = SimpleNamespace(use_testnet=True)

    def __init__(self, modify_error=None):
        self.modify_calls = []
        self.replaced = []
        self.cancel_calls = 0
        self.close_calls = 0
        self.modify_error = modify_error

    def get_position(self, symbol):
        return {"szi": 0.01, "entryPx": 60000.0, "side": "B"}

    def get_trigger_orders(self, symbol):
        # SL aktif di exchange dgn oid -> jalur modify harus terpakai
        return [{"oid": 777, "triggerCondition": "sl", "triggerPx": "59000"}]

    def modify_sl_trigger(self, symbol, oid, close_is_buy, size, new_sl):
        self.modify_calls.append((symbol, oid, close_is_buy, size, new_sl))
        if self.modify_error:
            raise RuntimeError("simulasi modify gagal")
        return {"status": "ok"}

    def place_tpsl_pair(self, symbol, close_is_buy, size, sl, tp=None):
        self.replaced.append((symbol, close_is_buy, size, sl, tp))
        return {"status": "ok"}

    def cancel_all_trigger_orders(self, symbol):
        self.cancel_calls += 1

    def market_close_position(self, symbol):
        self.close_calls += 1
        return {"status": "ok"}

    def get_mid_price(self, symbol):
        return 61000.0


STATE = {"side": "B", "entry_price": 60000.0, "entry_atr": 500.0, "sl": 59000.0, "tp": 62000.0}
# trailing: profit 1000 >= 1.5*500 -> aktif; new_sl = 61000 - 1.2*500 = 60400


def test_trailing_uses_modify_no_cancel_window():
    client = TrailingStubClient()
    notifier = StubNotifier()
    engine = make_engine(client, notifier, use_trailing=True)
    engine.live_positions = {"BTC": dict(STATE)}
    engine._manage_open_positions("BTC")

    assert len(client.modify_calls) == 1, "SL harus digeser via modify (in-place)"
    sym, oid, close_is_buy, size, new_sl = client.modify_calls[0]
    assert sym == "BTC" and oid == 777
    assert close_is_buy is False and size == 0.01
    assert abs(new_sl - 60400.0) < 1e-6, f"new_sl salah: {new_sl}"
    assert client.cancel_calls == 0, "TIDAK boleh cancel trigger (tidak ada window tanpa SL)"
    assert client.replaced == [], "TIDAK boleh place pair baru saat modify sukses"
    assert engine.live_positions["BTC"]["sl"] == new_sl, "state SL harus ter-update"
    assert notifier.trailing, "harus ada notifikasi trailing"
    print("test_trailing_uses_modify_no_cancel_window: OK")


def test_trailing_modify_fails_falls_back_to_old_pair():
    client = TrailingStubClient(modify_error=True)
    notifier = StubNotifier()
    engine = make_engine(client, notifier, use_trailing=True)
    engine.live_positions = {"BTC": dict(STATE)}
    engine._manage_open_positions("BTC")

    assert len(client.modify_calls) == 1, "modify dicoba dulu"
    assert client.cancel_calls == 1 and len(client.replaced) == 1, (
        "modify gagal -> fallback cancel + pasang pair lama"
    )
    sym, close_is_buy, size, sl, tp = client.replaced[0]
    assert sl == 59000.0 and tp == 62000.0, "fallback harus pakai SL/TP LAMA dari state"
    assert engine.live_positions["BTC"]["sl"] == 59000.0, "state tidak berubah saat modify gagal"
    assert client.close_calls == 0, "fallback sukses -> tidak boleh force-close"
    print("test_trailing_modify_fails_falls_back_to_old_pair: OK")


# --- P2-10: monitor_kill_switch di luar poll utama ---------------------------

class EquityStubClient:
    config = SimpleNamespace(use_testnet=True)

    def __init__(self, equity):
        self.equity = equity

    def get_account_state(self):
        return {"marginSummary": {"accountValue": str(self.equity)}}


def test_monitor_kill_switch_triggers_immediately():
    client = EquityStubClient(800.0)  # -20% dari baseline 1000
    notifier = StubNotifier()
    engine = make_engine(client, notifier)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    engine.daily_state = {"date_utc": today, "day_start_equity": 1000.0, "kill_triggered": False}

    engine.monitor_kill_switch()  # dipanggil loop monitoring tiap ~60s

    assert engine.risk_manager.daily_pnl_pct <= -0.05
    assert engine.daily_state["kill_triggered"] is True, "kill switch harus terpicu SEKARANG"
    assert notifier.kill_switches, "alert kill switch harus terkirim tanpa nunggu poll 1 jam"
    print("test_monitor_kill_switch_triggers_immediately: OK")


def test_monitor_kill_switch_healthy_and_no_equity():
    notifier = StubNotifier()
    engine = make_engine(EquityStubClient(1010.0), notifier)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    engine.daily_state = {"date_utc": today, "day_start_equity": 1000.0, "kill_triggered": False}
    engine.monitor_kill_switch()
    assert engine.daily_state["kill_triggered"] is False

    # equity tidak tersedia -> tidak exception, state tidak berubah (fail-safe)
    engine2 = make_engine(EquityStubClient(0.0), notifier)
    engine2.daily_state = {"date_utc": today, "day_start_equity": 1000.0, "kill_triggered": True}
    engine2.monitor_kill_switch()
    assert engine2.daily_state["kill_triggered"] is True, "state tidak boleh berubah dgn data kosong"
    print("test_monitor_kill_switch_healthy_and_no_equity: OK")


# --- P2-11: heartbeat membaca kill_triggered aktual --------------------------

def test_heartbeat_reads_actual_kill_flag():
    client = EquityStubClient(800.0)
    notifier = StubNotifier()
    engine = make_engine(client, notifier)
    engine.live_positions = {}
    # kemarin: kill switch sempat terpicu -> hari baru, rollover + heartbeat
    engine.daily_state = {
        "date_utc": "2000-01-01",
        "day_start_equity": 1000.0,
        "kill_triggered": True,
    }
    engine._update_daily_pnl()  # jalur yang sama dipakai monitor & run_once

    assert len(notifier.heartbeats) == 1, "rollover harus kirim heartbeat sekali"
    kill_arg = notifier.heartbeats[0][4]
    assert isinstance(kill_arg, bool), "arg kill harus bool dari daily_state, bukan literal"
    assert kill_arg == engine.daily_state["kill_triggered"], (
        "heartbeat harus mencerminkan nilai kill_triggered aktual"
    )
    print("test_heartbeat_reads_actual_kill_flag: OK")


# --- P2-16: alert rekonstruksi state -----------------------------------------

class ReconstructStubClient:
    config = SimpleNamespace(use_testnet=True)

    def get_position(self, symbol):
        return {"szi": 0.02, "entryPx": 61000.0, "side": "B"}

    def get_trigger_orders(self, symbol):
        return []


def test_reconstruction_sends_alert():
    client = ReconstructStubClient()
    notifier = StubNotifier()
    engine = make_engine(client, notifier)
    engine.live_positions = {}  # posisi ada di exchange tapi state hilang

    engine._manage_open_positions("BTC")

    assert "BTC" in engine.live_positions, "state harus direkonstruksi"
    assert engine.live_positions["BTC"]["entry_atr"] is None
    assert notifier.errors, "harus ada ALERT ke user (trailing nonaktif)"
    assert any("trailing" in e.lower() or "NONAKTIF" in e for e in notifier.errors)
    print("test_reconstruction_sends_alert: OK")


if __name__ == "__main__":
    test_trailing_uses_modify_no_cancel_window()
    test_trailing_modify_fails_falls_back_to_old_pair()
    test_monitor_kill_switch_triggers_immediately()
    test_monitor_kill_switch_healthy_and_no_equity()
    test_heartbeat_reads_actual_kill_flag()
    test_reconstruction_sends_alert()
    print("\nSemua test P2 lulus.")

