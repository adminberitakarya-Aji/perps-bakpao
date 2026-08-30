"""
Wrapper tipis di atas hyperliquid-python-sdk.
Menyatukan Info (baca data publik) dan Exchange (kirim order, butuh signing)
jadi satu titik akses, supaya modul lain tidak perlu tahu detail SDK.
"""

import time

import eth_account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange

from src.config import Config
from src.utils.logger import get_logger

log = get_logger("client")


def round_px(px: float, sz_decimals: int, is_spot: bool = False) -> float:
    """Bulatkan harga sesuai aturan presisi Hyperliquid: maks (6 - szDecimals)
    desimal untuk perps (8 - szDecimals untuk spot) dan maks 5 significant
    figures.

    `exchange.bulk_orders` SDK TIDAK membulatkan harga otomatis (berbeda dari
    market_open/market_close yang memakai _slippage_price), jadi trigger order
    SL/TP yang dikirim manual WAJIB dibulatkan lewat fungsi ini -- kalau tidak
    (mis. BTC 2 desimal = 7 sig-fig), exchange menolak order.
    Pola rounding identik dengan hyperliquid.exchange._slippage_price.
    """
    max_decimals = (8 if is_spot else 6) - sz_decimals
    return round(float(f"{px:.5g}"), max_decimals)


def round_sz(sz: float, sz_decimals: int) -> float:
    """Bulatkan UKURAN order (bukan harga) ke szDecimals asli aset.

    Beda dari round_px: ukuran tidak punya batas 5 significant figures,
    cuma dibatasi jumlah desimal (szDecimals) -- tapi tetap WAJIB dibulatkan,
    karena hardcode desimal (mis. selalu 5) salah untuk aset yang szDecimals
    aslinya lebih kecil (banyak altcoin < 5) -> exchange menolak order,
    persis pola kegagalan yang sama dengan harga yang tidak di-round_px.
    """
    return round(sz, sz_decimals)


class OrderRejectedError(RuntimeError):
    """Exchange MENOLAK order (status err / per-order error).

    SDK hyperliquid TIDAK me-raise exception untuk order yang ditolak -- ia
    mengembalikan dict {'status': 'err', ...} atau status ok dengan elemen
    'error' per order. Tanpa validasi eksplisit, order gagal terlihat sukses
    dan engine mencatat state posisi fantasi.
    """


def validate_order_result(result, context: str = "order"):
    """Validasi respons order SDK; raise OrderRejectedError kalau ditolak.

    Bentuk respons:
      {'status': 'err', 'response': '...'}                        -> ditolak
      {'status': 'ok', 'response': {'data': {'statuses': [
          {'resting': {...}} | {'filled': {...}} | {'error': '...'}
      ]}}}                                                        -> cek per order
    Return result apa adanya kalau valid.
    """
    if not isinstance(result, dict):
        raise OrderRejectedError(f"{context}: respons tidak dikenal: {result!r}")
    status = result.get("status")
    if status == "err":
        raise OrderRejectedError(f"{context} ditolak: {result.get('response')}")
    if status == "ok":
        statuses = (
            result.get("response", {}).get("data", {}).get("statuses", [])
        )
        for st in statuses:
            if isinstance(st, dict) and "error" in st:
                raise OrderRejectedError(f"{context} ditolak: {st['error']}")
            elif isinstance(st, str) and "error" in st.lower():
                raise OrderRejectedError(f"{context} ditolak: {st}")
    return result


class ProtectionError(RuntimeError):
    """Entry terisi tapi SL/TP gagal dipasang -> posisi SUDAH ditutup paksa.

    Dilempar ke executor supaya alert force-close terkirim (jalur engine
    sendiri tidak tahu proteksi gagal).
    """


