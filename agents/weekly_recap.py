"""Weekly market recap agent.

Every Monday at 9 AM SGT:
  1. Scrape T. Rowe Price, Edward Jones, and Charles Schwab weekly updates
  2. Fetch live market data (indices, fixed income, currencies, sectors, etc.)
  3. Generate three AI-powered sections:
       - What Happened Last Week  (multi-source + data-grounded)
       - Sector Rotation Commentary  (data-driven)
       - What Markets Are Watching This Week  (forward-looking)
  4. Assemble an HTML email and send via Gmail SMTP
"""
import logging
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from groq import Groq
import pytz
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.market_data import fetch_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ── Source URLs ────────────────────────────────────────────────────────────

_SOURCE_TROWE = (
    "https://www.troweprice.com/personal-investing/resources/insights/"
    "global-markets-weekly-update.html"
)
_SOURCE_EJONES = (
    "https://www.edwardjones.com/us-en/market-news-insights/stock-market-news/"
    "stock-market-weekly-update"
)
_SOURCE_SCHWAB = "https://www.schwab.com.sg/story/weekly-traders-outlook"

_SOURCES = [
    ("T. Rowe Price",  _SOURCE_TROWE),
    ("Edward Jones",   _SOURCE_EJONES),
    ("Charles Schwab", _SOURCE_SCHWAB),
]

# ── Prompts ────────────────────────────────────────────────────────────────

_WEEKLY_REVIEW_SYSTEM = """You are a senior financial analyst writing the "What Happened Last Week" section of a Monday morning market briefing email.

You will receive:
1. Market commentary from up to three sources: T. Rowe Price, Edward Jones, and Charles Schwab
2. Actual market data for the week (index returns, yields, spreads, FX, commodities)

METRIC INTERPRETATION GUIDE — apply these definitions precisely when interpreting the data:
- VIX: higher = more fear / risk-off; lower = calmer / risk-on
- LQD/HYG ratio: HIGHER = risk-OFF (investment-grade bonds outperforming high-yield = flight to safety); LOWER = risk-ON
- Credit spreads (HY Spread, IG Spread): WIDENING = risk-off; TIGHTENING = risk-on
- 10Y-2Y Spread: deeply negative = inversion / recession signal; moving toward zero or positive = curve normalising

ATTRIBUTION FORMAT — when attributing a claim to a specific source, use inline greyed HTML only:
<span style="color:#9ca3af;font-size:11px">(T. Rowe Price)</span>
Never write "as reported by", "according to", "as noted by", or any similar phrasing.

Write a comprehensive weekly review in HTML. Rules:
- Use <h3> for section headers (include a flag emoji)
- Use <ul><li> for bullet points (3–5 per section)
- Bold (<b>) any percentage moves, rate decisions, or key data figures — use the actual numbers provided
- Sections (in this order):
    📰 Major News — 3–5 bullets on significant non-market global events from last week mentioned in the source commentary (political changes, geopolitical developments, major policy shifts). Only include events explicitly present in the provided sources — do not invent.
    🇺🇸 U.S. Markets
    🌐 Global Markets
    📊 Cross-Asset Themes — synthesize what bond, FX, and commodity moves collectively signal about macro conditions and risk appetite; apply the metric interpretation guide above
- Start directly with the first <h3> tag — no preamble"""


_WEEK_AHEAD_SYSTEM = """You are a senior financial analyst writing the "What Markets Are Watching This Week" section of a Monday morning market briefing email.

You will receive:
1. Forward-looking commentary from T. Rowe Price, Edward Jones, and Charles Schwab
2. This week's high-impact economic calendar events with consensus forecasts and prior readings
3. Current market positioning data (VIX, yield curve, credit spreads)

Write a concise but substantive week-ahead outlook in HTML using ONLY bullet points — no prose paragraphs:

<h3>📋 Key Events & Data Releases</h3>
<ul>
  <li>One event per bullet. Include: date, event name (bolded), consensus expectation, and one sentence on what a surprise would mean.</li>
  ... (4–6 bullets total)
</ul>

<h3>🎯 Themes to Watch</h3>
<ul>
  <li>One theme per bullet — the macro narratives that will drive price action this week.</li>
  ... (3–4 bullets total)
</ul>

<h3>⚠️ Risks & Wildcards</h3>
<ul>
  <li>One risk per bullet — tail risks or potential surprise catalysts.</li>
  ... (2–3 bullets total)
</ul>

Keep each bullet to 1–2 sentences. Bold event names, key dates, and consensus figures.
When attributing a claim to a specific source, use inline greyed HTML: <span style="color:#9ca3af;font-size:11px">(T. Rowe Price)</span> — never write "as noted by" or similar phrasing.
Start directly with the first <h3> tag — no preamble."""

