import logging
import os
import time
from datetime import datetime
from typing import Dict, Tuple, List

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from analyzer import analyze_wallet_clustering, fetch_top_recent_purchases
from collector import fetch_solana_pairs, analyze_pair
from models import RiskAnalysisRequest, RiskAnalysisResponse, PairRisk

# ---------------------------------------------------------
# Environment / configuration
# ---------------------------------------------------------
load_dotenv()

HIGH_RISK_THRESHOLD = float(os.getenv("HIGH_RISK_THRESHOLD", "70.0"))
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "60"))
RATE_LIMIT_WINDOW_SEC = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))

# ---------------------------------------------------------
# Ensure basic folder structure exists
# ---------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

# ---------------------------------------------------------
# Logging configuration for security audit
# ---------------------------------------------------------
logger = logging.getLogger("security_audit")
logger.setLevel(logging.INFO)

file_handler = logging.FileHandler("security_audit.log")
file_handler.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(file_handler)

# ---------------------------------------------------------
# Simple in-memory rate limiting (per IP, fixed window)
# ---------------------------------------------------------
_rate_limit_store: Dict[str, Tuple[int, float]] = {}  # ip -> (count, window_start_ts)


async def rate_limiter(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()

    count, window_start = _rate_limit_store.get(client_ip, (0, now))

    # Reset window if expired
    if now - window_start > RATE_LIMIT_WINDOW_SEC:
        count = 0
        window_start = now

    count += 1
    _rate_limit_store[client_ip] = (count, window_start)

    if count > RATE_LIMIT_REQUESTS:
        # Too many requests in current window
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Try again later.",
        )


# ---------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------
app = FastAPI(
    title="Solana Token Risk Analysis API",
    version="1.0.0",
    description=(
        "Detects potential manipulation via DexScreener volume/liquidity signals "
        "and wallet clustering / wallet-age heuristics."
    ),
)

# Static files (if you add custom JS/CSS later) and templates.
# Only mount /static if the directory actually exists on disk.
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


@app.post(
    "/v1/risk-analysis/{token_address}",
    response_model=RiskAnalysisResponse,
    tags=["Risk Analysis"],
)
async def risk_analysis(
    token_address: str,
    payload: RiskAnalysisRequest,
    request: Request,
    _: None = Depends(rate_limiter),
):
    """
    Analyze a Solana token for potential manipulation.

    - Uses DexScreener data for volume-change and liquidity / wash-trading checks.
    - Uses wallet clustering & wallet-age heuristics on recent purchase transactions.
    """

    # 1) Fetch (simulated) recent purchase transactions and run clustering analysis
    try:
        transactions = await fetch_top_recent_purchases(
            token_address=token_address,
            limit=payload.max_transactions,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch recent transactions: {e}",
        )

    (
        manipulation_score,
        reasons,
        freeze_present,
        mint_present,
        top_10_share,
    ) = analyze_wallet_clustering(transactions, token_address=token_address)

    # 2) Fetch DexScreener Solana pairs and run volume/liquidity analysis
    pair_risks: List[PairRisk] = []
    timeout = httpx.Timeout(10.0, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            pairs = await fetch_solana_pairs(client)
    except Exception as e:
        # Don't fail the whole analysis if DexScreener is down; just log and continue.
        logger.warning("DexScreener fetch failed: %s", e)
        pairs = None

    if pairs:
        for pair in pairs:
            analyzed = analyze_pair(pair)
            if not analyzed:
                continue

            # Match either by base token address or pair address to the requested token
            base_addr = analyzed.get("base_token_address")
            pair_addr = analyzed.get("pair_address")
            if token_address not in {base_addr, pair_addr}:
                continue

            pair_risks.append(PairRisk(**analyzed))

        # If any DexScreener pair is high wash-trading risk, reflect that in reasons/score
        if any(p.wash_trading_risk == "High Wash-Trading Risk" for p in pair_risks):
            reasons.append("DexScreener indicates high wash-trading risk (Volume/Liquidity anomaly).")
            # Nudge the score upward for on-chain market structure anomalies
            manipulation_score = min(100.0, manipulation_score + 10.0)

    # Derive a backend Trust Score that also accounts for authority status.
    base_trust = max(0.0, 100.0 - manipulation_score)
    if freeze_present:
        base_trust = max(0.0, base_trust - 20.0)
    if mint_present:
        base_trust = max(0.0, base_trust - 20.0)
    # Penalize trust slightly for highly concentrated holder structure
    if top_10_share > 30.0:
        base_trust = max(0.0, base_trust - 10.0)
    trust_score = base_trust

    # 3) Log high-risk events
    if manipulation_score >= HIGH_RISK_THRESHOLD:
        client_ip = request.client.host if request.client else "unknown"
        logger.info(
            "HIGH RISK DETECTION | token=%s | score=%.2f | ip=%s",
            token_address,
            manipulation_score,
            client_ip,
        )

    # 4) Build response
    response = RiskAnalysisResponse(
        token_address=token_address,
        manipulation_score=manipulation_score,
        reasons=reasons,
        analyzed_transactions=len(transactions),
        timestamp_utc=datetime.utcnow(),
        pair_risks=pair_risks,
        freeze_authority_present=freeze_present,
        mint_authority_present=mint_present,
        trust_score=trust_score,
        top_holders_share=top_10_share,
    )
    return response


@app.get("/", response_class=HTMLResponse, tags=["Frontend"])
async def index(request: Request):
    """
    Render the main dashboard UI.
    """
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


