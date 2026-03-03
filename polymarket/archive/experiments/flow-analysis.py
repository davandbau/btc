#!/usr/local/bin/python3.12
"""
Analyze flow correlation data.
Usage: python3 flow-analysis.py flow-data-*.csv
"""

import csv, sys, os

def analyze(filepath):
    rows = []
    with open(filepath) as f:
        for r in csv.DictReader(f):
            # Skip incomplete rows
            if not r.get("delta_10s") or not r.get("delta_30s") or not r.get("delta_60s"):
                continue
            rows.append(r)

    if not rows:
        print("No complete rows found.")
        return

    print(f"\n{'='*70}")
    print(f"Flow Correlation Analysis — {len(rows)} samples")
    print(f"File: {os.path.basename(filepath)}")
    print(f"{'='*70}\n")

    # Bucket by flow strength
    buckets = {
        "Strong sell (buy <30%)": [],
        "Moderate sell (30-45%)": [],
        "Neutral (45-55%)": [],
        "Moderate buy (55-70%)": [],
        "Strong buy (buy >70%)": [],
    }

    for r in rows:
        bp = float(r["buy_pct"])
        if bp < 30:
            buckets["Strong sell (buy <30%)"].append(r)
        elif bp < 45:
            buckets["Moderate sell (30-45%)"].append(r)
        elif bp <= 55:
            buckets["Neutral (45-55%)"].append(r)
        elif bp <= 70:
            buckets["Moderate buy (55-70%)"].append(r)
        else:
            buckets["Strong buy (buy >70%)"].append(r)

    print(f"{'Flow Signal':<30} {'N':>5} {'Avg Δ10s':>10} {'Avg Δ30s':>10} {'Avg Δ60s':>10} {'Hit% 10s':>10} {'Hit% 60s':>10}")
    print("-" * 95)

    for label, bucket in buckets.items():
        if not bucket:
            print(f"{label:<30} {'—':>5}")
            continue

        n = len(bucket)
        avg_10 = sum(float(r["delta_10s"]) for r in bucket) / n
        avg_30 = sum(float(r["delta_30s"]) for r in bucket) / n
        avg_60 = sum(float(r["delta_60s"]) for r in bucket) / n

        # "Hit rate" = does the flow direction predict price direction?
        is_buy_signal = "buy" in label.lower() and "sell" not in label.lower()
        is_sell_signal = "sell" in label.lower()

        if is_buy_signal:
            hit_10 = sum(1 for r in bucket if float(r["delta_10s"]) > 0) / n * 100
            hit_60 = sum(1 for r in bucket if float(r["delta_60s"]) > 0) / n * 100
        elif is_sell_signal:
            hit_10 = sum(1 for r in bucket if float(r["delta_10s"]) < 0) / n * 100
            hit_60 = sum(1 for r in bucket if float(r["delta_60s"]) < 0) / n * 100
        else:
            hit_10 = hit_60 = 0

        print(f"{label:<30} {n:>5} {avg_10:>+10.2f} {avg_30:>+10.2f} {avg_60:>+10.2f} {hit_10:>9.1f}% {hit_60:>9.1f}%")

    # Overall correlation (simple Pearson)
    print(f"\n{'='*70}")
    print("Correlation: net_flow_btc vs price delta")
    print(f"{'='*70}\n")

    for horizon in ["10s", "30s", "60s"]:
        flows = [float(r["net_flow_btc"]) for r in rows]
        deltas = [float(r[f"delta_{horizon}"]) for r in rows]
        n = len(flows)
        mean_f = sum(flows) / n
        mean_d = sum(deltas) / n
        cov = sum((f - mean_f) * (d - mean_d) for f, d in zip(flows, deltas)) / n
        std_f = (sum((f - mean_f)**2 for f in flows) / n) ** 0.5
        std_d = (sum((d - mean_d)**2 for d in deltas) / n) ** 0.5
        corr = cov / (std_f * std_d) if std_f > 0 and std_d > 0 else 0

        strength = "strong" if abs(corr) > 0.5 else "moderate" if abs(corr) > 0.3 else "weak" if abs(corr) > 0.1 else "none"
        print(f"  {horizon}: r = {corr:+.4f} ({strength})")

    # Volume analysis
    print(f"\n{'='*70}")
    print("Does higher volume = stronger signal?")
    print(f"{'='*70}\n")

    vols = sorted(rows, key=lambda r: float(r["total_vol_btc"]))
    low_vol = vols[:len(vols)//3]
    mid_vol = vols[len(vols)//3:2*len(vols)//3]
    high_vol = vols[2*len(vols)//3:]

    for label, subset in [("Low volume", low_vol), ("Mid volume", mid_vol), ("High volume", high_vol)]:
        if not subset:
            continue
        n = len(subset)
        avg_vol = sum(float(r["total_vol_btc"]) for r in subset) / n
        avg_abs_delta = sum(abs(float(r["delta_60s"])) for r in subset) / n

        # Correlation within this volume tier
        flows = [float(r["net_flow_btc"]) for r in subset]
        deltas = [float(r["delta_60s"]) for r in subset]
        mean_f = sum(flows) / n
        mean_d = sum(deltas) / n
        cov = sum((f - mean_f) * (d - mean_d) for f, d in zip(flows, deltas)) / n
        std_f = (sum((f - mean_f)**2 for f in flows) / n) ** 0.5
        std_d = (sum((d - mean_d)**2 for d in deltas) / n) ** 0.5
        corr = cov / (std_f * std_d) if std_f > 0 and std_d > 0 else 0

        print(f"  {label:<15} n={n:>4} | avg vol={avg_vol:.3f} BTC | avg |Δ60s|=${avg_abs_delta:.2f} | flow→price r={corr:+.4f}")

    print(f"\n{'='*70}")
    print(f"Summary: If 60s correlation > +0.3, trade flow is a useful predictor.")
    print(f"If < 0.1, it's noise — remove from agent prompt.")
    print(f"{'='*70}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        # Find latest file
        import glob
        files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "flow-data-*.csv")))
        if files:
            analyze(files[-1])
        else:
            print("Usage: python3 flow-analysis.py flow-data-*.csv")
    else:
        analyze(sys.argv[1])