# ── HTML helpers ───────────────────────────────────────────────────────────

_GREEN = "#16a34a"
_RED   = "#dc2626"
_GRAY  = "#6b7280"
_TH    = "background:#1a3a5c;color:#fff;padding:6px 10px;text-align:right;white-space:nowrap;"
_TH_L  = "background:#1a3a5c;color:#fff;padding:6px 10px;text-align:left;"
_TD    = "padding:5px 10px;border-bottom:1px solid #e5e7eb;text-align:right;"
_TD_L  = "padding:5px 10px;border-bottom:1px solid #e5e7eb;text-align:left;"


def _pct(val: Optional[float], decimals: int = 2) -> str:
    if val is None:
        return '<span style="color:#9ca3af">—</span>'
    sign = "+" if val >= 0 else ""
    color = _GREEN if val > 0 else (_RED if val < 0 else _GRAY)
    return f'<span style="color:{color};font-weight:600">{sign}{val:.{decimals}f}%</span>'


def _bps(val: Optional[float]) -> str:
    if val is None:
        return '<span style="color:#9ca3af">—</span>'
    sign = "+" if val >= 0 else ""
    color = _GREEN if val < 0 else (_RED if val > 0 else _GRAY)  # lower yields = green
    return f'<span style="color:{color};font-weight:600">{sign}{val:.1f} bps</span>'


def _price(val: Optional[float]) -> str:
    if val is None:
        return "—"
    if val >= 10_000:
        return f"{val:,.0f}"
    if val >= 10:
        return f"{val:,.2f}"
    return f"{val:.4f}"


def _returns_table(rows: List[Tuple[str, Optional[Dict]]], title: str) -> str:
    header = (
        f'<h3 style="color:#1a3a5c;margin-top:24px">{title}</h3>'
        '<table style="border-collapse:collapse;width:100%;font-size:13px">'
        f'<thead><tr>'
        f'<th style="{_TH_L}">Name</th>'
        f'<th style="{_TH}">Last</th>'
        f'<th style="{_TH}">1W %</th>'
        f'<th style="{_TH}">1M %</th>'
        f'<th style="{_TH}">YTD %</th>'
        f'<th style="{_TH}">1Y %</th>'
        f'</tr></thead><tbody>'
    )
    body = ""
    for name, d in rows:
        if d:
            body += (
                f'<tr><td style="{_TD_L}">{name}</td>'
                f'<td style="{_TD}">{_price(d["last"])}</td>'
                f'<td style="{_TD}">{_pct(d["weekly"])}</td>'
                f'<td style="{_TD}">{_pct(d.get("one_month"))}</td>'
                f'<td style="{_TD}">{_pct(d["ytd"])}</td>'
                f'<td style="{_TD}">{_pct(d.get("one_year"))}</td></tr>'
            )
        else:
            body += (
                f'<tr><td style="{_TD_L}">{name}</td>'
                f'<td style="{_TD}color:#9ca3af;" colspan="5">data unavailable</td></tr>'
            )
    return header + body + "</tbody></table>"


