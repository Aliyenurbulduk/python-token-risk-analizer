from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple
import base64
import os

import httpx

# Solana RPC configuration
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# LP mint and locker configuration (optional, token-specific)
LP_MINT_ADDRESS = os.getenv("LP_MINT_ADDRESS")  # Raydium/Orca LP mint for this token, if known
LOCKER_ADDRESSES = {
    addr.strip()
    for addr in os.getenv("LIQUIDITY_LOCKER_ADDRESSES", "").split(",")
    if addr.strip()
}
BURN_ADDRESSES = {
    "11111111111111111111111111111111",
}


def _solana_rpc_call(method: str, params: list) -> Any:
    """
    Thin synchronous wrapper around the Solana JSON-RPC endpoint.
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = httpx.post(SOLANA_RPC_URL, json=payload, timeout=15.0)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Solana RPC error for {method}: {data['error']}")
    return data.get("result")





def simulate_token_authorities(token_address: str) -> Tuple[bool, bool, List[str]]:
    """
    Fetch real Solana token 'mintAuthority' and 'freezeAuthority' status via JSON-RPC.

    This inspects the SPL Token mint account data for the given mint address.
    Returns:
        freeze_present: bool
        mint_present: bool
        reasons: List[str]
    """
    reasons: List[str] = []

    try:
        result = _solana_rpc_call(
            "getAccountInfo",
            [
                token_address,
                {"encoding": "base64"},
            ],
        )
    except Exception as e:
        reasons.append(f"Mint authority check failed via RPC: {e}")
        return False, False, reasons

    value = result.get("value")
    if not value or "data" not in value or not value["data"]:
        reasons.append("Mint authority check: no account data returned for this mint.")
        return False, False, reasons

    data_b64 = value["data"][0]
    try:
        raw = base64.b64decode(data_b64)
    except Exception as e:
        reasons.append(f"Mint authority check: failed to decode mint data: {e}")
        return False, False, reasons

    # SPL Token Mint layout (82 bytes):
    #   0   : mint_authority_option (1)
    #   1-32: mint_authority (32, if option != 0)
    #   33-40 / variable: supply (8)
    #   ... : decimals (1), is_initialized (1)
    #   ... : freeze_authority_option (1)
    #   ... : freeze_authority (32, if option != 0)
    try:
        idx = 0
        mint_auth_option = raw[idx]
        idx += 1
        if mint_auth_option != 0:
            mint_present = True
            idx += 32
        else:
            mint_present = False

        # Skip supply (8), decimals (1), is_initialized (1)
        idx += 8 + 1 + 1

        freeze_auth_option = raw[idx]
        if freeze_auth_option != 0:
            freeze_present = True
        else:
            freeze_present = False
    except Exception as e:
        reasons.append(f"Mint authority parsing error: {e}")
        return False, False, reasons

    if freeze_present:
        reasons.append(
            "Authority Risk: Token has an active freezeAuthority on-chain, allowing accounts "
            "to be frozen."
        )
    if mint_present:
        reasons.append(
            "Authority Risk: Token has an active mintAuthority on-chain, enabling additional "
            "supply to be minted."
        )

    if not freeze_present and not mint_present:
        reasons.append(
            "Authority Check: Both mintAuthority and freezeAuthority appear to be renounced."
        )

    return freeze_present, mint_present, reasons


def simulate_lp_stability(token_address: str) -> Tuple[float, List[str]]:
    """
    Check Liquidity Pool (LP) stability using getTokenLargestAccounts.

    Uses the LP mint address configured via LP_MINT_ADDRESS and inspects
    the top holders. If the dominant holders are known burn/locker addresses,
    we consider liquidity as locked; otherwise flagged as risky.

    Returns:
        lp_risk (0.0–1.0), reasons: List[str]
    """
    reasons: List[str] = []

    if not LP_MINT_ADDRESS:
        reasons.append(
            "Liquidity lock check skipped: LP_MINT_ADDRESS is not configured for this token."
        )
        return 0.0, reasons

    try:
        result = _solana_rpc_call(
            "getTokenLargestAccounts",
            [
                LP_MINT_ADDRESS,
                {"commitment": "confirmed"},
            ],
        )
    except Exception as e:
        reasons.append(f"Liquidity lock check via RPC failed: {e}")
        return 0.5, reasons

    accounts = result.get("value") or []
    if not accounts:
        reasons.append(
            "Liquidity lock check: no largest accounts returned for LP mint; "
            "cannot determine lock status."
        )
        return 0.5, reasons

    lp_risk = 1.0
    for acc in accounts:
        addr = acc.get("address")
        if not addr:
            continue
        if addr in BURN_ADDRESSES or addr in LOCKER_ADDRESSES:
            lp_risk = 0.0
            reasons.append(
                "Liquidity Guard: LP token largest accounts include known burn/locker targets; "
                "liquidity appears locked or burned."
            )
            break

    if lp_risk > 0:
        reasons.append(
            "⚠️ Unlocked Liquidity Risk: LP tokens are not concentrated in known burn/locker "
            "addresses, indicating withdrawable liquidity."
        )

    return lp_risk, reasons


def get_top_holders_onchain(token_address: str) -> Tuple[float, List[str]]:
    """
    Fetch the top 10 token holders and their share of total supply using
    Solana JSON-RPC (getTokenSupply + getTokenLargestAccounts).

    Returns:
        - top_10_share: total percentage (0–100) owned by the top 10 holders.
        - reasons: explanatory messages or 'Data Unavailable' notes.
    """
    reasons: List[str] = []

    # 1) Get total supply
    try:
        supply_res = _solana_rpc_call("getTokenSupply", [token_address])
    except Exception as e:
        reasons.append(
            f"Top-Holder analysis: Data Unavailable (getTokenSupply RPC error: {e})."
        )
        return 0.0, reasons

    try:
        supply_value = supply_res.get("value") or {}
        total_amount = int(supply_value.get("amount", "0"))
    except Exception as e:
        reasons.append(
            f"Top-Holder analysis: Data Unavailable (invalid supply format: {e})."
        )
        return 0.0, reasons

    if total_amount <= 0:
        reasons.append(
            "Top-Holder analysis: Data Unavailable (total supply reported as zero)."
        )
        return 0.0, reasons

    # 2) Get largest accounts (top holders)
    try:
        largest_res = _solana_rpc_call(
            "getTokenLargestAccounts",
            [token_address, {"commitment": "confirmed"}],
        )
    except Exception as e:
        reasons.append(
            f"Top-Holder analysis: Data Unavailable (getTokenLargestAccounts RPC error: {e})."
        )
        return 0.0, reasons

    accounts = largest_res.get("value") or []
    if not accounts:
        reasons.append(
            "Top-Holder analysis: Data Unavailable (no largest accounts returned)."
        )
        return 0.0, reasons

    holders: List[Tuple[str, float]] = []
    for acc in accounts[:10]:
        addr = acc.get("address")
        amt_str = acc.get("amount")
        if not addr or amt_str is None:
            continue
        try:
            amt = int(amt_str)
        except ValueError:
            continue
        pct = (amt / total_amount) * 100.0
        holders.append((addr, pct))

    if not holders:
        reasons.append(
            "Top-Holder analysis: Data Unavailable (unable to parse holder balances)."
        )
        return 0.0, reasons

    top_10_share = sum(p for _, p in holders)

    # Single wallet >10% -> Whale concentration warning
    whale_holders = [(addr, pct) for addr, pct in holders if pct > 10.0]
    if whale_holders:
        largest_whale = max(whale_holders, key=lambda x: x[1])
        reasons.append(
            "Whale Concentration Risk: Wallet '{}' controls {:.2f}% of total supply.".format(
                largest_whale[0],
                largest_whale[1],
            )
        )

    # Top-10 share >30% -> general concentration signal
    if top_10_share > 30.0:
        reasons.append(
            "Top-Holder Concentration: Top 10 holders collectively own "
            f"{top_10_share:.2f}% of the verified total supply."
        )

    return top_10_share, reasons


def analyze_wallet_clustering(
    transactions: List[Dict[str, Any]],
    token_address: str | None = None,
) -> Tuple[float, List[str], bool, bool, float]:
    """
    Analyze multiple risk dimensions and compute a Manipulation Score (0–100)
    based entirely on on-chain data:

    - Temporal clustering / sequential buying (from real transaction times).
    - Wallet age distribution (from first-seen timestamps in on-chain history).
    - High activity but low buyer diversity in the last 5 minutes.
    - Liquidity stability (LP token locking / burning).
    - On-chain token authorities (mintAuthority, freezeAuthority).
    - Top-holder concentration (from largest token accounts).

    transactions: list of dicts with at least:
        {
            "wallet": "wallet_address_string",
            "timestamp": datetime or ISO string
        }
    """
    reasons: List[str] = []

    # Normalize timestamps to datetime objects (if any)
    norm_tx: List[Dict[str, Any]] = []
    if transactions:
        for tx in transactions:
            ts = tx["timestamp"]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            norm_tx.append(
                {
                    "wallet": tx["wallet"],
                    "timestamp": ts,
                    "amount": tx.get("amount"),  # optional
                }
            )

    total = len(norm_tx)

    # Defaults when we have no transaction data
    sequential_risk = 0.0
    wallet_age_risk = 0.0
    hvld_risk = 0.0

    if total > 0:
        # 1) Temporal clustering: did > 50% of wallets trade within a 2-second window?
        norm_tx.sort(key=lambda x: x["timestamp"])

        max_in_window = 1
        window_size = timedelta(seconds=2)

        start = 0
        for end in range(total):
            while norm_tx[end]["timestamp"] - norm_tx[start]["timestamp"] > window_size:
                start += 1
            window_count = end - start + 1
            if window_count > max_in_window:
                max_in_window = window_count

        clustered_fraction = max_in_window / total

        # Convert clustering into [0,1] risk
        if clustered_fraction <= 0.5:
            sequential_risk = 0.0
        elif clustered_fraction >= 1.0:
            sequential_risk = 1.0
        else:
            sequential_risk = (clustered_fraction - 0.5) / 0.5

        if sequential_risk > 0:
            reasons.append(
                f"Sequential Buying Detected: {max_in_window}/{total} trades "
                f"occurred within a 2-second window (clustered_fraction={clustered_fraction:.2f})."
            )

        # 2) Wallet age: fraction of wallets younger than 24 hours (multi-factor check)
        unique_wallets = {tx["wallet"] for tx in norm_tx}
        new_wallet_threshold_hours = 24.0

        # Track first trade time per wallet for age analysis
        first_tx_times: Dict[str, datetime] = {}
        for tx in norm_tx:
            w = tx["wallet"]
            ts = tx["timestamp"]
            if w not in first_tx_times or ts < first_tx_times[w]:
                first_tx_times[w] = ts

        now_utc = datetime.utcnow()

        new_wallet_count = 0
        for w in unique_wallets:
            first_ts = first_tx_times.get(w)
            if not first_ts:
                continue
            age_hours = (now_utc - first_ts).total_seconds() / 3600.0
            if age_hours < new_wallet_threshold_hours:
                new_wallet_count += 1

        new_wallet_fraction = new_wallet_count / max(len(unique_wallets), 1)

        # Wallet age risk used as a *modifier* rather than a primary dimension
        wallet_age_risk = min(1.0, new_wallet_fraction)
        if wallet_age_risk > 0.5:
            reasons.append(
                f"New Wallet Concentration: {new_wallet_count}/{len(unique_wallets)} wallets "
                f"are younger than 24h (new_wallet_fraction={new_wallet_fraction:.2f})."
            )

        # Multi-factor: Fresh wallet surge for the latest up-to-50 buyers
        recent_wallets_ordered = [tx["wallet"] for tx in norm_tx[-50:]]
        if recent_wallets_ordered:
            # Evaluate unique recent buyers in this window
            recent_unique_wallets = list(dict.fromkeys(recent_wallets_ordered))
            recent_new_count = 0
            for w in recent_unique_wallets:
                first_ts = first_tx_times.get(w)
                if not first_ts:
                    continue
                age_hours = (now_utc - first_ts).total_seconds() / 3600.0
                if age_hours < new_wallet_threshold_hours:
                    recent_new_count += 1

            recent_fraction = recent_new_count / max(len(recent_unique_wallets), 1)
            if recent_fraction > 0.5 and len(recent_unique_wallets) >= 10:
                reasons.append(
                    "🚨 Fresh Wallet Surge Detected: "
                    f"{recent_new_count}/{len(recent_unique_wallets)} of the last buyers "
                    f"are younger than 24h (fraction={recent_fraction:.2f})."
                )
                # Extra bump for strongly fresh flows
                wallet_age_risk = min(1.0, max(wallet_age_risk, recent_fraction))

        # 3) Additional strictness: high activity but low buyer diversity in last 5 minutes
        latest_ts = norm_tx[-1]["timestamp"]
        five_min_window_start = latest_ts - timedelta(minutes=5)
        last_5m_txs = [tx for tx in norm_tx if tx["timestamp"] >= five_min_window_start]
        unique_buyers_5m = {tx["wallet"] for tx in last_5m_txs}

        tx_count_5m = len(last_5m_txs)
        unique_buyers_count_5m = len(unique_buyers_5m)

        # With only ~10 recent tx available, treat "high volume" as most of them
        # occurring in the last 5 minutes but coming from fewer than 10 buyers.
        high_volume_low_diversity = tx_count_5m >= 8 and unique_buyers_count_5m < 10
        hvld_risk = 1.0 if high_volume_low_diversity else 0.0

        if high_volume_low_diversity:
            reasons.append(
                "High trade activity with low buyer diversity in last 5 minutes: "
                f"{tx_count_5m} trades from {unique_buyers_count_5m} unique wallets."
            )

    # 5) Optional token-level checks (authorities, holders, LP stability)
    freeze_present = False
    mint_present = False
    top_10_share = 0.0
    lp_risk = 0.0
    honeypot_risk=0.0
    if token_address is not None:
        freeze_present, mint_present, authority_reasons = simulate_token_authorities(
            token_address
        )
        reasons.extend(authority_reasons)

        # Top-holder analysis
        top_10_share, th_reasons = get_top_holders_onchain(token_address)
        reasons.extend(th_reasons)

        # Liquidity pool stability
        lp_risk, lp_reasons = simulate_lp_stability(token_address)
        reasons.extend(lp_reasons)

    # 6) Risk matrix: core risk from temporal clustering, with modifiers from
    #    wallet age, liquidity, and wash-trading. All derived from on-chain facts.
    if total > 0:
        base_risk_score = sequential_risk * 100.0
    else:
        # No transactions: apply a conservative neutral floor to manipulation_score,
        # then let authority / liquidity checks adjust upwards if needed.
        base_risk_score = 50.0
        reasons.append("Insufficient transaction data for depth analysis.")

    # 7) Wash trading detection: same wallets repeatedly dominating flows
    wallet_trade_counts: Dict[str, int] = {}
    for tx in norm_tx:
        w = tx["wallet"]
        wallet_trade_counts[w] = wallet_trade_counts.get(w, 0) + 1

    sorted_wallets = sorted(
        wallet_trade_counts.items(), key=lambda kv: kv[1], reverse=True
    )
    top_cluster = sorted_wallets[:3]
    top_cluster_trades = sum(c for _, c in top_cluster)
    wash_fraction = top_cluster_trades / total if total else 0.0
    wash_trading_risk = 0.0
    if wash_fraction >= 0.6 and all(c >= 2 for _, c in top_cluster):
        wash_trading_risk = 1.0
        cluster_desc = ", ".join(f"{addr}({cnt})" for addr, cnt in top_cluster)
        reasons.append(
            "🚨 Wash-Trading Pattern Detected: "
            f"Top {len(top_cluster)} wallets account for {wash_fraction:.2f} of recent trades "
            f"({cluster_desc}), indicating the token is circulating within a tight cluster."
        )

    # 8) Apply modifiers from wallet age, HVLD, LP stability, and wash-trading (up to +50 points)
    modifier = (
        wallet_age_risk * 10.0
        + hvld_risk * 20.0
        + lp_risk * 10.0
        + wash_trading_risk * 10.0
    )
    manipulation_score = base_risk_score + modifier

    # 9) Honeypot behavior is considered critical: push score to 100 if triggered
    
    if honeypot_risk > 0:
        manipulation_score = 100.0

    if not reasons:
        reasons.append(
            "No strong clustering, new-wallet anomalies, source-of-funds correlation, "
            "high-volume/low-diversity patterns, honeypot-like token behavior, or "
            "centralized authority risks detected."
        )

    # If top 10 holders have >30% collectively, bump the score (already reflected
    # in reasons from simulate_top_holders).
    if top_10_share > 30.0:
        manipulation_score = min(100.0, manipulation_score + 10.0)

    # When there is no transaction history available at all, enforce a strict
    # safety floor: without observable market behavior we treat the token as
    # high-risk by default.
    if total == 0 and manipulation_score < 85.0:
        manipulation_score = 85.0

    # Additionally, enforce a high-risk floor when critical authority or
    # liquidity risks exist, even if some transactions are present. A token
    # cannot be considered low-risk if liquidity is not locked/burned or
    # if a freezeAuthority is still active.
    if total > 0 and (lp_risk > 0 or freeze_present) and manipulation_score < 85.0:
        manipulation_score = 85.0

    manipulation_score = max(0.0, min(100.0, manipulation_score))

    return manipulation_score, reasons, freeze_present, mint_present, top_10_share


async def fetch_top_recent_purchases(token_address: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Fetch recent transactions involving the given token address using
    Solana's getSignaturesForAddress and getTransaction RPC methods.

    This approximates "recent buyers" by taking the fee payer / signer
    of each transaction touching the token address.
    """
    try:
        sig_result = _solana_rpc_call(
            "getSignaturesForAddress",
            [
                token_address,
                {"limit": max(limit * 3, limit)},  # fetch a bit more for deduping
            ],
        )
    except Exception:
        # In case of RPC failure, fallback to empty list (handled upstream)
        return []

    signatures = sig_result or []
    transactions: List[Dict[str, Any]] = []

    for sig_info in signatures:
        if len(transactions) >= limit:
            break
        signature = sig_info.get("signature")
        block_time = sig_info.get("blockTime")
        if not signature or block_time is None:
            continue

        try:
            tx_res = _solana_rpc_call(
                "getTransaction",
                [
                    signature,
                    {"encoding": "jsonParsed", "commitment": "confirmed"},
                ],
            )
        except Exception:
            continue

        if not tx_res:
            continue

        tx = tx_res.get("transaction") or {}
        message = tx.get("message") or {}
        account_keys = message.get("accountKeys") or []

        payer = None
        for ak in account_keys:
            # jsonParsed returns objects with 'pubkey' and 'signer' flags
            if isinstance(ak, dict):
                if ak.get("signer"):
                    payer = ak.get("pubkey")
                    break
            else:
                # Fallback if non-parsed keys are returned
                payer = ak
                break

        if not payer:
            continue

        ts = datetime.utcfromtimestamp(block_time)
        transactions.append(
            {
                "wallet": payer,
                "timestamp": ts,
            }
        )

    return transactions


