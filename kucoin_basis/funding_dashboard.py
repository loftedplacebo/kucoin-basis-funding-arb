from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.symbols import normalise_symbol, split_standard_symbol
from kucoin_basis.config import DEFAULT_CONFIG, KucoinBasisConfig
from kucoin_basis.kucoin_public_client import KucoinPublicClient
from kucoin_basis.models import parse_datetime, parse_float
from kucoin_basis.paper_store import PaperStore


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KuCoin Funding Rates</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --line: #d7dde8;
      --text: #162033;
      --muted: #5d6a7d;
      --accent: #12715f;
      --bad: #a23b3b;
      --good: #167052;
      --shadow: 0 10px 30px rgba(17, 30, 54, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 22px 28px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 {
      font-size: 22px;
      margin: 0 0 12px;
      font-weight: 650;
      letter-spacing: 0;
    }
    .controls {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    .tabs {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    button, input {
      height: 36px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
    }
    button {
      cursor: pointer;
      font-weight: 600;
    }
    button.active {
      border-color: var(--accent);
      color: var(--accent);
      box-shadow: inset 0 0 0 1px var(--accent);
    }
    input {
      min-width: 220px;
    }
    .meta {
      margin-top: 10px;
      color: var(--muted);
      font-size: 13px;
      display: flex;
      gap: 18px;
      flex-wrap: wrap;
    }
    .notes {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .note {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 10px 12px;
      color: var(--muted);
      font-size: 13px;
      box-shadow: var(--shadow);
    }
    .note strong {
      color: var(--text);
    }
    .tab-section.hidden { display: none; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      box-shadow: var(--shadow);
    }
    .metric-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }
    .metric-value {
      display: block;
      margin-top: 6px;
      font-size: 22px;
      font-weight: 650;
    }
    main { padding: 18px 28px 28px; }
    .table-wrap {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: auto;
      box-shadow: var(--shadow);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 1080px;
    }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: right;
      white-space: nowrap;
      font-size: 14px;
    }
    th {
      background: #eef2f7;
      color: #27364d;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0;
      position: sticky;
      top: 0;
      z-index: 1;
    }
    th:first-child, td:first-child,
    th:nth-child(2), td:nth-child(2) {
      text-align: left;
    }
    tr:hover td { background: #f6fbf9; }
    #shortlistSection .table-wrap {
      overflow-x: hidden;
    }
    #shortlistSection table {
      min-width: 0;
      width: 100%;
      table-layout: fixed;
    }
    #shortlistSection th,
    #shortlistSection td {
      padding: 7px 8px;
      font-size: 12px;
      white-space: normal;
      overflow-wrap: anywhere;
    }
    #shortlistSection th {
      line-height: 1.15;
    }
    #shortlistSection th:nth-child(1),
    #shortlistSection td:nth-child(1) {
      width: 7%;
    }
    #shortlistSection th:nth-child(2),
    #shortlistSection td:nth-child(2) {
      width: 14%;
    }
    #shortlistSection th:nth-child(2),
    #shortlistSection td:nth-child(2),
    #shortlistSection th:last-child,
    #shortlistSection td:last-child {
      text-align: left;
    }
    .edge-cell {
      background: #e7f5ef;
      border-left: 1px solid #b8ded1;
      border-right: 1px solid #b8ded1;
    }
    .decision-cell {
      font-weight: 650;
    }
    .decision-cell.enter {
      color: var(--good);
    }
    .decision-cell.reject {
      color: var(--bad);
    }
    .positive { color: var(--good); font-weight: 650; }
    .negative { color: var(--bad); font-weight: 650; }
    .muted { color: var(--muted); }
    .warning { color: #9a5b00; font-weight: 650; }
    .date-cell {
      color: var(--text);
      font-variant-numeric: tabular-nums;
    }
    .error {
      margin: 18px 0;
      padding: 12px;
      border: 1px solid #d48b8b;
      background: #fff1f1;
      color: #862f2f;
      border-radius: 8px;
      display: none;
    }
    .chunk-cell { display: none; }
    #shortlistSection.show-chunks .chunk-cell { display: table-cell; }
    @media (max-width: 720px) {
      header, main { padding-left: 14px; padding-right: 14px; }
      input { min-width: 100%; }
    }
  </style>
</head>
<body>
  <header>
    <h1>KuCoin USDT Perp Funding Rates</h1>
    <div class="tabs">
      <button class="tab active" data-tab="funding">Funding</button>
      <button class="tab" data-tab="shortlist">Shortlist</button>
      <button class="tab" data-tab="positions">Positions</button>
      <button class="tab" data-tab="summary">Summary</button>
    </div>
    <div class="controls" id="fundingControls">
      <button id="sortFundingDesc" class="active">Funding high to low</button>
      <button id="sortFundingAsc">Funding low to high</button>
      <button id="sortSoonest">Next funding soonest</button>
      <button id="refresh">Refresh</button>
      <input id="filter" placeholder="Filter coin or symbol" autocomplete="off">
    </div>
    <div class="meta">
      <span id="status">Loading public KuCoin data...</span>
      <span>Rate shown: <strong>contracts/active fundingFeeRate x 100</strong></span>
      <span>Filtered to perps with enabled KuCoin spot USDT pairs</span>
      <span>Positive rate: shorts receive, longs pay</span>
    </div>
  </header>
  <main>
    <div id="error" class="error"></div>
    <section id="fundingSection" class="tab-section">
      <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Base</th>
            <th>Perp</th>
            <th>Spot</th>
            <th>KuCoin funding symbol</th>
            <th>Funding fee</th>
            <th>Raw decimal</th>
            <th>Next funding (UTC)</th>
            <th>Minutes</th>
            <th>Interval</th>
            <th>Cap</th>
            <th>Floor</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
      </div>
    </section>
    <section id="shortlistSection" class="tab-section hidden">
      <div class="controls" id="shortlistControls">
        <button class="direction-filter active" data-direction="ALL">All directions</button>
        <button class="direction-filter" data-direction="LONG_SPOT_SHORT_PERP">Long spot / short perp</button>
        <button class="direction-filter" data-direction="SHORT_SPOT_LONG_PERP">Short spot / long perp</button>
        <input id="shortlistFilter" placeholder="Search symbol or reason" autocomplete="off">
        <button id="toggleChunks">Show chunks</button>
      </div>
      <div class="notes">
        <div class="note"><strong>Funding edge</strong>: expected funding profit after executable entry/exit slippage, taker fees, and safety buffer.</div>
        <div class="note"><strong>Basis</strong>: perp mid-price versus spot mid-price. Positive basis means perp is richer than spot; negative basis means perp is cheaper than spot.</div>
        <div class="note"><strong>Scenario edge</strong>: funding edge plus haircutted basis-convergence upside. Informational only; entries still use funding edge.</div>
      </div>
      <div class="table-wrap">
        <table class="shortlist-table">
          <thead>
            <tr>
              <th>Base</th>
              <th>Dir</th>
              <th class="chunk-cell">Notional</th>
              <th>Funding</th>
              <th>Edge</th>
              <th>Basis up</th>
              <th>Scenario</th>
              <th>Min</th>
              <th>Basis</th>
              <th>Fillable</th>
              <th>Decision</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody id="shortlistRows"></tbody>
        </table>
      </div>
    </section>
    <section id="positionsSection" class="tab-section hidden">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Status</th>
              <th>Base</th>
              <th>Direction</th>
              <th>Notional</th>
              <th>Age</th>
              <th>Funding state</th>
              <th>Exp funding</th>
              <th>Exp fund PnL</th>
              <th>Entry basis</th>
              <th>Current basis</th>
              <th>Basis improvement</th>
              <th>Realised funding</th>
              <th>Basis PnL</th>
              <th>Net PnL</th>
              <th>Next funding</th>
              <th>Last decision</th>
              <th>Symbol cap</th>
              <th>Events</th>
            </tr>
          </thead>
          <tbody id="positionRows"></tbody>
        </table>
      </div>
      <h2>Recent Fills</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Event</th>
              <th>Base</th>
              <th>Direction</th>
              <th>Notional</th>
              <th>Realised PnL</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody id="fillRows"></tbody>
        </table>
      </div>
    </section>
    <section id="summarySection" class="tab-section hidden">
      <div id="summaryMetrics" class="metrics"></div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Type</th>
              <th>Reason</th>
              <th>Count</th>
            </tr>
          </thead>
          <tbody id="summaryReasons"></tbody>
        </table>
      </div>
    </section>
  </main>
  <script>
    let items = [];
    let shortlistItems = [];
    let shortlistRawItems = [];
    let shortlistRawRowCount = 0;
    let positionsPayload = { positions: [], recentFills: [] };
    let summaryPayload = {};
    let sortMode = "fundingDesc";
    let activeTab = "funding";
    let shortlistDirection = "ALL";
    let shortlistShowChunks = false;

    const rowsEl = document.getElementById("rows");
    const shortlistRowsEl = document.getElementById("shortlistRows");
    const positionRowsEl = document.getElementById("positionRows");
    const fillRowsEl = document.getElementById("fillRows");
    const summaryMetricsEl = document.getElementById("summaryMetrics");
    const summaryReasonsEl = document.getElementById("summaryReasons");
    const statusEl = document.getElementById("status");
    const errorEl = document.getElementById("error");
    const filterEl = document.getElementById("filter");
    const shortlistFilterEl = document.getElementById("shortlistFilter");
    const toggleChunksEl = document.getElementById("toggleChunks");
    const fundingControlsEl = document.getElementById("fundingControls");
    const shortlistControlsEl = document.getElementById("shortlistControls");

    const buttons = {
      fundingDesc: document.getElementById("sortFundingDesc"),
      fundingAsc: document.getElementById("sortFundingAsc"),
      soonest: document.getElementById("sortSoonest")
    };

    function fmt(value, digits = 4) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "";
      return Number(value).toFixed(digits);
    }

    function fmtPct(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "";
      return `${Number(value).toFixed(4)}%`;
    }

    function fmtMoney(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "";
      return `$${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    }

    function fmtDateTime(value, includeSeconds = false) {
      if (value === null || value === undefined || value === "") return "";
      const date = typeof value === "number" ? new Date(value) : new Date(String(value));
      if (Number.isNaN(date.getTime())) return "";
      const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
      const dd = String(date.getUTCDate()).padStart(2, "0");
      const month = months[date.getUTCMonth()];
      const yyyy = date.getUTCFullYear();
      const hh = String(date.getUTCHours()).padStart(2, "0");
      const mm = String(date.getUTCMinutes()).padStart(2, "0");
      const ss = String(date.getUTCSeconds()).padStart(2, "0");
      return `${dd} ${month} ${yyyy} ${hh}:${mm}${includeSeconds ? `:${ss}` : ""} UTC`;
    }

    function dateCell(value, includeSeconds = false, rawValue = null) {
      const display = fmtDateTime(value, includeSeconds);
      if (!display) return "";
      const raw = rawValue ?? value;
      return `<span class="date-cell" title="${raw}">${display}</span>`;
    }

    function fundingStateClass(value) {
      if (value === "funding due") return "warning";
      if (value === "funding captured") return "positive";
      return "muted";
    }

    function displayDirection(value) {
      if (value === "SHORT_SPOT_LONG_PERP") return "Short spot / long perp";
      if (value === "LONG_SPOT_SHORT_PERP") return "Long spot / short perp";
      return value || "";
    }

    function displayReason(value) {
      const labels = {
        entry_rules_passed: "entry passed",
        expected_edge_below_threshold: "edge below min",
        funding_below_threshold: "funding too low",
        too_close_to_funding: "too close",
        round_trip_not_fillable: "not fillable",
        basis_not_low_enough_for_short_spot: "basis not low enough",
        basis_not_high_enough_for_long_spot: "basis not high enough",
        lower_ranked_chunk_same_tick: "lower ranked chunk",
        max_symbol_exposure: "symbol cap",
        max_total_exposure: "total cap",
        max_open_positions: "position cap",
        full_position_close_liquidity_missing: "close liquidity missing",
      };
      return labels[value] || value || "";
    }

    function decisionClass(value) {
      if (value === "ENTER_CANDIDATE") return "enter";
      if (value === "REJECT") return "reject";
      return "";
    }

    function minutesToFunding(item) {
      if (!item.fundingTimeMs) return null;
      return (item.fundingTimeMs - Date.now()) / 60000;
    }

    function activeButtons() {
      for (const [mode, button] of Object.entries(buttons)) {
        button.classList.toggle("active", mode === sortMode);
      }
    }

    function sortedRows() {
      const filter = filterEl.value.trim().toUpperCase();
      const filtered = items.filter((item) => {
        if (!filter) return true;
        return item.base.includes(filter)
          || item.perpSymbol.includes(filter)
          || item.spotSymbol.includes(filter)
          || item.fundingSymbol.includes(filter);
      });
      filtered.sort((a, b) => {
        if (sortMode === "fundingAsc") {
          return (a.nextFundingRatePct ?? 999) - (b.nextFundingRatePct ?? 999);
        }
        if (sortMode === "soonest") {
          return (minutesToFunding(a) ?? 999999) - (minutesToFunding(b) ?? 999999);
        }
        return (b.nextFundingRatePct ?? -999) - (a.nextFundingRatePct ?? -999);
      });
      return filtered;
    }

    function render() {
      activeButtons();
      rowsEl.innerHTML = "";
      for (const item of sortedRows()) {
        const minutes = minutesToFunding(item);
        const rateClass = item.nextFundingRatePct > 0 ? "positive" : item.nextFundingRatePct < 0 ? "negative" : "";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><strong>${item.base}</strong></td>
          <td>${item.perpSymbol}</td>
          <td>${item.spotSymbol}</td>
          <td class="muted">${item.fundingSymbol}</td>
          <td class="${rateClass}">${fmtPct(item.nextFundingRatePct)}</td>
          <td>${item.nextFundingRateDecimal ?? ""}</td>
          <td>${dateCell(item.fundingTimeMs, false, item.fundingTimeUtc)}</td>
          <td>${minutes === null ? "" : fmt(minutes, 1)}</td>
          <td>${item.intervalHours ? `${fmt(item.intervalHours, 2)}h` : ""}</td>
          <td>${fmtPct(item.fundingRateCapPct)}</td>
          <td>${fmtPct(item.fundingRateFloorPct)}</td>
        `;
        rowsEl.appendChild(tr);
      }
    }

    function renderShortlist() {
      shortlistRowsEl.innerHTML = "";
      document.getElementById("shortlistSection").classList.toggle("show-chunks", shortlistShowChunks);
      toggleChunksEl.classList.toggle("active", shortlistShowChunks);
      toggleChunksEl.textContent = shortlistShowChunks ? "Hide chunks" : "Show chunks";
      const sourceItems = shortlistShowChunks ? shortlistRawItems : shortlistItems;
      const filter = shortlistFilterEl.value.trim().toUpperCase();
      const visibleItems = sourceItems.filter((item) => {
        const directionMatches = shortlistDirection === "ALL" || item.direction === shortlistDirection;
        if (!directionMatches) return false;
        if (!filter) return true;
        return item.base.includes(filter)
          || item.direction.includes(filter)
          || item.spotSymbol.includes(filter)
          || item.perpSymbol.includes(filter)
          || item.decision.includes(filter)
          || item.reason.includes(filter);
      });
      for (const item of visibleItems) {
        const edgeClass = item.expectedEdgePct >= 0 ? "positive" : "negative";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><strong>${item.base}</strong></td>
          <td>${displayDirection(item.direction)}</td>
          <td class="chunk-cell">${fmtMoney(item.notionalUsd)}</td>
          <td class="${item.fundingBenefitPct >= 0 ? "positive" : "negative"}">${fmtPct(item.fundingBenefitPct)}</td>
          <td class="edge-cell ${edgeClass}">${fmtPct(item.expectedEdgePct)}</td>
          <td class="${item.basisConvergenceUpsidePct >= 0 ? "positive" : "negative"}">${fmtPct(item.basisConvergenceUpsidePct)}</td>
          <td class="${item.scenarioEdgePct >= 0 ? "positive" : "negative"}">${fmtPct(item.scenarioEdgePct)}</td>
          <td>${fmt(item.minutesToFunding, 1)}</td>
          <td>${fmtPct(item.basisPct)}</td>
          <td>${item.roundTripFillable ? "yes" : "no"}</td>
          <td class="decision-cell ${decisionClass(item.decision)}">${item.decision === "ENTER_CANDIDATE" ? "ENTER" : item.decision}</td>
          <td title="${item.reason}">${displayReason(item.reason)}</td>
        `;
        shortlistRowsEl.appendChild(tr);
      }
      document.querySelectorAll(".direction-filter").forEach((button) => {
        button.classList.toggle("active", button.dataset.direction === shortlistDirection);
      });
      updateShortlistStatus(visibleItems.length, sourceItems.length);
    }

    function updateShortlistStatus(visibleCount, sourceCount) {
      const directionText = shortlistDirection === "ALL" ? "all directions" : shortlistDirection;
      const filter = shortlistFilterEl.value.trim();
      const modeText = shortlistShowChunks ? "chunk rows" : "symbol/direction rows";
      const filterText = filter ? ` matching "${filter}"` : "";
      statusEl.textContent = `${visibleCount} of ${sourceCount} ${modeText}${filterText} (${directionText}); ${shortlistRawRowCount || shortlistRawItems.length} raw chunk rows loaded`;
    }

    function renderPositions() {
      positionRowsEl.innerHTML = "";
      for (const position of positionsPayload.positions || []) {
        const pnl = Number(position.estimated_net_pnl_usd || 0);
        const improvement = Number(position.basis_improvement_pct || 0);
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${position.status || ""}</td>
          <td><strong>${position.base || ""}</strong></td>
          <td>${position.direction || ""}</td>
          <td>${fmtMoney(position.notional_usd)}</td>
          <td>${position.age_label || ""}</td>
          <td class="${fundingStateClass(position.funding_state)}">${position.funding_state || ""}</td>
          <td class="${Number(position.expected_funding_pct || 0) >= 0 ? "positive" : "negative"}">${fmtPct(position.expected_funding_pct)}</td>
          <td class="${Number(position.expected_funding_pnl_usd || 0) >= 0 ? "positive" : "negative"}">${fmtMoney(position.expected_funding_pnl_usd)}</td>
          <td>${fmtPct(position.entry_basis_pct)}</td>
          <td>${fmtPct(position.current_basis_pct)}</td>
          <td class="${improvement >= 0 ? "positive" : "negative"}">${fmtPct(improvement)}</td>
          <td>${fmtMoney(position.realised_funding_pnl_usd)}</td>
          <td>${fmtMoney(position.unrealised_basis_pnl_usd)}</td>
          <td class="${pnl >= 0 ? "positive" : "negative"}">${fmtMoney(pnl)}</td>
          <td>${dateCell(position.next_funding_time)}</td>
          <td>${position.latest_exit_reason || ""}</td>
          <td>${fmtMoney(position.symbol_usage_usd)} / ${fmtMoney(positionsPayload.maxSymbolNotionalUsd)}</td>
          <td>${position.funding_events_captured || "0"}</td>
        `;
        positionRowsEl.appendChild(tr);
      }

      fillRowsEl.innerHTML = "";
      for (const fill of positionsPayload.recentFills || []) {
        const pnl = Number(fill.realised_pnl_usd || 0);
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${dateCell(fill.timestamp_utc, true)}</td>
          <td>${fill.event_type || ""}</td>
          <td><strong>${fill.base || ""}</strong></td>
          <td>${fill.direction || ""}</td>
          <td>${fmtMoney(fill.notional_usd)}</td>
          <td class="${pnl >= 0 ? "positive" : "negative"}">${fmtMoney(pnl)}</td>
          <td>${fill.reason || ""}</td>
        `;
        fillRowsEl.appendChild(tr);
      }
    }

    function renderSummary() {
      const metrics = [
        ["Open positions", summaryPayload.openPositions],
        ["Open notional", fmtMoney(summaryPayload.totalOpenNotionalUsd)],
        ["Estimated open PnL", fmtMoney(summaryPayload.estimatedOpenPnlUsd)],
        ["Funding PnL today", fmtMoney(summaryPayload.realisedFundingPnlTodayUsd)],
        ["Total PnL today", fmtMoney(summaryPayload.realisedTotalPnlTodayUsd)],
        ["Funding events today", summaryPayload.fundingEventsCapturedToday],
        ["Shortlist rows", summaryPayload.shortlistRows],
        ["Entry candidates", summaryPayload.entryCandidates],
      ];
      summaryMetricsEl.innerHTML = "";
      for (const [label, value] of metrics) {
        const div = document.createElement("div");
        div.className = "metric";
        div.innerHTML = `<span class="metric-label">${label}</span><span class="metric-value">${value ?? ""}</span>`;
        summaryMetricsEl.appendChild(div);
      }

      summaryReasonsEl.innerHTML = "";
      for (const [type, rows] of [["Entry reject", summaryPayload.entryRejections || []], ["Exit", summaryPayload.exitReasons || []]]) {
        for (const row of rows) {
          const tr = document.createElement("tr");
          tr.innerHTML = `<td>${type}</td><td>${row[0]}</td><td>${row[1]}</td>`;
          summaryReasonsEl.appendChild(tr);
        }
      }
    }

    function showTab(name) {
      activeTab = name;
      document.querySelectorAll(".tab").forEach((button) => {
        button.classList.toggle("active", button.dataset.tab === name);
      });
      for (const section of document.querySelectorAll(".tab-section")) {
        section.classList.add("hidden");
      }
      document.getElementById(`${name}Section`).classList.remove("hidden");
      fundingControlsEl.style.display = name === "funding" ? "flex" : "none";
      shortlistControlsEl.style.display = name === "shortlist" ? "flex" : "none";
      loadActive();
    }

    async function load() {
      errorEl.style.display = "none";
      statusEl.textContent = "Loading public KuCoin data...";
      try {
        const response = await fetch("/api/funding", { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || response.statusText);
        items = payload.items || [];
        statusEl.textContent = `${items.length} active USDT perps with spot pairs loaded at ${fmtDateTime(payload.observedAtUtc, true)}`;
        render();
      } catch (error) {
        errorEl.textContent = error.message;
        errorEl.style.display = "block";
        statusEl.textContent = "Load failed";
      }
    }

    async function loadShortlist() {
      errorEl.style.display = "none";
      statusEl.textContent = "Loading latest shortlist...";
      try {
        const response = await fetch("/api/shortlist", { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || response.statusText);
        shortlistItems = payload.items || [];
        shortlistRawItems = payload.rawItems || [];
        shortlistRawRowCount = payload.rawRowCount || shortlistRawItems.length;
        renderShortlist();
      } catch (error) {
        errorEl.textContent = error.message;
        errorEl.style.display = "block";
        statusEl.textContent = "Shortlist load failed";
      }
    }

    async function loadPositions() {
      errorEl.style.display = "none";
      statusEl.textContent = "Loading paper positions...";
      try {
        const response = await fetch("/api/positions", { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || response.statusText);
        positionsPayload = payload;
        statusEl.textContent = `${(payload.positions || []).length} paper positions loaded at ${fmtDateTime(payload.observedAtUtc, true)}`;
        renderPositions();
      } catch (error) {
        errorEl.textContent = error.message;
        errorEl.style.display = "block";
        statusEl.textContent = "Positions load failed";
      }
    }

    async function loadSummary() {
      errorEl.style.display = "none";
      statusEl.textContent = "Loading paper summary...";
      try {
        const response = await fetch("/api/summary", { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || response.statusText);
        summaryPayload = payload;
        statusEl.textContent = `Summary loaded at ${fmtDateTime(payload.observedAtUtc, true)}`;
        renderSummary();
      } catch (error) {
        errorEl.textContent = error.message;
        errorEl.style.display = "block";
        statusEl.textContent = "Summary load failed";
      }
    }

    function loadActive() {
      if (activeTab === "shortlist") return loadShortlist();
      if (activeTab === "positions") return loadPositions();
      if (activeTab === "summary") return loadSummary();
      return load();
    }

    for (const [mode, button] of Object.entries(buttons)) {
      button.addEventListener("click", () => {
        sortMode = mode;
        render();
      });
    }
    document.querySelectorAll(".tab").forEach((button) => {
      button.addEventListener("click", () => showTab(button.dataset.tab));
    });
    document.querySelectorAll(".direction-filter").forEach((button) => {
      button.addEventListener("click", () => {
        shortlistDirection = button.dataset.direction;
        renderShortlist();
      });
    });
    shortlistFilterEl.addEventListener("input", renderShortlist);
    toggleChunksEl.addEventListener("click", () => {
      shortlistShowChunks = !shortlistShowChunks;
      renderShortlist();
    });
    document.getElementById("refresh").addEventListener("click", loadActive);
    filterEl.addEventListener("input", render);
    setInterval(render, 15000);
    load();
  </script>
</body>
</html>
"""


def _to_pct(value: float | None) -> float | None:
    return None if value is None else value * 100


def _float_value(data: dict, key: str) -> float | None:
    value = data.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _funding_time_ms(data: dict) -> int | None:
    value = data.get("fundingTime") or data.get("nextFundingTime") or data.get("timePoint")
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < 10_000_000_000:
        numeric *= 1000
    return int(numeric)


def _iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _interval_hours(data: dict) -> float | None:
    value = _float_value(data, "currentGranularity")
    if value is None:
        value = _float_value(data, "newGranularity")
    if value is None:
        value = _float_value(data, "granularity")
    if value is None:
        return None
    return value / 1000 / 60 / 60


def _contract_to_base(symbol: str) -> str:
    standard = normalise_symbol(symbol)
    base, _ = split_standard_symbol(standard)
    return base


def _spot_usdt_symbol_lookup(client: KucoinPublicClient) -> dict[str, str]:
    lookup = {}
    for item in client.get_spot_symbols():
        if item.get("quoteCurrency") != "USDT":
            continue
        if str(item.get("enableTrading", "true")).lower() == "false":
            continue
        symbol = item.get("symbol")
        if not symbol:
            continue
        lookup[normalise_symbol(str(symbol))] = str(symbol)
    return lookup


def load_funding_rows() -> list[dict]:
    client = KucoinPublicClient()
    spot_symbols = _spot_usdt_symbol_lookup(client)
    contracts = [
        contract
        for contract in client.get_active_contracts()
        if contract.get("symbol")
        and contract.get("quoteCurrency") == "USDT"
        and contract.get("status") == "Open"
        and normalise_symbol(str(contract.get("symbol"))) in spot_symbols
    ]
    contracts.sort(key=lambda item: str(item.get("symbol")))

    def parse(contract: dict) -> dict:
        perp_symbol = str(contract["symbol"])
        standard_symbol = normalise_symbol(perp_symbol)
        funding_rate = _float_value(contract, "fundingFeeRate")
        funding_time = _funding_time_ms({
            "fundingTime": contract.get("nextFundingRateDateTime"),
        })
        return {
            "base": _contract_to_base(perp_symbol),
            "perpSymbol": perp_symbol,
            "spotSymbol": spot_symbols[standard_symbol],
            "fundingSymbol": str(contract.get("fundingRateSymbol") or ""),
            "nextFundingRateDecimal": funding_rate,
            "nextFundingRatePct": _to_pct(funding_rate),
            "fundingTimeMs": funding_time,
            "fundingTimeUtc": _iso_from_ms(funding_time),
            "intervalHours": _interval_hours({
                "currentGranularity": contract.get("currentFundingRateGranularity")
                or contract.get("fundingRateGranularity")
            }),
            "fundingRateCapPct": _to_pct(_float_value(contract, "fundingRateCap")),
            "fundingRateFloorPct": _to_pct(_float_value(contract, "fundingRateFloor")),
        }

    rows = [parse(contract) for contract in contracts]
    rows.sort(key=lambda item: (item["nextFundingRatePct"] is None, -(item["nextFundingRatePct"] or -999)))
    return rows


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _is_today(row: dict, field: str, now: datetime) -> bool:
    timestamp = parse_datetime(row.get(field))
    return timestamp is not None and timestamp.date() == now.astimezone(timezone.utc).date()


def _latest_opportunity_file(config: KucoinBasisConfig) -> Path | None:
    files = sorted(config.opportunities_dir.glob("kucoin_basis_opportunities_*.csv"))
    return files[-1] if files else None


def _latest_opportunity_rows(config: KucoinBasisConfig) -> tuple[Path | None, list[dict]]:
    path = _latest_opportunity_file(config)
    if path is None:
        return None, []
    rows = _load_csv(path)
    timestamps = [row.get("timestamp_utc", "") for row in rows if row.get("timestamp_utc")]
    if not timestamps:
        return path, []
    latest_timestamp = max(timestamps)
    latest_rows = [row for row in rows if row.get("timestamp_utc") == latest_timestamp]
    latest_rows.sort(key=lambda row: parse_float(row.get("expected_edge_pct"), -999) or -999, reverse=True)
    return path, latest_rows


def _shortlist_display_rows(rows: list[dict]) -> list[dict]:
    best_by_symbol_direction = {}
    for row in rows:
        key = (row.get("base", ""), row.get("direction", ""))
        expected_edge = parse_float(row.get("expected_edge_pct"), -999) or -999
        scenario_edge = parse_float(row.get("scenario_edge_pct"), expected_edge) or expected_edge
        rank = (
            row.get("decision") == "ENTER_CANDIDATE",
            expected_edge,
            scenario_edge,
            -(parse_float(row.get("notional_usd"), 0.0) or 0.0),
        )
        current = best_by_symbol_direction.get(key)
        if current is None or rank > current[0]:
            best_by_symbol_direction[key] = (rank, row)
    display_rows = [value[1] for value in best_by_symbol_direction.values()]
    display_rows.sort(
        key=lambda row: (
            row.get("decision") == "ENTER_CANDIDATE",
            parse_float(row.get("expected_edge_pct"), -999) or -999,
        ),
        reverse=True,
    )
    return display_rows


def _funding_benefit_pct(row: dict) -> float | None:
    funding = parse_float(row.get("funding_rate_pct"))
    if funding is None:
        return None
    if row.get("direction") == "SHORT_SPOT_LONG_PERP":
        return -funding
    return funding


def _basis_convergence_scenario(
    *,
    config: KucoinBasisConfig,
    direction: str,
    basis_pct: float | None,
    expected_edge_pct: float | None,
) -> tuple[float | None, float | None, float | None]:
    if basis_pct is None:
        return None, None, expected_edge_pct
    target_abs = config.basis_near_flat_exit_abs_pct
    if direction == "SHORT_SPOT_LONG_PERP":
        target = -target_abs
        raw_upside = max(0.0, target - basis_pct)
    else:
        target = target_abs
        raw_upside = max(0.0, basis_pct - target)
    upside = raw_upside * config.basis_convergence_haircut
    scenario_edge = None if expected_edge_pct is None else expected_edge_pct + upside
    return target, upside, scenario_edge


def _age_label(created_at: datetime | None, now: datetime) -> str:
    if created_at is None:
        return ""
    seconds = max(0, int((now - created_at).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _shortlist_item_from_row(row: dict, config: KucoinBasisConfig) -> dict:
    funding_benefit = _funding_benefit_pct(row)
    basis_pct = parse_float(row.get("basis_pct"))
    expected_edge_pct = parse_float(row.get("expected_edge_pct"))
    basis_target_pct, basis_convergence_upside_pct, scenario_edge_pct = _basis_convergence_scenario(
        config=config,
        direction=row.get("direction", ""),
        basis_pct=basis_pct,
        expected_edge_pct=expected_edge_pct,
    )
    return {
        "timestampUtc": row.get("timestamp_utc", ""),
        "base": row.get("base", ""),
        "direction": row.get("direction", ""),
        "spotSymbol": row.get("spot_symbol", ""),
        "perpSymbol": row.get("perp_symbol", ""),
        "notionalUsd": parse_float(row.get("notional_usd"), 0.0) or 0.0,
        "fundingRatePct": parse_float(row.get("funding_rate_pct")),
        "fundingBenefitPct": funding_benefit,
        "fundingTimeUtc": row.get("funding_time_utc", ""),
        "minutesToFunding": parse_float(row.get("minutes_to_funding")),
        "basisPct": basis_pct,
        "basisObservationCount": parse_float(row.get("basis_observation_count"), 0.0) or 0.0,
        "basisStatsReady": (parse_float(row.get("basis_observation_count"), 0.0) or 0.0)
        >= config.min_basis_observations_for_stats,
        "basisMeanPct": parse_float(row.get("basis_mean_pct")),
        "basisMedianPct": parse_float(row.get("basis_median_pct")),
        "basisStdPct": parse_float(row.get("basis_std_pct")),
        "basisZscore": parse_float(row.get("basis_zscore")),
        "basisPercentile": parse_float(row.get("basis_percentile")),
        "basisTrendPct": parse_float(row.get("basis_trend_pct")),
        "expectedEdgePct": expected_edge_pct,
        "basisTargetPct": parse_float(row.get("basis_target_pct"), basis_target_pct),
        "basisConvergenceUpsidePct": parse_float(
            row.get("basis_convergence_upside_pct"),
            basis_convergence_upside_pct,
        ),
        "scenarioEdgePct": parse_float(row.get("scenario_edge_pct"), scenario_edge_pct),
        "roundTripFillable": str(row.get("round_trip_fillable", "")).lower() == "true",
        "decision": row.get("decision", ""),
        "reason": row.get("reason", ""),
        "spotEntrySlippagePct": parse_float(row.get("spot_entry_slippage_pct")),
        "perpEntrySlippagePct": parse_float(row.get("perp_entry_slippage_pct")),
        "spotExitSlippagePct": parse_float(row.get("spot_exit_slippage_pct")),
        "perpExitSlippagePct": parse_float(row.get("perp_exit_slippage_pct")),
    }


def load_shortlist_payload(config: KucoinBasisConfig = DEFAULT_CONFIG) -> dict:
    path, rows = _latest_opportunity_rows(config)
    display_rows = _shortlist_display_rows(rows)
    items = [_shortlist_item_from_row(row, config) for row in display_rows]
    raw_items = [_shortlist_item_from_row(row, config) for row in rows]
    return {
        "observedAtUtc": datetime.now(timezone.utc).isoformat(),
        "sourceFile": str(path) if path else "",
        "rawRowCount": len(rows),
        "items": items,
        "rawItems": raw_items,
    }


def load_positions_payload(config: KucoinBasisConfig = DEFAULT_CONFIG) -> dict:
    store = PaperStore(config)
    now = datetime.now(timezone.utc)
    all_positions = store.load_all_positions()
    open_notional_by_base = Counter()
    for position in all_positions:
        if position.status == "OPEN":
            open_notional_by_base[position.base] += position.notional_usd
    decisions = _load_csv(store.decisions_path)
    latest_exit_reason = {}
    for decision in decisions:
        if decision.get("decision_type") == "EXIT":
            latest_exit_reason[decision.get("position_id", "")] = decision.get("reason", "")
    positions = []
    for position in all_positions:
        row = position.to_csv_row()
        if position.direction == "SHORT_SPOT_LONG_PERP":
            basis_improvement = position.current_basis_pct - position.entry_basis_pct
        else:
            basis_improvement = position.entry_basis_pct - position.current_basis_pct
        expected_funding_pnl = position.notional_usd * position.expected_funding_pct / 100
        funding_due = position.next_funding_time is not None and position.next_funding_time <= now
        row["basis_improvement_pct"] = f"{basis_improvement:.8f}"
        row["age_label"] = _age_label(position.created_at, now)
        row["expected_funding_pnl_usd"] = f"{expected_funding_pnl:.8f}"
        row["funding_due"] = str(funding_due)
        if funding_due:
            row["funding_state"] = "funding due"
        elif position.funding_events_captured > 0:
            row["funding_state"] = "funding captured"
        elif position.next_funding_time is not None:
            row["funding_state"] = "awaiting funding"
        else:
            row["funding_state"] = "no funding time"
        row["latest_exit_reason"] = latest_exit_reason.get(position.position_id, "")
        row["symbol_usage_usd"] = f"{open_notional_by_base.get(position.base, 0.0):.8f}"
        positions.append(row)
    positions.sort(key=lambda item: (item.get("status") != "OPEN", item.get("base", ""), item.get("direction", "")))
    fills = _load_csv(store.fills_path)
    fills.sort(key=lambda row: row.get("timestamp_utc", ""), reverse=True)
    return {
        "observedAtUtc": datetime.now(timezone.utc).isoformat(),
        "maxSymbolNotionalUsd": config.max_symbol_notional_usd,
        "positions": positions,
        "recentFills": fills[:25],
    }


def load_summary_payload(config: KucoinBasisConfig = DEFAULT_CONFIG) -> dict:
    store = PaperStore(config)
    now = datetime.now(timezone.utc)
    positions = store.load_all_positions()
    open_positions = [position for position in positions if position.status == "OPEN"]
    fills = _load_csv(store.fills_path)
    funding_events = _load_csv(store.funding_events_path)
    decisions = _load_csv(store.decisions_path)
    latest_path, latest_rows = _latest_opportunity_rows(config)

    realised_funding_today = sum(
        parse_float(row.get("funding_pnl_usd"), 0.0) or 0.0
        for row in funding_events
        if _is_today(row, "timestamp_utc", now)
    )
    realised_trade_today = sum(
        parse_float(row.get("realised_pnl_usd"), 0.0) or 0.0
        for row in fills
        if _is_today(row, "timestamp_utc", now)
    )
    entry_rejections = Counter(
        row.get("reason", "")
        for row in decisions
        if row.get("decision_type") == "ENTRY" and str(row.get("allowed")).lower() == "false"
    )
    exit_reasons = Counter(
        row.get("reason", "")
        for row in decisions
        if row.get("decision_type") == "EXIT"
    )
    return {
        "observedAtUtc": now.isoformat(),
        "sourceFile": str(latest_path) if latest_path else "",
        "openPositions": len(open_positions),
        "totalOpenNotionalUsd": sum(position.notional_usd for position in open_positions),
        "estimatedOpenPnlUsd": sum(position.estimated_net_pnl_usd for position in open_positions),
        "realisedFundingPnlTodayUsd": realised_funding_today,
        "realisedTradePnlTodayUsd": realised_trade_today,
        "realisedTotalPnlTodayUsd": realised_funding_today + realised_trade_today,
        "fundingEventsCapturedToday": sum(1 for row in funding_events if _is_today(row, "timestamp_utc", now)),
        "shortlistRows": len(latest_rows),
        "entryCandidates": sum(1 for row in latest_rows if row.get("decision") == "ENTER_CANDIDATE"),
        "entryRejections": entry_rejections.most_common(10),
        "exitReasons": exit_reasons.most_common(10),
    }


class FundingDashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(HTTPStatus.OK, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/funding":
            try:
                payload = {
                    "observedAtUtc": datetime.now(timezone.utc).isoformat(),
                    "items": load_funding_rows(),
                }
                self._send_json(HTTPStatus.OK, payload)
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if parsed.path == "/api/shortlist":
            try:
                self._send_json(HTTPStatus.OK, load_shortlist_payload())
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if parsed.path == "/api/positions":
            try:
                self._send_json(HTTPStatus.OK, load_positions_payload())
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if parsed.path == "/api/summary":
            try:
                self._send_json(HTTPStatus.OK, load_summary_payload())
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local KuCoin funding-rate dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), FundingDashboardHandler)
    print(f"KuCoin funding dashboard running at http://{args.host}:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