def _sector_alpha_table(sectors: List[Tuple[str, Optional[Dict]]], indices: List[Tuple[str, Optional[Dict]]]) -> str:
    spx = next((d for n, d in indices if n == "S&P 500"), None)
    if not spx:
        return ""
    header = (
        '<h3 style="color:#1a3a5c;margin-top:24px">📊 S&P 500 Sectors — Alpha vs. S&amp;P 500</h3>'
        '<table style="border-collapse:collapse;width:100%;font-size:13px">'
        f'<thead><tr>'
        f'<th style="{_TH_L}">Sector</th>'
        f'<th style="{_TH}">1W α</th>'
        f'<th style="{_TH}">1M α</th>'
        f'<th style="{_TH}">YTD α</th>'
        f'<th style="{_TH}">1Y α</th>'
        f'</tr></thead><tbody>'
    )
    body = ""
    for name, d in sectors:
        if d:
            def _a(key):
                sv, spxv = d.get(key), spx.get(key)
                return sv - spxv if sv is not None and spxv is not None else None
            body += (
                f'<tr><td style="{_TD_L}">{name}</td>'
                f'<td style="{_TD}">{_pct(_a("weekly"))}</td>'
                f'<td style="{_TD}">{_pct(_a("one_month"))}</td>'
                f'<td style="{_TD}">{_pct(_a("ytd"))}</td>'
                f'<td style="{_TD}">{_pct(_a("one_year"))}</td></tr>'
            )
        else:
            body += (
                f'<tr><td style="{_TD_L}">{name}</td>'
                f'<td style="{_TD}" colspan="4"><span style="color:#9ca3af">data unavailable</span></td></tr>'
            )
    footer = '<p style="font-size:10px;color:#9ca3af;margin:4px 0 0">Alpha = sector return minus S&amp;P 500 return for the same period</p>'
    return header + body + "</tbody></table>" + footer


_COUNTRY_FLAGS = {
    "US":  "🇺🇸", "EMU": "🇪🇺",
    "JP":  "🇯🇵", "CN":  "🇨🇳",
    "SG":  "🇸🇬",
}
_COUNTRY_NAMES = {
    "US": "United States", "EMU": "Euro Area",
    "JP": "Japan",         "CN":  "China",
    "SG": "Singapore",
}

_SIGNAL_DESCRIPTIONS = {
    "VXN / VIX": "Nasdaq vs broad market vol",
    "RSP / SPY":  "market breadth",
    "IWD / IWF":  "value vs growth",
}


def _spread_chg(val: Optional[float]) -> str:
    """Color for 10Y-2Y spread: steepening (positive Δ) = green, inverting (negative Δ) = red."""
    if val is None:
        return '<span style="color:#9ca3af">—</span>'
    sign = "+" if val >= 0 else ""
    color = _GREEN if val > 0 else (_RED if val < 0 else _GRAY)
    return f'<span style="color:{color};font-weight:600">{sign}{val:.1f} bps</span>'


def _vix_chg(val: Optional[float]) -> str:
    if val is None:
        return '<span style="color:#9ca3af">—</span>'
    sign = "+" if val >= 0 else ""
    color = _GREEN if val < 0 else (_RED if val > 0 else _GRAY)
    return f'<span style="color:{color};font-weight:600">{sign}{val:.2f}</span>'


def _ratio_chg(val: Optional[float]) -> str:
    if val is None:
        return '<span style="color:#9ca3af">—</span>'
    sign = "+" if val >= 0 else ""
    color = _GREEN if val > 0 else (_RED if val < 0 else _GRAY)
    return f'<span style="color:{color};font-weight:600">{sign}{val:.4f}</span>'


