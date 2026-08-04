"""
Renders the backtest trade log into a self-contained local HTML dashboard —
KPI summary, a per-trade P&L chart, and a sortable/filterable trade table.
No server needed; it's opened directly in a browser (main.py does this
automatically after a run).
"""
import json
import os
import webbrowser

import pandas as pd

import backtest as backtest_mod

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Scanner backtest report</title>
<style>
  :root {
    --bg: #f7f7f5; --surface: #ffffff; --border: #e3e2dc; --text: #171715;
    --text-secondary: #6b6a64; --text-muted: #9a998f; --green: #0f6e56;
    --green-bg: #e1f5ee; --gray: #6b6a64; --gray-bg: #f0efea; --red: #a32d2d;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #171715; --surface: #201f1d; --border: #35342f; --text: #f2f1ec;
      --text-secondary: #b8b7ae; --text-muted: #8a8980; --green: #5dcaa5;
      --green-bg: #0b3329; --gray-bg: #2a2a26; --red: #f09595;
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); margin: 0; padding: 2rem;
  }
  .wrap { max-width: 1000px; margin: 0 auto; }
  h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; }
  .subtitle { color: var(--text-secondary); font-size: 14px; margin: 0 0 1.5rem; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 1.5rem; }
  .kpi { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 1rem; }
  .kpi .label { font-size: 13px; color: var(--text-secondary); margin: 0 0 4px; }
  .kpi .value { font-size: 24px; font-weight: 600; margin: 0; }
  .kpi .value.pos { color: var(--green); }
  .kpi .value.neg { color: var(--red); }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 1.25rem; margin-bottom: 1.5rem; }
  .legend { display: flex; gap: 16px; font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
  .legend span { display: inline-flex; align-items: center; gap: 5px; }
  .dot { width: 9px; height: 9px; border-radius: 2px; display: inline-block; }
  .controls { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
  .controls select, .controls input {
    background: var(--surface); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 10px; font-size: 13px;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--border); }
  th { color: var(--text-muted); font-weight: 500; cursor: pointer; user-select: none; white-space: nowrap; }
  th:hover { color: var(--text); }
  td.num, th.num { text-align: right; }
  .pill { font-size: 12px; padding: 3px 9px; border-radius: 6px; white-space: nowrap; }
  .pill.target_hit { background: var(--green-bg); color: var(--green); }
  .pill.time_stop { background: var(--gray-bg); color: var(--text-secondary); }
  .pill.stop_loss { background: var(--gray-bg); color: var(--red); }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .empty { color: var(--text-muted); font-size: 14px; padding: 2rem 0; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Scanner backtest report</h1>
  <p class="subtitle">Generated __GENERATED_AT__ &middot; window __WINDOW__ &middot; __N_TRADES__ trades</p>

  <div class="kpis" id="kpis"></div>

  <div class="card">
    <div class="legend">
      <span><span class="dot" style="background:#199e70"></span>Target hit (__TARGET_MAX__%)</span>
      <span><span class="dot" style="background:#898781"></span>Time stop (__MAX_HOLD__ trading days)</span>
      <span><span class="dot" style="background:#e24b4a"></span>Stop loss</span>
    </div>
    <div style="position: relative; height: 280px;">
      <canvas id="pnlChart" role="img" aria-label="Bar chart of profit and loss percent for every trade in the backtest"></canvas>
    </div>
  </div>

  <div class="card">
    <div class="controls">
      <select id="filterReason">
        <option value="">All outcomes</option>
        <option value="target_hit">Target hit</option>
        <option value="time_stop">Time stop</option>
        <option value="stop_loss">Stop loss</option>
      </select>
      <input id="filterSymbol" type="text" placeholder="Filter by symbol...">
    </div>
    <div id="tableWrap"></div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const trades = __TRADES_JSON__;
const summary = __SUMMARY_JSON__;

function fmtPct(v) { return (v >= 0 ? '+' : '') + v.toFixed(1) + '%'; }

// --- KPI cards ---
const kpiDefs = [
  ['Total trades', summary.total_trades ?? 0, null],
  ['Win rate', (summary.win_rate_pct ?? 0) + '%', 'pos'],
  ['Hit 10% floor', (summary.hit_min_10pct_target_rate ?? 0) + '%', 'pos'],
  ['Avg P&L', fmtPct(summary.avg_pnl_pct ?? 0), (summary.avg_pnl_pct ?? 0) >= 0 ? 'pos' : 'neg'],
  ['Avg duration', (summary.avg_duration_days ?? 0) + ' days', null],
];
document.getElementById('kpis').innerHTML = kpiDefs.map(([label, value, cls]) =>
  `<div class="kpi"><p class="label">${label}</p><p class="value ${cls || ''}">${value}</p></div>`
).join('');

// --- Chart ---
if (trades.length) {
  const colorFor = (r) => r === 'target_hit' ? '#199e70' : r === 'stop_loss' ? '#e24b4a' : '#898781';
  new Chart(document.getElementById('pnlChart'), {
    type: 'bar',
    data: {
      labels: trades.map(t => t.symbol + ' ' + t.signal_date),
      datasets: [{
        data: trades.map(t => t.pnl_pct),
        backgroundColor: trades.map(t => colorFor(t.exit_reason)),
        borderRadius: 3,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (ctx) => fmtPct(ctx.raw) } }
      },
      scales: {
        y: { grid: { color: 'rgba(128,128,128,0.15)' }, ticks: { callback: (v) => v + '%' } },
        x: { grid: { display: false }, ticks: { display: trades.length <= 30, maxRotation: 60 } }
      }
    }
  });
} else {
  document.querySelector('.card').innerHTML = '<div class="empty">No trades in this backtest window.</div>';
}

// --- Table ---
let sortKey = 'signal_date', sortDir = -1;

function render() {
  const reasonFilter = document.getElementById('filterReason').value;
  const symbolFilter = document.getElementById('filterSymbol').value.toUpperCase();
  let rows = trades.filter(t =>
    (!reasonFilter || t.exit_reason === reasonFilter) &&
    (!symbolFilter || t.symbol.toUpperCase().includes(symbolFilter))
  );
  rows.sort((a, b) => {
    const av = a[sortKey], bv = b[sortKey];
    if (av < bv) return -1 * sortDir;
    if (av > bv) return 1 * sortDir;
    return 0;
  });

  if (!rows.length) {
    document.getElementById('tableWrap').innerHTML = '<div class="empty">No trades match this filter.</div>';
    return;
  }

  const reasonLabel = { target_hit: 'Target hit', time_stop: 'Time stop', stop_loss: 'Stop loss' };
  const cols = [
    ['symbol', 'Symbol'], ['signal_date', 'Signal'], ['entry_date', 'Entry date'],
    ['entry_price', 'Entry ₹'], ['exit_date', 'Exit date'], ['exit_price', 'Exit ₹'],
    ['pnl_pct', 'P&L'], ['duration_days', 'Days'], ['exit_reason', 'Outcome'],
  ];
  const numCols = new Set(['entry_price', 'exit_price', 'pnl_pct', 'duration_days']);

  let html = '<table><thead><tr>' + cols.map(([key, label]) =>
    `<th class="${numCols.has(key) ? 'num' : ''}" onclick="setSort('${key}')">${label}${sortKey === key ? (sortDir === 1 ? ' \\u2191' : ' \\u2193') : ''}</th>`
  ).join('') + '</tr></thead><tbody>';

  for (const t of rows) {
    html += '<tr>';
    html += `<td>${t.symbol}</td>`;
    html += `<td>${t.signal_date}</td>`;
    html += `<td>${t.entry_date}</td>`;
    html += `<td class="num">${t.entry_price.toFixed(2)}</td>`;
    html += `<td>${t.exit_date}</td>`;
    html += `<td class="num">${t.exit_price.toFixed(2)}</td>`;
    html += `<td class="num ${t.pnl_pct >= 0 ? 'pos' : 'neg'}">${fmtPct(t.pnl_pct)}</td>`;
    html += `<td class="num">${t.duration_days}</td>`;
    html += `<td><span class="pill ${t.exit_reason}">${reasonLabel[t.exit_reason] || t.exit_reason}</span></td>`;
    html += '</tr>';
  }
  html += '</tbody></table>';
  document.getElementById('tableWrap').innerHTML = html;
}

function setSort(key) {
  if (sortKey === key) { sortDir *= -1; } else { sortKey = key; sortDir = -1; }
  render();
}
window.setSort = setSort;

document.getElementById('filterReason').addEventListener('change', render);
document.getElementById('filterSymbol').addEventListener('input', render);
render();
</script>
</body>
</html>
"""


def generate_report(trades_csv_path: str, output_html_path: str = None, open_in_browser: bool = True) -> str:
    """Reads a trades CSV (as written by main.py) and writes an HTML dashboard
    next to it. Returns the path to the HTML file."""
    trades_df = pd.read_csv(trades_csv_path)
    summary = backtest_mod.summarize(trades_df)

    if output_html_path is None:
        output_html_path = os.path.splitext(trades_csv_path)[0] + "_report.html"

    if trades_df.empty:
        trades_records = []
        window_str = "n/a"
    else:
        trades_records = trades_df.to_dict(orient="records")
        window_str = f"{trades_df['signal_date'].min()} to {trades_df['signal_date'].max()}"

    import config
    html = (TEMPLATE
            .replace("__GENERATED_AT__", pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__WINDOW__", window_str)
            .replace("__N_TRADES__", str(len(trades_df)))
            .replace("__TARGET_MAX__", str(config.TARGET_MAX_PCT))
            .replace("__MAX_HOLD__", str(config.MAX_HOLDING_DAYS))
            .replace("__TRADES_JSON__", json.dumps(trades_records))
            .replace("__SUMMARY_JSON__", json.dumps(summary)))

    with open(output_html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Report written to {output_html_path}")
    if open_in_browser:
        webbrowser.open(f"file://{os.path.abspath(output_html_path)}")

    return output_html_path


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python report.py path/to/trades_<timestamp>.csv")
        sys.exit(1)
    generate_report(sys.argv[1])
