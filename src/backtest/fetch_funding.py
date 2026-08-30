"""
Fetch riwayat funding rate BTC dari Hyperliquid (info fundingHistory,
paginasi maju 500 rekam/request, retensi penuh sejak 2023) untuk fitur ML.

    python -m src.backtest.fetch_funding --coin BTC --days 1100

Output: data/BTC_funding.csv (time_ms, funding_rate) -- rate per jam.
"""

import argparse
import csv
import time

import requests

URL = "https://api.hyperliquid.xyz/info"


def fetch_funding(coin: str, days: int) -> list:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    out, cursor = [], start_ms
    while cursor < end_ms:
        r = requests.post(URL, json={
            "type": "fundingHistory", "coin": coin,
            "startTime": cursor, "endTime": end_ms,
        }, timeout=20)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for row in rows:
            out.append((int(row["time"]), float(row["fundingRate"])))
        last_t = int(rows[-1]["time"])
        if last_t <= cursor:
            break
        cursor = last_t + 1
        if len(rows) < 500:
            break
        time.sleep(0.1)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--days", type=int, default=1100)
    args = ap.parse_args()

    rows = fetch_funding(args.coin, args.days)
    if not rows:
        raise SystemExit("fundingHistory kosong")

    out_path = f"data/{args.coin}_funding.csv"
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "funding_rate"])
        w.writerows(rows)

    rates = [r for _, r in rows]
    print(f"{len(rows)} rekam -> {out_path}")
    print(f"rentang : {time.strftime('%Y-%m-%d', time.gmtime(rows[0][0]/1000))} "
          f"s/d {time.strftime('%Y-%m-%d', time.gmtime(rows[-1][0]/1000))} UTC")
    print(f"mean    : {sum(rates)/len(rates):+.6f} | mean|rate|: "
          f"{sum(abs(r) for r in rates)/len(rates):.6f}")


if __name__ == "__main__":
    main()