def _snapshot_signals_section(vix, spread_10y_2y, spreads, lqd_hyg, signals) -> str:
    html = (
        '<h3 style="color:#1a3a5c;margin-top:24px">🔍 Snapshot Signals</h3>'
        '<table style="border-collapse:collapse;width:100%;font-size:13px">'
        f'<thead><tr>'
        f'<th style="{_TH_L}">Metric</th>'
        f'<th style="{_TH}">Level</th>'
        f'<th style="{_TH}">1W Δ</th>'
        f'<th style="{_TH}">1M Δ</th>'
        f'<th style="{_TH}">1Y Δ</th>'
        f'</tr></thead><tbody>'
    )
    if vix:
        html += (
            f'<tr><td style="{_TD_L}">VIX <span style="font-size:10px;color:#9ca3af">(fear gauge)</span></td>'
            f'<td style="{_TD}">{vix["value"]:.2f}</td>'
            f'<td style="{_TD}">{_vix_chg(vix.get("weekly_change"))}</td>'
            f'<td style="{_TD}">{_vix_chg(vix.get("one_month_change"))}</td>'
            f'<td style="{_TD}">{_vix_chg(vix.get("one_year_change"))}</td></tr>'
        )
    if spread_10y_2y:
        html += (
            f'<tr><td style="{_TD_L}">10Y–2Y Spread <span style="font-size:10px;color:#9ca3af">(↑ = steepening)</span></td>'
            f'<td style="{_TD}">{spread_10y_2y["value"]} bps</td>'
            f'<td style="{_TD}">{_spread_chg(spread_10y_2y.get("weekly_bps"))}</td>'
            f'<td style="{_TD}">{_spread_chg(spread_10y_2y.get("one_month_bps"))}</td>'
            f'<td style="{_TD}">{_spread_chg(spread_10y_2y.get("one_year_bps"))}</td></tr>'
        )
    for name, d in spreads:
        if d:
            html += (
                f'<tr><td style="{_TD_L}">{name}</td>'
                f'<td style="{_TD}">{d["value"] * 100:.0f} bps</td>'
                f'<td style="{_TD}">{_bps(d.get("weekly_bps"))}</td>'
                f'<td style="{_TD}">{_bps(d.get("one_month_bps"))}</td>'
                f'<td style="{_TD}">{_bps(d.get("one_year_bps"))}</td></tr>'
            )
    if lqd_hyg:
        lqd_hyg_label = f'LQD / HYG <span style="font-size:10px;color:#9ca3af">(↑ = risk-off, ↓ = risk-on)</span>'
        html += (
            f'<tr><td style="{_TD_L}">{lqd_hyg_label}</td>'
            f'<td style="{_TD}">{lqd_hyg["ratio"]:.4f}</td>'
            f'<td style="{_TD}">{_ratio_chg(lqd_hyg.get("weekly_change"))}</td>'
            f'<td style="{_TD}">{_ratio_chg(lqd_hyg.get("one_month_change"))}</td>'
            f'<td style="{_TD}">{_ratio_chg(lqd_hyg.get("one_year_change"))}</td></tr>'
        )
    if signals:
        for name, d in signals:
            desc = _SIGNAL_DESCRIPTIONS.get(name, "")
            label = (f'{name} <span style="font-size:10px;color:#9ca3af">({desc})</span>'
                     if desc else name)
            if d:
                html += (
                    f'<tr><td style="{_TD_L}">{label}</td>'
                    f'<td style="{_TD}">{d["ratio"]:.4f}</td>'
                    f'<td style="{_TD}">{_ratio_chg(d.get("weekly_change"))}</td>'
                    f'<td style="{_TD}">{_ratio_chg(d.get("one_month_change"))}</td>'
                    f'<td style="{_TD}">{_ratio_chg(d.get("one_year_change"))}</td></tr>'
                )
            else:
                html += (
                    f'<tr><td style="{_TD_L}">{label}</td>'
                    f'<td style="{_TD}" colspan="4"><span style="color:#9ca3af">data unavailable</span></td></tr>'
                )
    html += "</tbody></table>"
    return html


def _yields_table(us_yields, sovereign) -> str:
    html = '<h3 style="color:#1a3a5c;margin-top:24px">💵 Fixed Income</h3>'
    html += (
        '<p style="font-weight:600;margin:12px 0 4px">Rates</p>'
        '<table style="border-collapse:collapse;width:100%;font-size:13px">'
        f'<thead><tr>'
        f'<th style="{_TH_L}">Instrument</th>'
        f'<th style="{_TH}">Yield (%)</th>'
        f'<th style="{_TH}">1W Δ</th>'
        f'<th style="{_TH}">1M Δ</th>'
        f'<th style="{_TH}">1Y Δ</th>'
        f'</tr></thead><tbody>'
    )
    for name, d in us_yields:
        if d:
            html += (
                f'<tr><td style="{_TD_L}">{name}</td>'
                f'<td style="{_TD}">{d["value"]:.2f}%</td>'
                f'<td style="{_TD}">{_bps(d.get("weekly_bps"))}</td>'
                f'<td style="{_TD}">{_bps(d.get("one_month_bps"))}</td>'
                f'<td style="{_TD}">{_bps(d.get("one_year_bps"))}</td></tr>'
            )
        else:
            html += f'<tr><td style="{_TD_L}">{name}</td><td style="{_TD}" colspan="4">—</td></tr>'
    for name, d in sovereign:
        label = f'{name} <span style="font-size:10px;color:#cbd5e1">†</span>'
        if d:
            html += (
                f'<tr><td style="{_TD_L}">{label}</td>'
                f'<td style="{_TD}">{d["value"]:.2f}%</td>'
                f'<td style="{_TD}">{_bps(d.get("weekly_bps"))}</td>'
                f'<td style="{_TD}">{_bps(d.get("one_month_bps"))}</td>'
                f'<td style="{_TD}">{_bps(d.get("one_year_bps"))}</td></tr>'
            )
        else:
            html += f'<tr><td style="{_TD_L}">{label}</td><td style="{_TD}" colspan="4">—</td></tr>'
    html += '</tbody></table>'
    html += '<p style="font-size:10px;color:#9ca3af;margin:2px 0 10px">† Monthly FRED data — 1W Δ = MoM, 1M Δ = MoM, 1Y Δ = 12M change</p>'
    return html