class HyperliquidClient:
    def __init__(self, config: Config):
        self.config = config
        self.wallet = eth_account.Account.from_key(config.private_key)

        # Info: query market data, posisi, order book, funding rate, dll.
        self.info = Info(config.api_url, skip_ws=True)

        # Exchange: kirim order, cancel, adjust leverage. Butuh wallet untuk sign.
        self.exchange = Exchange(
            self.wallet,
            config.api_url,
            account_address=config.account_address,
        )

    def get_mid_price(self, symbol: str) -> float:
        mids = self.info.all_mids()
        return float(mids[symbol])

    def get_candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
        """interval contoh: '1m', '5m', '15m', '1h', '4h', '1d'"""
        return self.info.candles_snapshot(symbol, interval, start_ms, end_ms)

    def get_account_state(self) -> dict:
        return self.info.user_state(self.config.account_address)

    def get_open_positions(self) -> list:
        state = self.get_account_state()
        return state.get("assetPositions", [])

    def place_market_order(
        self,
        symbol: str,
        is_buy: bool,
        size: float,
        reduce_only: bool = False,
        sl: float | None = None,
        tp: float | None = None,
    ):
        """Order market via helper SDK.

        Kalau sl/tp diberikan (dan bukan reduce_only): setelah entry terisi,
        pasang pasangan SL/TP (normalTpsl) sesuai ukuran posisi AKTUAL.
        Kalau proteksi gagal total -> posisi ditutup paksa: engine ini tidak
        pernah meninggalkan posisi telanjang tanpa stop-loss.
        """
        if reduce_only:
            return self.exchange.market_close(symbol)

        result = validate_order_result(
            self.exchange.market_open(symbol, is_buy, size), f"entry {symbol}"
        )

        if sl is None and tp is None:
            return result

        pos = self.get_position_retry(symbol)
        if pos is None:
            raise ProtectionError(
                f"posisi {symbol} tidak terdeteksi setelah entry -- SL/TP gagal dipasang"
            )

        close_is_buy = pos["side"] == "S"  # menutup posisi = arah berlawanan
        try:
            self.place_tpsl_pair(symbol, close_is_buy, abs(pos["szi"]), sl, tp)
        except Exception as e:
            log.error("GAGAL pasang SL/TP (%s) -> tutup paksa posisi %s", e, symbol)
            try:
                self.cancel_all_trigger_orders(symbol)
                self.exchange.market_close(symbol)
            except Exception as e2:
                log.critical("gagal tutup paksa %s: %s -- PERIKSA MANUAL!", symbol, e2)
            raise
        return result

    def get_position_retry(self, symbol: str, retries: int = 3, delay_s: float = 1.0) -> dict | None:
        """Posisi kadang belum terlihat sesaat setelah fill; retry ringan."""
        for _ in range(retries):
            pos = self.get_position(symbol)
            if pos is not None:
                return pos
            time.sleep(delay_s)
        return self.get_position(symbol)

    def _round_px(self, symbol: str, px: float) -> float:
        """round_px() dengan szDecimals dari metadata exchange yang sudah
        di-fetch Info di __init__ (butuh koneksi; fungsi murni round_px
        dipisah supaya bisa dites tanpa jaringan)."""
        asset = self.info.coin_to_asset[symbol]
        sz_decimals = self.info.asset_to_sz_decimals[asset]
        return round_px(px, sz_decimals, is_spot=asset >= 10_000)

    def round_size(self, symbol: str, size: float) -> float:
        """Bulatkan UKURAN order ke szDecimals asli aset (lihat round_sz()).

        Dipakai executor.py sebelum kirim entry order -- size dari
        size_usd/price bisa berdesimal berapapun, dan szDecimals BEDA per
        aset (BTC/ETH kebetulan >= 5, tapi banyak aset lain lebih kecil ->
        hardcode 5 desimal akan ditolak exchange untuk aset seperti itu)."""
        asset = self.info.coin_to_asset[symbol]
        sz_decimals = self.info.asset_to_sz_decimals[asset]
        return round_sz(size, sz_decimals)

    def place_tpsl_pair(self, symbol: str, close_is_buy: bool, size: float, sl: float, tp: float | None = None):
        """Pasang SL (+TP) reduce-only, grouping normalTpsl: kalau salah satu
        trigger terisi, satunya otomatis di-cancel oleh exchange.

        Harga dibulatkan dulu ke presisi yang diterima exchange (round_px):
        bulk_orders tidak membulatkan otomatis, harga mentah dengan desimal
        berlebih akan DITOLAK -> ProtectionError -> force-close percuma.

        close_is_buy = arah order PENUTUP (posisi long -> close_is_buy=False).
        """
        sl = self._round_px(symbol, sl)
        if tp is not None and tp > 0:
            tp = self._round_px(symbol, tp)
        orders = [
            {
                "coin": symbol,
                "is_buy": close_is_buy,
                "sz": size,
                "limit_px": sl,
                "order_type": {"trigger": {"triggerPx": sl, "isMarket": True, "tpsl": "sl"}},
                "reduce_only": True,
            }
        ]
        grouping = "na"
        if tp is not None and tp > 0:
            orders.append(
                {
                    "coin": symbol,
                    "is_buy": close_is_buy,
                    "sz": size,
                    "limit_px": tp,
                    "order_type": {"trigger": {"triggerPx": tp, "isMarket": True, "tpsl": "tp"}},
                    "reduce_only": True,
                }
            )
            grouping = "normalTpsl"
        result = validate_order_result(
            self.exchange.bulk_orders(orders, grouping=grouping),
            f"SL/TP {symbol}",
        )
        return result

    def modify_sl_trigger(self, symbol: str, oid: int, close_is_buy: bool, size: float, new_sl: float) -> dict:
        """Geser trigger SL yang SUDAH terpasang via modify (fix P2-9).

        modify_order mengubah order in-place: tidak ada jeda cancel->replace,
        jadi tidak ada momen posisi telanjang tanpa SL. Harga WAJIB dibulatkan
        _round_px dulu (bulk_modify juga tidak membulatkan otomatis).
        """
        new_sl = self._round_px(symbol, new_sl)
        order_type = {"trigger": {"triggerPx": new_sl, "isMarket": True, "tpsl": "sl"}}
        return validate_order_result(
            self.exchange.modify_order(
                int(oid), symbol, close_is_buy, size, new_sl, order_type, reduce_only=True
            ),
            f"modify SL {symbol}",
        )

    def market_close_position(self, symbol: str) -> dict:
        """Tutup posisi market (reduce-only) via SDK."""
        return validate_order_result(
            self.exchange.market_close(symbol), f"market_close {symbol}"
        )

    def cancel_all_orders(self, symbol: str):
        open_orders = self.info.open_orders(self.config.account_address)
        for order in open_orders:
            if order["coin"] == symbol:
                self.exchange.cancel(symbol, order["oid"])

    def get_funding_rate(self, coin: str) -> float | None:
        """Funding rate terakhir (per jam) -- fitur ML & estimasi biaya."""
        try:
            rows = self.info.funding_history(
                coin, int(time.time() * 1000) - 3_600_000, int(time.time() * 1000)
            )
            return float(rows[-1]["fundingRate"]) if rows else None
        except Exception:
            return None

    def get_position(self, symbol: str) -> dict | None:
        """Posisi terbuka satu simbol, atau None.

        Return {"szi": float bertanda (+long/-short), "entryPx": float, "side": "B"/"S"}.
        Struktur assetPositions: [{"position": {"coin": ..., "szi": ..., ...}}, ...]
        """
        for p in self.get_open_positions():
            pos = p.get("position", {})
            if pos.get("coin") == symbol:
                szi = float(pos.get("szi") or 0)
                if szi == 0:
                    return None
                return {
                    "szi": szi,
                    "entryPx": float(pos.get("entryPx") or 0),
                    "side": "B" if szi > 0 else "S",
                }
        return None

    def get_trigger_orders(self, symbol: str) -> list:
        """Trigger order aktif (SL/TP) satu simbol, dari frontendOpenOrders."""
        orders = self.info.frontend_open_orders(self.config.account_address)
        return [o for o in (orders or []) if o.get("coin") == symbol and o.get("isTrigger")]

    def cancel_all_trigger_orders(self, symbol: str) -> int:
        """Cancel semua trigger order aktif satu simbol. Return jumlah yang dibatalkan."""
        n = 0
        for o in self.get_trigger_orders(symbol):
            try:
                self.exchange.cancel(symbol, int(o["oid"]))
                n += 1
            except Exception as e:
                log.warning("gagal cancel trigger oid=%s: %s", o.get("oid"), e)
        return n


if __name__ == "__main__":
    # smoke test manual: pastikan koneksi ke testnet jalan
    from src.config import Config

    config = Config.from_env()
    client = HyperliquidClient(config)
    print(f"Terhubung ke {'TESTNET' if config.use_testnet else 'MAINNET'}")
    print(f"Mid price BTC: {client.get_mid_price('BTC')}")
