import os
import asyncio
import json
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

DEXSCREENER_API_KEY = os.getenv("DEXSCREENER_API_KEY")

# Base URL for all Solana pairs on DexScreener
DEXSCREENER_SOLANA_URL = "https://api.dexscreener.com/latest/dex/pairs/solana"

# Thresholds for volume / wash-trading analysis
VOLUME_INCREASE_THRESHOLD_PCT = float(os.getenv("VOLUME_INCREASE_THRESHOLD_PCT", "50.0"))
WASH_TRADING_RATIO_THRESHOLD = float(os.getenv("WASH_TRADING_RATIO_THRESHOLD", "10.0"))


async def fetch_solana_pairs(client: httpx.AsyncClient) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch all Solana pairs from DexScreener.
    Returns list of pairs or None on error.
    """
    headers: Dict[str, str] = {}
    if DEXSCREENER_API_KEY:
        # Reserved for when DexScreener requires authentication.
        headers["Authorization"] = f"Bearer {DEXSCREENER_API_KEY}"

    try:
        resp = await client.get(DEXSCREENER_SOLANA_URL, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        print(f"[HTTP ERROR] Status {e.response.status_code}: {e}")
        return None
    except httpx.RequestError as e:
        print(f"[REQUEST ERROR] {e}")
        return None

    # Handle malformed JSON explicitly
    try:
        data = json.loads(resp.text)
    except json.JSONDecodeError as e:
        print(f"[JSON ERROR] Malformed JSON from DexScreener: {e}")
        return None

    if not isinstance(data, dict):
        print("[DATA ERROR] Unexpected top-level JSON type (expected object).")
        return None

    pairs = data.get("pairs")
    if not isinstance(pairs, list):
        print("[DATA ERROR] 'pairs' key missing or not a list in response.")
        return None

    return pairs


def analyze_pair(pair: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Analyze a single pair:
      - Compute % volume increase in last hour (h1 vs h2).
      - Compute Volume/Liquidity ratio.
      - Return analysis dict if it passes filters, otherwise None.
    """
    try:
        volume = pair.get("volume", {})
        liquidity = pair.get("liquidity", {})

        # DexScreener commonly has h1, h6, h24; some deployments also expose h2 (previous hour).
        # Here we assume h1 (last hour) and h2 (previous hour) exist.
        vol_h1 = float(volume.get("h1") or 0.0)  # last hour
        vol_h2 = float(volume.get("h2") or 0.0)  # previous hour

        # If we don't have a previous-hour baseline, skip
        if vol_h2 <= 0:
            return None

        liquidity_usd = float(liquidity.get("usd") or 0.0)
        if liquidity_usd <= 0:
            return None

        volume_increase_pct = ((vol_h1 - vol_h2) / vol_h2) * 100.0
        if volume_increase_pct <= VOLUME_INCREASE_THRESHOLD_PCT:
            return None

        volume_liquidity_ratio = vol_h1 / liquidity_usd
        high_wash_trading_risk = volume_liquidity_ratio > WASH_TRADING_RATIO_THRESHOLD

        base_symbol = pair.get("baseToken", {}).get("symbol", "UNKNOWN")
        quote_symbol = pair.get("quoteToken", {}).get("symbol", "UNKNOWN")
        base_address = pair.get("baseToken", {}).get("address")
        pair_address = pair.get("pairAddress", "N/A")

        return {
            "pair": f"{base_symbol}/{quote_symbol}",
            "pair_address": pair_address,
            "base_token_address": base_address,
            "volume_h1": vol_h1,
            "volume_h2": vol_h2,
            "volume_increase_pct": volume_increase_pct,
            "liquidity_usd": liquidity_usd,
            "volume_liquidity_ratio": volume_liquidity_ratio,
            "wash_trading_risk": "High Wash-Trading Risk" if high_wash_trading_risk else "Normal",
        }

    except (TypeError, ValueError) as e:
        # Any casting/parsing error means this pair is malformed – skip it safely
        print(f"[PAIR ERROR] Failed to process pair: {e}")
        return None


async def analyze_solana_pairs_once() -> None:
    """
    One-shot analysis: fetch current Solana pairs and print those that
    match the filters. Useful for manual CLI runs.
    """
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        pairs = await fetch_solana_pairs(client)

    if not pairs:
        print("No pairs fetched; exiting.")
        return

    print(f"Fetched {len(pairs)} pairs from Solana. Analyzing...\n")

    matches: List[Dict[str, Any]] = []
    for pair in pairs:
        result = analyze_pair(pair)
        if result:
            matches.append(result)

    if not matches:
        print("No pairs met the volume increase and safety criteria.")
        return

    print(f"Found {len(matches)} pairs with >{VOLUME_INCREASE_THRESHOLD_PCT}% 1h volume increase.\n")

    for m in matches:
        print(f"Pair: {m['pair']} ({m['pair_address']})")
        print(f"  1h Volume (h1):       ${m['volume_h1']:.2f}")
        print(f"  Prev Hour Volume(h2): ${m['volume_h2']:.2f}")
        print(f"  Volume Increase:      {m['volume_increase_pct']:.2f}%")
        print(f"  Liquidity (USD):      ${m['liquidity_usd']:.2f}")
        print(f"  Volume/Liquidity:     {m['volume_liquidity_ratio']:.2f}")
        print(f"  Risk:                 {m['wash_trading_risk']}")
        print("-" * 60)


if __name__ == "__main__":
    asyncio.run(analyze_solana_pairs_once())