def _calendar_section(this_week: List[Dict], next_week: List[Dict]) -> str:

    def _table(events: List[Dict], title: str, show_actual: bool) -> str:
        t = f'<p style="font-weight:600;margin:12px 0 4px">{title}</p>'
        if not events:
            return t + '<p style="font-size:13px;color:#9ca3af">No high-impact events.</p>'
        t += (
            '<table style="border-collapse:collapse;width:100%;font-size:13px">'
            f'<thead><tr>'
            f'<th style="{_TH_L}">Date</th>'
            f'<th style="{_TH_L}">Country</th>'
            f'<th style="{_TH_L}">Event</th>'
        )
        if show_actual:
            t += f'<th style="{_TH}">Actual</th>'
        t += (
            f'<th style="{_TH}">Forecast</th>'
            f'<th style="{_TH}">Previous</th>'
            f'</tr></thead><tbody>'
        )
        for e in events:
            try:
                dt = datetime.fromisoformat(e["dateUtc"].replace("Z", "+00:00"))
                date_str = dt.strftime("%a %b %d")
            except Exception:
                date_str = e.get("dateUtc", "")[:10]
            code     = e.get("countryCode", "")
            flag     = _COUNTRY_FLAGS.get(code, "🌐")
            country  = _COUNTRY_NAMES.get(code, code)
            actual   = e.get("actual")    or "—"
            forecast = e.get("consensus") or "—"
            prev     = e.get("previous")  or "—"
            t += (
                f'<tr>'
                f'<td style="{_TD_L}">{date_str}</td>'
                f'<td style="{_TD_L}">{flag} {country}</td>'
                f'<td style="{_TD_L}">{e.get("name", "")}</td>'
            )
            if show_actual:
                t += f'<td style="{_TD}">{actual}</td>'
            t += (
                f'<td style="{_TD}">{forecast}</td>'
                f'<td style="{_TD}">{prev}</td>'
                f'</tr>'
            )
        t += '</tbody></table>'
        return t

    html  = '<h3 style="color:#1a3a5c;margin-top:24px">📅 Economic Calendar</h3>'
    html += _table(this_week, "Last Week's Key Events",  show_actual=True)
    html += _table(next_week, "This Week's Key Events",  show_actual=False)
    html += '<p style="font-size:10px;color:#9ca3af;margin:4px 0 0">High-impact events only · US, Euro Area, JP, CN, SG · Data via FXStreet</p>'
    return html


# ── LLM prompt helpers ─────────────────────────────────────────────────────

