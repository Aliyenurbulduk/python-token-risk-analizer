# python-token-risk-analizer
Solana Sentinel provides a comprehensive security dashboard for SPL tokens. By analyzing live transaction data directly from the Solana Mainnet, it identifies malicious patterns before they result in financial loss.

✨ Key Features
Trust Score (0-100): A dynamic rating system that evaluates token safety based on contract authorities and liquidity status.

Authority Audit: Real-time detection of Mint and Freeze authorities to prevent supply manipulation or account locking.

Liquidity Guard: Verifies if LP tokens are burned or locked in known vaults.

Wallet Clustering: Detects "Fresh Wallet" surges and sequential buying patterns typical of bot-driven pumps.

Wash Trading Detection: Identifies tight clusters of wallets cycling volume to create fake market activity.

🛠️ Tech Stack
Backend: FastAPI (Python 3.14+)

Frontend: HTML5, Tailwind CSS, JavaScript (Real-time Telemetry UI)

Data Layer: Solana JSON RPC API (Helius/QuickNode compatible)

Security Logic: Custom-built heuristic analysis engine.
