"""Test perbaikan P1 (audit.md):
  P1-4  self-healing SL hilang (re-place -> gagal -> force-close)
  P1-5  validasi respons order SDK (validate_order_result / OrderRejectedError)
  P1-6  sizing fail-closed (tanpa fallback equity $1000)
  P1-7  polling selaras boundary candle (seconds_until_next_poll)

Jalankan: .venv\\Scripts\\python.exe tests\\test_p1_fixes.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
from types import SimpleNamespace

# --- P1-5: validate_order_result -------------------------------------------
from src.client import OrderRejectedError, validate_order_result

OK_FILLED = {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": "0.01"}}]}}}
OK_RESTING = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}
STATUS_ERR = {"status": "err", "response": "User or API Wallet 0x... does not exist"}
PER_ORDER_ERR = {"status": "ok", "response": {"data": {"statuses": [{"filled": {}}, {"error": "Insufficient margin"}]}}}
PER_ORDER_ERR_STR = {"status": "ok", "response": {"data": {"statuses": ["error: margin"]}}}


def test_validate_order_result():
    assert validate_order_result(OK_FILLED, "ctx") is OK_FILLED
    assert validate_order_result(OK_RESTING, "ctx") is OK_RESTING

    for bad, label in [
        (STATUS_ERR, "status err"),
        (PER_ORDER_ERR, "per-order dict error"),
        (PER_ORDER_ERR_STR, "per-order str error"),
        ("bukan-dict", "respons bukan dict"),
        (None, "respons None"),
    ]:
        try:
            validate_order_result(bad, "ctx")
            raise AssertionError(f"seharusnya raise untuk {label}")
        except OrderRejectedError:
            pass
    print("test_validate_order_result: OK")


# --- P1-7: seconds_until_next_poll -----------------------------------------
from main import CANDLE_CLOSE_BUFFER_SECONDS, POLL_INTERVAL_SECONDS, seconds_until_next_poll


def test_seconds_until_next_poll():
    interval = POLL_INTERVAL_SECONDS  # 3600
    buffer_s = CANDLE_CLOSE_BUFFER_SECONDS  # 300

    def expected_target(now):
        cur = int(now // interval) * interval + buffer_s
        return cur if now < cur else cur + interval

    for now in [3600 + 143.0, 0.0, 3700.0, 7199.9, 7499.9, 7500.0, 12345.678]:
        wait = seconds_until_next_poll(interval, buffer_s, now=now)
        assert wait >= 1.0, f"wait harus >= 1s (now={now})"
        if wait > 1.0:
            assert abs((now + wait) - expected_target(now)) < 1e-6, (
                f"poll tidak selaras boundary: now={now} wait={wait} "
                f"target={expected_target(now)}"
            )
        else:
            # clamp 1s: target tercapai/terlewati maksimal 1 detik
            assert 0.0 <= now + wait - expected_target(now) <= 1.0

    # now 0.1s sebelum target (01:04:59.9 utk 1H+5m) -> tunggu target itu (clamp 1s)
    now = interval + buffer_s - 0.1
    assert seconds_until_next_poll(interval, buffer_s, now=now) == 1.0

    # default buffer & tanpa `now` (pakai time.time()) tetap sehat
    assert seconds_until_next_poll(60) >= 1.0
    print("test_seconds_until_next_poll: OK")


# --- P1-4 + P1-6: self-healing SL & fail-closed sizing (stub, tanpa jaringan)
from src.engine import TradingEngine
from src.risk.manager import RiskManager, RiskLimits


class StubClient:
    config = SimpleNamespace(use_testnet=True)

    def __init__(self, triggers, fail_replace=False):
        self.triggers = triggers
        self.fail_replace = fail_replace
        self.replaced = []
        self.cancel_calls = 0
        self.close_calls = 0

    def get_position(self, symbol):
        return {"szi": 0.01, "entryPx": 60000.0, "side": "B"}

    def get_trigger_orders(self, symbol):
        return self.triggers

    def place_tpsl_pair(self, symbol, close_is_buy, size, sl, tp=None):
        if self.fail_replace:
            raise RuntimeError("simulasi gagal re-place")
        self.replaced.append((symbol, close_is_buy, size, sl, tp))
        return {"status": "ok"}

    def cancel_all_trigger_orders(self, symbol):
        self.cancel_calls += 1

    def market_close_position(self, symbol):
        self.close_calls += 1
        return {"status": "ok"}

    def get_mid_price(self, symbol):
        return 60000.0


class StubNotifier:
    def __init__(self):
        self.errors = []
        self.force_closes = []
        self.closed = []

    def notify_error(self, context, error):
        self.errors.append(context)

    def notify_force_close(self, symbol, order_desc, detail=""):
        self.force_closes.append((symbol, order_desc))

    def notify_closed(self, *a, **k):
        self.closed.append(a)


def make_engine(client, notifier):
    rm = RiskManager(RiskLimits())
    rm.limits.use_trailing = False  # fokus ke guard SL, bukan trailing
    engine = TradingEngine(
        client=client,
        strategy=object(),  # _manage_open_positions tidak memakai strategi
        risk_manager=rm,
        executor=object(),
        symbols=["BTC"],
        notifier=notifier,
    )
    engine.state_path = tempfile.mkstemp(suffix=".json")[1]  # jangan sentuh state live
    engine.daily_state_path = tempfile.mkstemp(suffix=".json")[1]
    return engine


STATE = {"side": "B", "entry_price": 60000.0, "entry_atr": 500.0, "sl": 59000.0, "tp": 62000.0}


def test_self_heal_replaces_missing_sl():
    client = StubClient(triggers=[])  # SL hilang di exchange
    notifier = StubNotifier()
    engine = make_engine(client, notifier)
    engine.live_positions = {"BTC": dict(STATE)}
    engine._manage_open_positions("BTC")
    assert len(client.replaced) == 1, "SL harus di-pasang ulang"
    sym, close_is_buy, size, sl, tp = client.replaced[0]
    assert sym == "BTC" and sl == 59000.0 and tp == 62000.0
    assert close_is_buy is False, "posisi B ditutup dengan sell"
    assert size == 0.01
    assert notifier.errors, "harus ada alert re-place"
    assert client.close_calls == 0, "re-place sukses -> tidak boleh force-close"
    print("test_self_heal_replaces_missing_sl: OK")


def test_self_heal_force_close_when_replace_fails():
    client = StubClient(triggers=[], fail_replace=True)
    notifier = StubNotifier()
    engine = make_engine(client, notifier)
    engine.live_positions = {"BTC": dict(STATE)}
    engine._manage_open_positions("BTC")
    assert client.cancel_calls == 1 and client.close_calls == 1, "re-place gagal -> wajib tutup paksa"
    assert notifier.force_closes, "harus ada alert force-close"
    print("test_self_heal_force_close_when_replace_fails: OK")


def test_no_replace_when_sl_active():
    client = StubClient(triggers=[{"triggerCondition": "sl", "oid": 9}])
    notifier = StubNotifier()
    engine = make_engine(client, notifier)
    engine.live_positions = {"BTC": dict(STATE)}
    engine._manage_open_positions("BTC")
    assert not client.replaced and client.close_calls == 0, "SL masih ada -> jangan diutak-atik"
    print("test_no_replace_when_sl_active: OK")


def test_no_fallback_sizing():
    client = StubClient(triggers=[{"triggerCondition": "sl"}])
    engine = make_engine(client, StubNotifier())
    assert not hasattr(engine, "_get_equity_usd"), "fallback $1000 harus sudah dihapus"
    # Stub client tanpa get_account_state -> equity None (fail-closed path)
    assert engine._get_equity_or_none() is None
    print("test_no_fallback_sizing: OK")


if __name__ == "__main__":
    test_validate_order_result()
    test_seconds_until_next_poll()
    test_self_heal_replaces_missing_sl()
    test_self_heal_force_close_when_replace_fails()
    test_no_replace_when_sl_active()
    test_no_fallback_sizing()
    print("\nSEMUA TEST P1 LULUS")


    # default buffer & tanpa `now` (pakai time.time()) tetap sehat
    assert seconds_until_next_poll(60) >= 1.0
    print("test_seconds_until_next_poll: OK")