def _format_data_for_prompt(data: Dict) -> str:
    """Serialise fetch_all() output as human-readable text for LLM context."""
    parts = []

    if data.get("vix"):
        v = data["vix"]
        parts.append(f"VIX: {v['value']:.2f} (weekly change: {v['weekly_change']:+.2f} pts)")

    if data.get("spread_10y_2y"):
        s = data["spread_10y_2y"]
        wc = f", WoW: {s['weekly_bps']:+.1f} bps" if s.get("weekly_bps") is not None else ""
        parts.append(f"10Y-2Y Spread: {s['value']} bps{wc}")

    def _chg(d, key, suffix="bps"):
        v = d.get(key)
        return f" ({v:+.1f} {suffix})" if v is not None else ""

    yield_lines = [
        f"  {n}: {d['value']:.2f}%"
        + _chg(d, "weekly_bps") + _chg(d, "one_month_bps", "bps 1M") + _chg(d, "one_year_bps", "bps 1Y")
        for n, d in data.get("us_yields", []) if d
    ]
    if yield_lines:
        parts.append("US Treasury Yields:\n" + "\n".join(yield_lines))

    spread_lines = [
        f"  {n}: {d['value'] * 100:.0f} bps"
        + _chg(d, "weekly_bps") + _chg(d, "one_month_bps", "bps 1M") + _chg(d, "one_year_bps", "bps 1Y")
        for n, d in data.get("spreads", []) if d
    ]
    if spread_lines:
        parts.append("Credit Spreads:\n" + "\n".join(spread_lines))

    if data.get("lqd_hyg_ratio"):
        r = data["lqd_hyg_ratio"]
        changes = "".join([
            f" (1W: {r['weekly_change']:+.4f})"    if r.get("weekly_change")    is not None else "",
            f" (1M: {r['one_month_change']:+.4f})" if r.get("one_month_change") is not None else "",
            f" (1Y: {r['one_year_change']:+.4f})"  if r.get("one_year_change")  is not None else "",
        ])
        parts.append(f"LQD/HYG ratio (higher = risk-OFF / flight to safety): {r['ratio']:.4f}{changes}")

    idx_lines = [
        f"  {n}: {d['weekly']:+.2f}% 1W, {d.get('one_month', 0):+.2f}% 1M, {d['ytd']:+.2f}% YTD, {d.get('one_year', 0):+.2f}% 1Y"
        for n, d in data.get("indices", []) if d
    ]
    if idx_lines:
        parts.append("Equity Indices:\n" + "\n".join(idx_lines))

    sec_lines = [
        f"  {n}: {d['weekly']:+.2f}% 1W, {d.get('one_month', 0):+.2f}% 1M, {d['ytd']:+.2f}% YTD, {d.get('one_year', 0):+.2f}% 1Y"
        for n, d in data.get("sectors", []) if d
    ]
    if sec_lines:
        parts.append("S&P 500 Sectors:\n" + "\n".join(sec_lines))

    comm_lines = [
        f"  {n}: {d['weekly']:+.2f}% 1W, {d.get('one_month', 0):+.2f}% 1M, {d.get('one_year', 0):+.2f}% 1Y"
        for n, d in data.get("commodities", []) if d
    ]
    if comm_lines:
        parts.append("Commodities:\n" + "\n".join(comm_lines))

    fx_lines = [
        f"  {n}: {d['weekly']:+.2f}% 1W, {d.get('one_month', 0):+.2f}% 1M"
        for n, d in data.get("fx", []) if d
    ]
    if fx_lines:
        parts.append("FX Pairs:\n" + "\n".join(fx_lines))

    sov_lines = [
        f"  {n}: {d['value']:.2f}%"
        + _chg(d, "weekly_bps", "bps MoM") + _chg(d, "one_year_bps", "bps 1Y")
        for n, d in data.get("sovereign", []) if d
    ]
    if sov_lines:
        parts.append("Sovereign Yields (monthly FRED):\n" + "\n".join(sov_lines))

    return "\n\n".join(parts)


def _format_calendar_for_prompt(calendar: Dict) -> str:
    """Serialise economic calendar as human-readable text for LLM context."""
    _FLAGS = {"US": "🇺🇸", "EMU": "🇪🇺", "JP": "🇯🇵", "CN": "🇨🇳", "SG": "🇸🇬"}
    _NAMES = {"US": "United States", "EMU": "Euro Area", "JP": "Japan", "CN": "China", "SG": "Singapore"}

    def _section(events: List[Dict], title: str) -> str:
        if not events:
            return f"{title}: none"
        lines = [f"{title}:"]
        for e in events:
            try:
                dt = datetime.fromisoformat(e["dateUtc"].replace("Z", "+00:00"))
                date_str = dt.strftime("%a %b %d")
            except Exception:
                date_str = e.get("dateUtc", "")[:10]
            code     = e.get("countryCode", "")
            flag     = _FLAGS.get(code, "🌐")
            name     = e.get("name", "")
            forecast = e.get("consensus") or "—"
            prev     = e.get("previous")  or "—"
            actual   = e.get("actual")    or ""
            act_str  = f" | Actual: {actual}" if actual else ""
            lines.append(
                f"  {date_str} {flag} {_NAMES.get(code, code)}: {name}"
                f" | Forecast: {forecast} | Prior: {prev}{act_str}"
            )
        return "\n".join(lines)

    return (
        _section(calendar.get("this_week", []),  "LAST WEEK'S KEY EVENTS (with actuals)")
        + "\n\n"
        + _section(calendar.get("next_week", []), "THIS WEEK'S UPCOMING EVENTS (forecasts)")
    )


