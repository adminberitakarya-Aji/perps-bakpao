"""
Backtest engine sederhana, single-position-at-a-time.

Asumsi & simplifikasi (penting dibaca sebelum percaya hasilnya):
- Entry price = harga close candle saat sinyal muncul (bukan harga tick
  presisi -- di real market, entry price bisa sedikit berbeda karena
  slippage)
- SL/TP dicek terhadap high/low candle berikutnya (kalau dalam satu candle
  SL dan TP dua-duanya "tersentuh", TP diasumsikan tidak tercapai duluan --
  ini asumsi konservatif, bukan yang paling akurat)
- Fee dihitung sebagai % dari notional, di entry dan exit
- Funding rate dihitung sebagai % notional per bar yang posisi terbuka,
  selalu sebagai BIAYA (konservatif; realita funding itu signed -- long
  bayar saat positif, short justru terima). Default 0.000009 = mean
  |funding rate| BTC per jam terukur dari fundingHistory Hyperliquid,
  window Mar-Agu 2026 (bar di sini = 1 jam).
- Tidak ada partial fill, tidak ada model liquidation eksplisit -- kalau SL
  ketembus lebih dalam dari yang dihitung, hasil backtest ini masih optimis
  dibanding kondisi real leverage tinggi
"""

from dataclasses import dataclass, field

from src.strategy.base import Strategy, MarketSnapshot, Signal
from src.risk.manager import RiskManager

MAX_LOOKBACK = 560  # bar; >= 500 utk fitur regime ML + 50 vol + 10 buffer


@dataclass
class BacktestConfig:
    initial_equity: float = 1000.0
    leverage: float = 1.0
    fee_rate: float = 0.00035        # taker fee Hyperliquid perps (~0.035%), cek rate terbaru sebelum percaya angka ini
    funding_rate_per_bar: float = 0.000009  # per bar (1 jam): mean |rate| BTC terukur Mar-Agu 2026; stress test: 0.0000125
    min_bars: int = 60               # warm-up minimum sebelum strategi mulai dievaluasi


@dataclass
class Trade:
    entry_index: int
    exit_index: int
    signal: Signal
    entry_price: float
    exit_price: float
    size_usd: float
    pnl_usd: float
    exit_reason: str  # "SL" | "TP" | "end_of_data"
    trailing_triggered: bool = False


@dataclass
class BacktestResult:
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)  # [(bar_index, equity), ...]
    final_equity: float = 0.0

    def summary(self) -> dict:
        if not self.trades:
            return {"total_trades": 0, "note": "tidak ada trade sama sekali -- cek parameter strategi/risk"}

        wins = [t for t in self.trades if t.pnl_usd > 0]
        losses = [t for t in self.trades if t.pnl_usd <= 0]

        gross_profit = sum(t.pnl_usd for t in wins)
        gross_loss = abs(sum(t.pnl_usd for t in losses))

        equity_values = [e for _, e in self.equity_curve]
        peak = equity_values[0]
        max_drawdown_pct = 0.0
        for e in equity_values:
            peak = max(peak, e)
            dd = (peak - e) / peak if peak > 0 else 0
            max_drawdown_pct = max(max_drawdown_pct, dd)

        return {
            "total_trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(self.trades) * 100, 1),
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
            "total_return_pct": round((self.final_equity - equity_values[0]) / equity_values[0] * 100, 2),
            "max_drawdown_pct": round(max_drawdown_pct * 100, 2),
            "final_equity": round(self.final_equity, 2),
            "avg_trade_pnl_usd": round(sum(t.pnl_usd for t in self.trades) / len(self.trades), 2),
            "trades_with_trailing": sum(1 for t in self.trades if t.trailing_triggered),
        }


