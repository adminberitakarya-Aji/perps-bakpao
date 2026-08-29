"""Test notifier Telegram (Fase 3): silent mode, gagal-kirim aman, semua event terpicu.

Jalankan: python tests/test_notifier.py
Uji nyata kirim (butuh token asli): python -m src.utils.notifier
"""

import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.notifier import TelegramNotifier
from tests.test_daily_kill_switch import build_engine  # mock engine + client dari suite kill switch

EVENT_MARKERS = [
    ("entry", "ENTRY"),
    ("trailing", "TRAILING"),
    ("closed", "TERTUTUP"),
    ("kill", "KILL SWITCH"),
    ("force_close", "FORCE-CLOSE"),
    ("force_close_trailing", "FORCE-CLOSE"),
    ("error", "ERROR"),
    ("heartbeat", "HEARTBEAT"),
]


class RecordingNotifier(TelegramNotifier):
    """Notifier palsu: rekam (text, silent) tanpa jaringan."""

    def __init__(self):
        super().__init__("recording-token", "recording-chat")
        self.sent = []  # list of (text, silent)

    def _send(self, text, silent):
        self.sent.append((text, silent))

    def texts_with(self, marker: str) -> list:
        return [t for t, _ in self.sent if marker in t]

    def silent_of(self, marker: str) -> list:
        return [s for t, s in self.sent if marker in t]


def fire_all_events(n: TelegramNotifier):
    n.notify_entry("BTC", "SELL", 0.00628, 490.18, 78054.12, 79669.31, 75687.28, 0.65, 940.0, "FOLLOW SELL: close<EMA, ADX=25.2, RSI=37.0")
    n.notify_trailing("BTC", 79200.0, 79669.31, 78100.0)
    n.notify_closed("BTC", "S", 948.0, -0.012, False)
    n.notify_kill_switch(-0.053, 0.05, 947.0, 1000.0)
    n.notify_force_close("BTC", "SELL 0.00628 BTC (~$490)")
    n.notify_force_close_trailing("BTC", 79669.31, 78100.0)
    n.notify_error("di run_once", RuntimeError("KeyError: 'assetPositions'"))
    n.notify_heartbeat("2026-08-30", 1012.50, 0.0125, [("BTC", "S", 0.00628, 78054.12, 79200.0, 75687.28)], False, True)



def test_silent_mode():
    print("=== Test 1: tanpa token/chat_id -> no-op penuh, tidak error ===")
    n = TelegramNotifier()
    assert n.enabled is False
    fire_all_events(n)  # tidak boleh raise apa pun
    print("[OK] silent mode no-op\n")


def test_fail_send_safety():
    print("=== Test 2: gagal kirim (token palsu) -> TIDAK crash ===")
    n = TelegramNotifier("INVALID_TOKEN", "12345")
    assert n.enabled is True
    # token palsu -> Telegram balas 404; harus tertelan jadi log warning
    n.notify_kill_switch(-0.06, 0.05, 940.0, 1000.0)
    n.notify_entry("BTC", "SELL", 0.00628, 490.18, 78054.12, 79669.31, 75687.28, 0.65, 940.0, "uji")
    print("[OK] kegagalan kirim tertelan (lihat log warning 'gagal kirim alert')\n")


def test_all_events_and_loudness():
    print("=== Test 3: semua 8 event terpicu + aturan silent/loud benar ===")
    n = RecordingNotifier()
    fire_all_events(n)
    assert len(n.sent) == 8, f"harus 8 pesan, dapat {len(n.sent)}"

    # info events -> silent (notifikasi mati)
    for marker in ["ENTRY", "TRAILING", "TERTUTUP", "HEARTBEAT"]:
        assert n.texts_with(marker), f"event {marker} tidak terkirim"
        assert all(n.silent_of(marker)), f"{marker} harus silent"
        print(f"[OK] {marker}: silent")

    # loud events -> disable_notification=False (HP berbunyi)
    for marker in ["KILL SWITCH", "FORCE-CLOSE", "ERROR"]:
        assert n.texts_with(marker), f"event {marker} tidak terkirim"
        assert not any(n.silent_of(marker)), f"{marker} harus LOUD"
        print(f"[OK] {marker}: loud")

    # detail format: angka ter-format, monospace <code>
    entry_text = n.texts_with("ENTRY")[0]
    assert "78,054.12" in entry_text and "<code>" in entry_text, "format harga/monospace hilang"
    assert "0.00628" in entry_text and "79,669.31" in entry_text, "detail size/SL hilang"
    kill_text = n.texts_with("KILL SWITCH")[0]
    assert "-5.00%" in kill_text and "baseline" in kill_text, "detail kill switch hilang"
    hb_text = n.texts_with("HEARTBEAT")[0]
    assert "Kemarin" in hb_text and "+1.25%" in hb_text and "BTC SHORT" in hb_text, "detail heartbeat hilang"
    print("[OK] format detail & monospace terjaga\n")


def test_engine_wiring():
    print("=== Test 4: integrasi engine -> semua titik wiring terpicu via run_once/mock ===")
    n = RecordingNotifier()
    engine = build_engine(1000.0)
    engine.notifier = n
    engine.run_once()
    assert n.texts_with("ENTRY"), "entry alert harus terkirim lewat engine"
    assert n.texts_with("HEARTBEAT"), "heartbeat harus terkirim lewat engine (run pertama = rollover)"

    # turunkan equity -6% -> kill switch + posisi mock hilang -> alert kill & closed
    engine.client.equity = 940.0
    engine.run_once()
    assert n.texts_with("KILL SWITCH"), "kill switch alert harus terkirim"
    assert n.texts_with("TERTUTUP"), "closed alert harus terkirim saat posisi hilang dari exchange"
    print("[OK] entry/heartbeat/kill/closed terpicu lewat jalur engine nyata\n")


if __name__ == "__main__":
    print("=== Test notifier Telegram (Fase 3) ===\n")
    test_silent_mode()
    test_fail_send_safety()
    test_all_events_and_loudness()
    test_engine_wiring()
    print("Semua test selesai -> silent mode, fail-safe, 8 event, wiring engine OK.")