def _md_to_html(text: str) -> str:
    """Convert basic markdown bold/italic to HTML and strip LLM formatting artifacts."""
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*',     r'<em>\1</em>', text, flags=re.DOTALL)
    # Remove empty <p> tags that create unwanted gaps between section headers and bullet lists
    text = re.sub(r'<p>\s*(&nbsp;)?\s*</p>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>\s*<br\s*/?>', '', text, flags=re.IGNORECASE)
    return text


# ── Core agent ─────────────────────────────────────────────────────────────

class WeeklyRecapAgent:

    def _scrape_page(self, url: str, label: str, max_chars: int = 4000) -> str:
        """Generic page scraper — returns plain text or empty string on failure."""
        try:
            logger.info(f"Fetching {label}...")
            r = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; weekly-recap/1.0)"},
                timeout=15,
            )
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            main = soup.find("main") or soup.find("article") or soup.body
            lines = [l.strip() for l in main.get_text(separator="\n").splitlines() if l.strip()]
            text = "\n".join(lines)
            if len(text) > max_chars:
                text = text[:max_chars] + "\n[truncated]"
            return text
        except Exception as e:
            logger.warning(f"Could not fetch {label}: {e}")
            return ""

    def fetch_all_articles(self) -> Dict[str, str]:
        """Fetch all source articles. Returns {label: text}, skipping failed sources."""
        return {
            label: self._scrape_page(url, label)
            for label, url in _SOURCES
        }

    def _llm(self, system: str, user: str, max_tokens: int) -> str:
        """Single Groq LLM call with markdown-to-HTML cleanup."""
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens=max_tokens,
        )
        return _md_to_html(response.choices[0].message.content)

    def summarise_weekly_review(self, articles: Dict[str, str], data: Dict) -> str:
        logger.info("Generating weekly review (Groq)...")
        sources_block = "".join(
            f"\n\n--- {label} ---\n{text}"
            for label, text in articles.items() if text
        )
        user_msg = (
            f"MARKET DATA FOR THE WEEK:\n{_format_data_for_prompt(data)}"
            f"\n\nSOURCE COMMENTARY:{sources_block}"
        )
        return self._llm(_WEEKLY_REVIEW_SYSTEM, user_msg, max_tokens=2500)

    def summarise_week_ahead(self, articles: Dict[str, str], data: Dict) -> str:
        logger.info("Generating week-ahead outlook (Groq)...")
        sources_block = "".join(
            f"\n\n--- {label} ---\n{text}"
            for label, text in articles.items() if text
        )
        positioning_parts = []
        if data.get("vix"):
            positioning_parts.append(f"VIX: {data['vix']['value']:.2f}")
        if data.get("spread_10y_2y"):
            positioning_parts.append(f"10Y-2Y: {data['spread_10y_2y']['value']} bps")
        for n, d in data.get("spreads", []):
            if d:
                positioning_parts.append(f"{n}: {d['value'] * 100:.0f} bps")
        user_msg = (
            f"CURRENT POSITIONING: {', '.join(positioning_parts)}\n\n"
            f"ECONOMIC CALENDAR:\n{_format_calendar_for_prompt(data['calendar'])}"
            f"\n\nSOURCE COMMENTARY:{sources_block}"
        )
        return self._llm(_WEEK_AHEAD_SYSTEM, user_msg, max_tokens=1500)

    def build_email(
        self,
        review_html: str,
        week_ahead_html: str,
        data: Dict,
        date_str: str,
    ) -> str:
        snapshot_section    = _snapshot_signals_section(
            data.get("vix"), data["spread_10y_2y"], data["spreads"],
            data["lqd_hyg_ratio"], data["signals"],
        )
        indices_section     = _returns_table(data["indices"],     "📈 Global Equity Indices")
        sectors_section     = _returns_table(data["sectors"],     "🏭 S&P 500 Sectors (GICS)")
        sectors_alpha       = _sector_alpha_table(data["sectors"], data["indices"])
        bond_etf_section    = _returns_table(data["bond_etfs"],   "Bond ETFs")
        fi_section          = _yields_table(data["us_yields"], data["sovereign"]) + bond_etf_section
        commodities_section = _returns_table(data["commodities"], "🛢️ Commodities")
        fx_section          = _returns_table(data["fx"],          "💱 FX")
        crypto_section      = _returns_table(data["crypto"],      "🪙 Crypto")
        cal_section         = _calendar_section(
            data["calendar"]["this_week"], data["calendar"]["next_week"]
        )
        source_links = " · ".join(
            f'<a href="{url}" style="color:#6b7280;">{label}</a>'
            for label, url in _SOURCES
        )

        return f"""<html>
<body style="font-family:Arial,sans-serif;max-width:720px;margin:auto;color:#222;line-height:1.6;">

  <div style="background:#1a3a5c;color:#fff;padding:18px 24px;border-radius:6px 6px 0 0;">
    <h2 style="margin:0;font-size:20px;">🌍 Weekly Market Recap &amp; Outlook</h2>
    <p style="margin:4px 0 0;font-size:13px;opacity:0.8;">{date_str}</p>
  </div>

  <div style="padding:20px 24px;background:#f9fafb;border:1px solid #e5e7eb;border-top:none;">

    <h3 style="color:#1a3a5c;margin-top:0">📝 What Happened Last Week</h3>
    <div style="background:#fff;padding:16px;border-radius:4px;border:1px solid #e5e7eb;">
      {review_html}
      <p style="font-size:11px;color:#9ca3af;margin:12px 0 0">
        Sources: {source_links}
      </p>
    </div>

    {snapshot_section}
    {indices_section}
    {sectors_section}
    {sectors_alpha}
    {fi_section}
    {commodities_section}
    {fx_section}
    {crypto_section}
    {cal_section}

    <h3 style="color:#1a3a5c;margin-top:24px">🔭 What Markets Are Watching This Week</h3>
    <div style="background:#fff;padding:16px;border-radius:4px;border:1px solid #e5e7eb;">
      {week_ahead_html}
      <p style="font-size:11px;color:#9ca3af;margin:12px 0 0">
        Sources: {source_links} · Calendar via FXStreet
      </p>
    </div>

    <hr style="margin-top:32px;border:none;border-top:1px solid #e5e7eb;">
    <p style="font-size:11px;color:#9ca3af;">
      Market data via Yahoo Finance &amp; FRED · Commentary via {source_links}<br>
      Delivered automatically every Monday at 9:00 AM SGT.
    </p>
  </div>

</body>
</html>"""

    def send_email(self, subject: str, html: str) -> None:
        sender    = os.getenv("GMAIL_USER")
        password  = os.getenv("GMAIL_APP_PASSWORD")
        recipient = os.getenv("RECIPIENT_EMAIL") or sender

        if not sender or not password:
            logger.error("GMAIL_USER / GMAIL_APP_PASSWORD not set — skipping send")
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = sender
        msg["To"]      = recipient
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        logger.info(f"Email sent → {recipient}")

    def run(self) -> None:
        sgt      = pytz.timezone("Asia/Singapore")
        date_str = datetime.now(sgt).strftime("%B %d, %Y")
        subject  = f"🌍 Weekly Market Recap & Outlook — {date_str}"

        articles = self.fetch_all_articles()
        fred_key = os.getenv("FRED_API_KEY", "")
        data     = fetch_all(fred_key)

        review_html     = self.summarise_weekly_review(articles, data)
        week_ahead_html = self.summarise_week_ahead(articles, data)

        email_html = self.build_email(review_html, week_ahead_html, data, date_str)
        self.send_email(subject, email_html)
        logger.info("Weekly recap complete.")


if __name__ == "__main__":
    WeeklyRecapAgent().run()