def run_backtest(
    candles: list,
    strategy: Strategy,
    risk_manager: RiskManager,
    config: BacktestConfig,
    signal_filter=None,  # callable(window, signal) -> bool; None = tanpa filter
) -> BacktestResult:
    result = BacktestResult()
    equity = config.initial_equity
    result.equity_curve.append((config.min_bars, equity))

    open_position = None  # dict: signal, entry_price, sl, tp, size_usd, entry_index

    for i in range(config.min_bars, len(candles)):
        candle = candles[i]
        high, low = float(candle["h"]), float(candle["l"])

        # --- 0. Update umur posisi terbuka (bar) ---
        # Dipakai _compute_pnl untuk funding: posisi yang entry di bar
        # `entry_index` dan exit di bar `i` telah menahan posisi selama
        # (i - entry_index) bar. Di-set SEBELUM cek exit supaya bar exit
        # ikut terhitung sebagai jam funding.
        if open_position is not None:
            open_position["bars_held"] = i - open_position["entry_index"]

        # --- 1. Cek exit posisi terbuka (SL/TP) di candle ini ---
        if open_position is not None:
            hit_sl = hit_tp = False
            if open_position["signal"] == Signal.BUY:
                hit_sl = low <= open_position["sl"]
                hit_tp = high >= open_position["tp"]
            else:  # SELL
                hit_sl = high >= open_position["sl"]
                hit_tp = low <= open_position["tp"]

            # asumsi konservatif: kalau dua-duanya tersentuh di candle yang
            # sama, anggap SL duluan yang kena
            if hit_sl or hit_tp:
                exit_price = open_position["sl"] if hit_sl else open_position["tp"]
                exit_reason = "SL" if hit_sl else "TP"

                pnl = _compute_pnl(open_position, exit_price, config)
                equity += pnl

                result.trades.append(Trade(
                    entry_index=open_position["entry_index"],
                    exit_index=i,
                    signal=open_position["signal"],
                    entry_price=open_position["entry_price"],
                    exit_price=exit_price,
                    size_usd=open_position["size_usd"],
                    pnl_usd=pnl,
                    exit_reason=exit_reason,
                    trailing_triggered=open_position.get("trailing_triggered", False),
                ))
                open_position = None

        # --- 1b. Kalau posisi masih terbuka (tidak exit di step 1), update trailing SL ---
        # Pakai high (BUY) / low (SELL) candle INI sebagai "harga terbaik tercapai",
        # tapi SL baru ini baru berlaku untuk pengecekan exit di candle SELANJUTNYA
        # (bukan candle yang sama) -- supaya tidak look-ahead bias.
        if open_position is not None:
            best_price = high if open_position["signal"] == Signal.BUY else low
            new_sl = risk_manager.compute_trailing_sl(
                signal=open_position["signal"],
                entry_price=open_position["entry_price"],
                current_price=best_price,
                current_sl=open_position["sl"],
                entry_atr=open_position["atr"],
            )
            if new_sl is not None:
                open_position["sl"] = new_sl
                open_position["trailing_triggered"] = True

        # --- 2. Kalau tidak ada posisi terbuka, cek sinyal baru ---
        if open_position is None:
            # Cap lookback window supaya recompute indikator tidak O(n) tiap bar
            # (jadi O(n * MAX_LOOKBACK) alih-alih O(n^2) untuk seluruh backtest).
            # 300 bar >> cukup untuk EMA-50/ADX-14/RSI-14 konvergen penuh --
            # ini optimasi performa murni, tidak mengubah hasil sinyal.
            window_start = max(0, i + 1 - MAX_LOOKBACK)
            window = candles[window_start: i + 1]
            snapshot = MarketSnapshot(symbol="BACKTEST", mid_price=float(candle["c"]), candles=window)
            sig_result = strategy.generate_signal(snapshot)

            if sig_result.signal != Signal.HOLD and (
                signal_filter is None or signal_filter(window, sig_result.signal)
            ):
                # sl_distance_pct supaya sizing risk-based (konsisten dgn live)
                atr = _get_last_atr(strategy, window)
                sl_distance_pct = None
                if atr is not None and atr > 0 and float(candle["c"]) > 0:
                    sl_distance_pct = (atr * risk_manager.limits.atr_sl_mult) / float(candle["c"])
                size_usd = risk_manager.check_and_size(
                    equity, sig_result.signal, sig_result.confidence, sl_distance_pct=sl_distance_pct
                )
                if size_usd > 0 and atr is not None and atr > 0:
                    entry_price = float(candle["c"])
                    sl, tp = risk_manager.compute_sl_tp(sig_result.signal, entry_price, atr)
                    open_position = {
                        "signal": sig_result.signal,
                        "entry_price": entry_price,
                        "sl": sl,
                        "tp": tp,
                        "atr": atr,
                        "size_usd": size_usd * config.leverage,
                        "entry_index": i,
                        "bars_held": 0,
                        "trailing_triggered": False,
                    }

        result.equity_curve.append((i, equity))

    # tutup posisi yang masih terbuka di akhir data (mark-to-market)
    if open_position is not None:
        last_close = float(candles[-1]["c"])
        # posisi yang masih terbuka di akhir data: umur dihitung eksplisit
        # (kalau entry tepat di bar terakhir, loop di atas belum sempat
        # meng-set bars_held)
        open_position["bars_held"] = (len(candles) - 1) - open_position["entry_index"]
        pnl = _compute_pnl(open_position, last_close, config)
        equity += pnl
        result.trades.append(Trade(
            entry_index=open_position["entry_index"],
            exit_index=len(candles) - 1,
            signal=open_position["signal"],
            entry_price=open_position["entry_price"],
            exit_price=last_close,
            size_usd=open_position["size_usd"],
            pnl_usd=pnl,
            exit_reason="end_of_data",
            trailing_triggered=open_position.get("trailing_triggered", False),
        ))

    result.final_equity = equity
    return result


def _compute_pnl(position: dict, exit_price: float, config: BacktestConfig) -> float:
    entry_price = position["entry_price"]
    size_usd = position["size_usd"]
    direction = 1 if position["signal"] == Signal.BUY else -1

    price_change_pct = (exit_price - entry_price) / entry_price
    gross_pnl = size_usd * price_change_pct * direction

    entry_fee = size_usd * config.fee_rate
    exit_fee = size_usd * config.fee_rate
    bars_held = max(0, position.get("bars_held", 1))
    funding_cost = size_usd * config.funding_rate_per_bar * bars_held

    return gross_pnl - entry_fee - exit_fee - funding_cost


def _get_last_atr(strategy: Strategy, window: list):
    """Helper: hitung ulang ATR dari window candle untuk dipakai risk_manager.
    Cara ini sedikit tidak efisien (recompute indikator), tapi menjaga
    backtest engine tidak coupling ketat ke strategi tertentu."""
    if not hasattr(strategy, "_to_df") or not hasattr(strategy, "_compute_indicators"):
        return None
    df = strategy._to_df(window)
    df = strategy._compute_indicators(df)
    last_atr = df["atr"].iloc[-1]
    return float(last_atr) if last_atr == last_atr else None  # NaN check
