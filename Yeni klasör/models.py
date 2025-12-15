from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, ConfigDict, field_validator


class RiskAnalysisRequest(BaseModel):
    """
    Incoming request body for risk analysis.
    """

    max_transactions: int = Field(
        10,
        ge=1,
        le=50,
        description="Maximum number of recent purchase transactions to analyze.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "max_transactions": 10,
            }
        }
    )


class PairRisk(BaseModel):
    """
    Summary of DexScreener-based risk metrics for a specific pair.
    """

    pair: str
    pair_address: str
    base_token_address: Optional[str]
    volume_h1: float
    volume_h2: float
    volume_increase_pct: float
    liquidity_usd: float
    volume_liquidity_ratio: float
    wash_trading_risk: str


class RiskAnalysisResponse(BaseModel):
    """
    Combined response including manipulation score and reasons, plus optional
    volume/liquidity analysis data.
    """

    token_address: str
    manipulation_score: float = Field(ge=0, le=100)
    reasons: List[str]
    analyzed_transactions: int
    timestamp_utc: datetime
    pair_risks: List[PairRisk] = Field(
        default_factory=list,
        description="DexScreener-based risk metrics for matching pairs.",
    )
    freeze_authority_present: bool = Field(
        default=False,
        description="Whether the token still has an active Freeze Authority.",
    )
    mint_authority_present: bool = Field(
        default=True,
        description="Whether the token still has an active Mint Authority.",
    )
    trust_score: float = Field(
        ge=0,
        le=100,
        description="Backend-computed Trust Score factoring manipulation risk and authority status.",
    )
    top_holders_share: float = Field(
        default=0.0,
        ge=0,
        le=100,
        description="Approximate percentage of total supply held by the top 10 holders.",
    )

    @field_validator("token_address")
    @classmethod
    def validate_token_address(cls, v: str) -> str:
        if not v:
            raise ValueError("Token address must not be empty.")
        return v


