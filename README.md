# Weekly Market Recap

An automated agent that delivers a Monday morning market briefing email covering the previous week's global market performance.

Every Monday at 9 AM SGT, GitHub Actions runs the agent which scrapes the [T. Rowe Price Global Markets Weekly Update](https://www.troweprice.com/personal-investing/resources/insights/global-markets-weekly-update.html), summarises it using an AI language model (Llama 3.3 70B via Groq), and assembles a structured HTML email covering global equity indices, S&P 500 sectors, fixed income, commodities, FX, crypto, key market signals, and the economic calendar for the week ahead.

## Sections

- **Snapshot Signals** — VIX, yield curve spread, HY/IG credit spreads, and ratio signals (LQD/HYG, RSP/SPY, IWD/IWF)
- **Global Equity Indices** — Major indices across US, Europe, and Asia
- **S&P 500 Sectors** — All 11 GICS sectors via SPDR ETFs
- **Fixed Income** — US Treasury yields, sovereign yields, and bond ETF returns
- **Commodities** — Gold, silver, copper, WTI and Brent crude
- **FX** — Major currency pairs
- **Crypto** — BTC, ETH, SOL
- **Economic Calendar** — High-impact events for US, Euro Area, Japan, China, and Singapore

## Data Sources

- [Yahoo Finance](https://finance.yahoo.com) via yfinance — price and return data
- [FRED](https://fred.stlouisfed.org) — US Treasury yields, sovereign yields, credit spreads
- [FXStreet](https://www.fxstreet.com/economic-calendar) — Economic calendar
- [Groq](https://groq.com) — AI summarisation (free tier)

## Setup

Add the following secrets to your GitHub repository under **Settings → Secrets → Actions**:

| Secret | Description |
|---|---|
| `GROQ_API_KEY` | Groq API key (free at groq.com) |
| `FRED_API_KEY` | FRED API key (free at fred.stlouisfed.org) |
| `GMAIL_USER` | Gmail address used to send the email |
| `GMAIL_APP_PASSWORD` | Gmail App Password (requires 2FA enabled) |
| `RECIPIENT_EMAIL` | Email address to receive the recap |
