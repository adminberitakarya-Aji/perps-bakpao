"""Notifier Telegram untuk alert bot (Fase 3).

Desain (disetujui 2026-08-29, layout "Indonesia, detail"):
- stdlib urllib saja (tanpa dependency baru), timeout 10 detik
- fire-and-forget: kegagalan kirim TIDAK PERNAH boleh mengganggu jalur
  trading (swallow + log warning)
- tanpa token/chat_id -> mode silent penuh (no-op, bot tetap jalan)
- Info (entry/trailing/closed/heartbeat) = notifikasi MATI (silent);
  kill switch / force-close / error = LOUD (HP berbunyi)
- parse_mode HTML + html.escape untuk teks dinamis; angka dibungkus <code>
  supaya monospace rapi (markdown tidak dipakai: rawan escape error)

Manual test kirim: python -m src.utils.notifier
"""

import html
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from src.utils.logger import get_logger

log = get_logger("notify")

BULAN_ID = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def _px(v: float) -> str:
    return f"{v:,.2f}"


def _pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def _pct_signed(v: float) -> str:
    return f"{v * 100:+.2f}%"


def _ts() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.day} {BULAN_ID[now.month - 1]} {now:%H:%M} UTC"


class TelegramNotifier:
    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.enabled = bool(self.bot_token and self.chat_id)
        if self.enabled:
            log.info("notifier Telegram ON (chat_id=%s)", self.chat_id)
        else:
            log.info("notifier Telegram OFF (silent mode)")

    @property
    def _api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def _send(self, text: str, silent: bool):
        """Kirim pesan; SEMUA kegagalan ditelan (cuma log warning)."""
        if not self.enabled:
            return
        try:
            payload = urllib.parse.urlencode(
                {
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_notification": "true" if silent else "false",
                }
            ).encode()
            req = urllib.request.Request(self._api_url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if not body.get("ok"):
                    log.warning("Telegram menolak pesan: %s", body)
        except Exception as e:
            log.warning("gagal kirim alert Telegram: %s", e)


    # ------------------------------------------------------------------
    # Event alert (layout "Indonesia, detail" -- disetujui 2026-08-29)
    # ------------------------------------------------------------------
    def notify_entry(
        self,
        symbol: str,
        signal: str,
        size: float,
        size_usd: float,
        price: float,
        sl: float | None,
        tp: float | None,
        confidence: float,
        equity: float | None,
        reason: str,
    ):
        """🟢 ENTRY -- silent."""
        equity_txt = f" {_usd(equity)}" if equity else ""
        text = (
            f"🟢 ENTRY {html.escape(signal)} {html.escape(symbol)}\n"
            f"\n"
            f"Harga  : <code>{_px(price)}</code>\n"
            f"Size   : <code>{size}</code> (~{_usd(size_usd)})\n"
            f"SL     : <code>{_px(sl) if sl else '-'}</code>\n"
            f"TP     : <code>{_px(tp) if tp else '-'}</code>\n"
            f"Conf   : <code>{confidence:.2f}</code> | Equity:{html.escape(equity_txt)}\n"
            f"Waktu  : {_ts()}\n"
            f"\n"
            f"{html.escape(reason)}"
        )
        self._send(text, silent=True)

    def notify_trailing(self, symbol: str, old_sl: float, new_sl: float, mid: float):
        """🔁 TRAILING -- silent."""
        text = (
            f"🔁 TRAILING {html.escape(symbol)}\n"
            f"SL <code>{_px(old_sl)}</code> → <code>{_px(new_sl)}</code> (mid <code>{_px(mid)}</code>)"
        )
        self._send(text, silent=True)

    def notify_closed(self, symbol: str, side: str, equity: float | None, daily_pnl_pct: float | None, kill_on: bool):
        """🏁 POSISI TERTUTUP (SL/TP terisi atau manual) -- silent."""
        equity_txt = _usd(equity) if equity else "-"
        pnl_txt = (
            f"{_pct_signed(daily_pnl_pct)} (kill switch: {'ON' if kill_on else 'OFF'})"
            if daily_pnl_pct is not None
            else "-"
        )
        text = (
            f"🏁 TERTUTUP {html.escape(symbol)} ({'LONG' if side == 'B' else 'SHORT'})\n"
            f"\n"
            f"Equity     : <code>{html.escape(equity_txt)}</code>\n"
            f"PnL harian : <code>{html.escape(pnl_txt)}</code>\n"
            f"Penyebab   : SL/TP terisi atau manual"
        )
        self._send(text, silent=True)

    def notify_kill_switch(self, daily_pnl_pct: float, limit_pct: float, equity: float, baseline: float):
        """🛑 KILL SWITCH AKTIF -- LOUD."""
        text = (
            f"🛑 KILL SWITCH AKTIF\n"
            f"\n"
            f"Rugi harian : <code>{_pct(daily_pnl_pct)}</code> (batas <code>-{_pct(limit_pct)}</code>)\n"
            f"Equity      : <code>{_usd(equity)}</code> (baseline <code>{_usd(baseline)}</code>)\n"
            f"\n"
            f"Entry baru DIBLOKIR sampai 00:00 UTC.\n"
            f"Posisi terbuka tetap dikelola (SL/TP/trailing jalan)."
        )
        self._send(text, silent=False)

    def notify_force_close(self, symbol: str, order_desc: str, detail: str = ""):
        """🔴 FORCE-CLOSE (proteksi gagal) -- LOUD."""
        extra = f"\n{html.escape(detail)}" if detail else ""
        text = (
            f"🔴 FORCE-CLOSE {html.escape(symbol)}\n"
            f"\n"
            f"Gagal pasang proteksi → posisi ditutup paksa.\n"
            f"Order: {html.escape(order_desc)}\n"
            f"⚠️ Cek manual di app — mungkin ada order menggantung.{extra}"
        )
        self._send(text, silent=False)


    def notify_force_close_trailing(self, symbol: str, last_sl: float | None, mid: float | None):
        """🔴 FORCE-CLOSE via trailing gagal (geser SL gagal + pulihkan gagal) -- LOUD."""
        sl_txt = _px(last_sl) if last_sl else "-"
        mid_txt = _px(mid) if mid else "-"
        text = (
            f"🔴 FORCE-CLOSE {html.escape(symbol)}\n"
            f"\n"
            f"Gagal geser SL + gagal pulihkan pair → ditutup paksa.\n"
            f"SL terakhir: <code>{html.escape(sl_txt)}</code> | Mid: <code>{html.escape(mid_txt)}</code>\n"
            f"⚠️ Cek manual di app."
        )
        self._send(text, silent=False)

    def notify_error(self, context: str, error: Exception):
        """❌ ERROR tak terduga (loop utama) -- LOUD."""
        text = (
            f"❌ ERROR {html.escape(context)}\n"
            f"\n"
            f"<code>{html.escape(str(error))}</code>\n"
            f"\n"
            f"Bot lanjut jalan — retry siklus berikutnya."
        )
        self._send(text, silent=False)

    def notify_heartbeat(
        self,
        date_utc: str,
        equity: float,
        yesterday_pnl_pct: float | None,
        positions: list,  # [(symbol, side, size, entry_px, sl, tp), ...]
        kill_on: bool,
        use_testnet: bool,
    ):
        """💓 HEARTBEAT harian (run pertama setelah rollover 00:00 UTC) -- silent."""
        yday_txt = _pct_signed(yesterday_pnl_pct) if yesterday_pnl_pct is not None else "-"
        if positions:
            lines = []
            for sym, side, size, entry_px, sl, tp in positions:
                dir_txt = "LONG" if side == "B" else "SHORT"
                size_txt = f"{size}" if size is not None else "-"
                sl_txt = _px(sl) if sl else "-"
                tp_txt = _px(tp) if tp else "-"
                lines.append(
                    f"{sym} {dir_txt} <code>{size_txt}</code> @ <code>{_px(entry_px)}</code> "
                    f"(SL <code>{sl_txt}</code> | TP <code>{tp_txt}</code>)"
                )
            positions_txt = "\n".join(lines)
        else:
            positions_txt = "Tidak ada"
        text = (
            f"💓 HEARTBEAT — {html.escape(date_utc)}\n"
            f"\n"
            f"Status : Hidup ✅ | {'Testnet' if use_testnet else 'MAINNET'}\n"
            f"Equity : <code>{_usd(equity)}</code>\n"
            f"Kemarin: <code>{yday_txt}</code>\n"
            f"Posisi :\n{positions_txt}\n"
            f"Kill   : {'ON 🛑' if kill_on else 'OFF'}"
        )
        self._send(text, silent=True)


if __name__ == "__main__":
    # smoke test manual: kirim semua jenis alert sekali (butuh .env terisi)
    from src.config import Config

    cfg = Config.from_env()
    n = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
    if not n.enabled:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID kosong -> tidak ada yang dikirim.")
    else:
        n.notify_entry("BTC", "SELL", 0.00628, 490.18, 78054.12, 79669.31, 75687.28, 0.65, 940.0, "FOLLOW SELL: close<EMA, ADX=25.2, RSI=37.0")
        n.notify_trailing("BTC", 79200.0, 79669.31, 78100.0)
        n.notify_closed("BTC", "S", 948.0, -0.012, False)
        n.notify_kill_switch(-0.053, 0.05, 947.0, 1000.0)
        n.notify_force_close("BTC", "SELL 0.00628 BTC (~$490)")
        n.notify_force_close_trailing("BTC", 79669.31, 78100.0)
        n.notify_error("di run_once", RuntimeError("KeyError: 'assetPositions'"))
        n.notify_heartbeat("2026-08-30", 1012.50, 0.0125, [("BTC", "S", 0.00628, 78054.12, 79200.0, 75687.28)], False, True)
        print("Semua alert terkirim — cek Telegram kamu.")
