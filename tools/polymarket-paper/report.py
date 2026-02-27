#!/usr/bin/env python3
"""
Polymarket Paper Trader — Premium PDF Report

Generates a polished dark-themed report with charts and tables.
Uses ReportLab for layout + matplotlib for chart rendering.
"""

import json
import io
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor, white, black, Color
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Flowable, Image, KeepTogether
)
from reportlab.lib.utils import ImageReader

# ─── Design Tokens ────────────────────────────────────────────────────
BG_DARK = HexColor('#0f1117')
BG_CARD = HexColor('#1a1d27')
BG_CARD_ALT = HexColor('#141722')
ACCENT = HexColor('#22d3ee')       # Cyan
ACCENT_GREEN = HexColor('#34d399')  # Emerald
ACCENT_RED = HexColor('#f87171')    # Rose
ACCENT_AMBER = HexColor('#fbbf24')  # Amber
ACCENT_BLUE = HexColor('#60a5fa')   # Blue
GRAY_400 = HexColor('#9ca3af')
GRAY_500 = HexColor('#6b7280')
GRAY_600 = HexColor('#4b5563')
GRAY_700 = HexColor('#374151')
GRAY_800 = HexColor('#1f2937')
WHITE = white
BLACK = black

STRATEGY_COLORS = {
    "momentum": '#60a5fa',  # Blue
    "spread": '#34d399',    # Green
    "fade": '#fbbf24',      # Amber
}
STRATEGY_COLORS_HEX = {k: HexColor(v) for k, v in STRATEGY_COLORS.items()}

W, H = A4
MARGIN = 1.8 * cm

LEDGER_DIR = Path(__file__).parent / "ledgers"
REPORT_DIR = Path(__file__).parent / "reports"
STRATEGIES = ["momentum", "spread", "fade"]


# ─── Styles ───────────────────────────────────────────────────────────
def make_styles():
    return {
        "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=28,
                                textColor=WHITE, leading=34, alignment=TA_LEFT),
        "subtitle": ParagraphStyle("subtitle", fontName="Helvetica", fontSize=12,
                                   textColor=GRAY_400, leading=16, alignment=TA_LEFT),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=16,
                             textColor=WHITE, leading=22, spaceBefore=18, spaceAfter=8),
        "h3": ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=12,
                             textColor=GRAY_400, leading=16, spaceBefore=12, spaceAfter=6,
                             textTransform="uppercase"),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=10,
                               textColor=GRAY_400, leading=14),
        "stat_big": ParagraphStyle("stat_big", fontName="Helvetica-Bold", fontSize=32,
                                   textColor=WHITE, leading=38, alignment=TA_CENTER),
        "stat_label": ParagraphStyle("stat_label", fontName="Helvetica", fontSize=9,
                                     textColor=GRAY_500, leading=12, alignment=TA_CENTER,
                                     textTransform="uppercase"),
        "stat_value": ParagraphStyle("stat_value", fontName="Helvetica-Bold", fontSize=14,
                                     textColor=WHITE, leading=18, alignment=TA_CENTER),
    }


# ─── Background Flowable ─────────────────────────────────────────────
class DarkBackground(Flowable):
    """Draws a dark background rect behind content."""
    def __init__(self, width, height, color=BG_DARK, radius=0):
        Flowable.__init__(self)
        self.width = width
        self.height = height
        self.color = color
        self.radius = radius

    def draw(self):
        self.canv.setFillColor(self.color)
        if self.radius:
            self.canv.roundRect(0, 0, self.width, self.height, self.radius, fill=1, stroke=0)
        else:
            self.canv.rect(0, 0, self.width, self.height, fill=1, stroke=0)


class CardBox(Flowable):
    """A rounded card with background color."""
    def __init__(self, width, height, color=BG_CARD):
        Flowable.__init__(self)
        self.width = width
        self.height = height
        self.color = color

    def draw(self):
        self.canv.setFillColor(self.color)
        self.canv.roundRect(0, 0, self.width, self.height, 8, fill=1, stroke=0)


class AccentLine(Flowable):
    """Horizontal accent line."""
    def __init__(self, width, color=ACCENT, thickness=2):
        Flowable.__init__(self)
        self.width = width
        self.line_color = color
        self.thickness = thickness
        self.height = thickness

    def draw(self):
        self.canv.setStrokeColor(self.line_color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, 0, self.width, 0)


class StatCard(Flowable):
    """Single stat card with value and label."""
    def __init__(self, value, label, color=WHITE, width=120, height=60):
        Flowable.__init__(self)
        self.value = value
        self.label = label
        self.color = color
        self.width = width
        self.height = height

    def draw(self):
        c = self.canv
        # Card background
        c.setFillColor(BG_CARD)
        c.roundRect(0, 0, self.width, self.height, 6, fill=1, stroke=0)
        # Value
        c.setFillColor(self.color)
        c.setFont("Helvetica-Bold", 20)
        c.drawCentredString(self.width / 2, self.height - 28, self.value)
        # Label
        c.setFillColor(GRAY_500)
        c.setFont("Helvetica", 8)
        c.drawCentredString(self.width / 2, 8, self.label.upper())


# ─── Data Loading ─────────────────────────────────────────────────────
def load_ledger(strategy):
    p = LEDGER_DIR / f"{strategy}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"strategy": strategy, "trades": [], "open_positions": [],
            "stats": {"total_pnl": 0, "wins": 0, "losses": 0, "total_trades": 0,
                      "gross_profit": 0, "gross_loss": 0, "total_fees": 0}}


def cumulative_pnl(trades):
    if not trades:
        return [], []
    sorted_t = sorted(trades, key=lambda t: t.get("resolved_at", t["timestamp"]))
    times, cum = [], []
    running = 0
    for t in sorted_t:
        ts = t.get("resolved_at", t["timestamp"])
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except:
            continue
        running += t.get("pnl", 0)
        times.append(dt)
        cum.append(running)
    return times, cum


# ─── Chart Generation ─────────────────────────────────────────────────
def set_chart_style(fig, ax):
    """Apply dark theme to matplotlib chart."""
    fig.patch.set_facecolor('#0f1117')
    ax.set_facecolor('#1a1d27')
    ax.tick_params(colors='#9ca3af', labelsize=8)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['bottom'].set_color('#374151')
    ax.spines['left'].set_color('#374151')
    ax.yaxis.label.set_color('#9ca3af')
    ax.xaxis.label.set_color('#9ca3af')
    ax.title.set_color('white')


def chart_to_image(fig, width_cm=16, height_cm=8):
    """Convert matplotlib figure to ReportLab Image flowable."""
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor(), edgecolor='none')
    plt.close(fig)
    buf.seek(0)
    img = Image(buf, width=width_cm * cm, height=height_cm * cm)
    return img


def make_pnl_chart(ledgers):
    """Cumulative P&L over time for all strategies."""
    fig, ax = plt.subplots(figsize=(8, 3.5))
    set_chart_style(fig, ax)

    has_data = False
    for s in STRATEGIES:
        times, cum = cumulative_pnl(ledgers[s]["trades"])
        if times:
            color = STRATEGY_COLORS[s]
            ax.plot(times, cum, color=color, linewidth=2.5, label=s.capitalize(),
                    marker='o', markersize=4, markerfacecolor=color, markeredgecolor='none',
                    alpha=0.9)
            # Fill under curve
            ax.fill_between(times, cum, alpha=0.1, color=color)
            has_data = True

    ax.axhline(y=0, color='#374151', linewidth=0.8, linestyle='--', alpha=0.5)

    if has_data:
        ax.legend(facecolor='#1a1d27', edgecolor='#374151', labelcolor='#9ca3af',
                  fontsize=8, loc='upper left')
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    else:
        ax.text(0.5, 0.5, 'Awaiting first trades...', transform=ax.transAxes,
                ha='center', va='center', fontsize=14, color='#4b5563', style='italic')

    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'${x:+,.0f}'))
    ax.set_title('Cumulative P&L', fontsize=11, fontweight='bold', pad=10)
    ax.grid(axis='y', color='#374151', alpha=0.3, linewidth=0.5)

    fig.tight_layout()
    return chart_to_image(fig, 16, 7)


def make_bar_chart(ledgers):
    """Strategy comparison bar chart."""
    fig, axes = plt.subplots(1, 3, figsize=(8, 2.8))
    set_chart_style(fig, axes[0])

    # P&L bars
    ax = axes[0]
    set_chart_style(fig, ax)
    names = [s.capitalize() for s in STRATEGIES]
    pnls = [ledgers[s]["stats"]["total_pnl"] for s in STRATEGIES]
    colors = [STRATEGY_COLORS[s] for s in STRATEGIES]
    bar_colors = [STRATEGY_COLORS[s] if p >= 0 else '#f87171' for s, p in zip(STRATEGIES, pnls)]
    bars = ax.bar(names, pnls, color=bar_colors, width=0.6, edgecolor='none', alpha=0.85)
    for bar, pnl in zip(bars, pnls):
        y = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, y + (2 if y >= 0 else -8),
                f'${pnl:+.0f}', ha='center', va='bottom' if y >= 0 else 'top',
                fontsize=9, fontweight='bold', color='white')
    ax.axhline(y=0, color='#374151', linewidth=0.5)
    ax.set_title('P&L', fontsize=10, fontweight='bold', color='white', pad=8)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'${x:,.0f}'))
    ax.grid(axis='y', color='#374151', alpha=0.3, linewidth=0.5)

    # Win rate
    ax = axes[1]
    set_chart_style(fig, ax)
    win_rates = []
    for s in STRATEGIES:
        st = ledgers[s]["stats"]
        wr = (st["wins"] / st["total_trades"] * 100) if st["total_trades"] > 0 else 0
        win_rates.append(wr)
    bars = ax.bar(names, win_rates, color=colors, width=0.6, edgecolor='none', alpha=0.85)
    ax.axhline(y=50, color='#f87171', linewidth=0.8, linestyle='--', alpha=0.4)
    for bar, wr, s in zip(bars, win_rates, STRATEGIES):
        tc = ledgers[s]["stats"]["total_trades"]
        label = f'{wr:.0f}%' if tc > 0 else 'N/A'
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 2,
                label, ha='center', va='bottom', fontsize=9, fontweight='bold', color='white')
    ax.set_title('Win Rate', fontsize=10, fontweight='bold', color='white', pad=8)
    ax.set_ylim(0, 100)
    ax.grid(axis='y', color='#374151', alpha=0.3, linewidth=0.5)

    # Trade count
    ax = axes[2]
    set_chart_style(fig, ax)
    counts = [ledgers[s]["stats"]["total_trades"] for s in STRATEGIES]
    bars = ax.bar(names, counts, color=colors, width=0.6, edgecolor='none', alpha=0.85)
    for bar, c in zip(bars, counts):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.3,
                str(c), ha='center', va='bottom', fontsize=9, fontweight='bold', color='white')
    ax.set_title('Trades', fontsize=10, fontweight='bold', color='white', pad=8)
    ax.grid(axis='y', color='#374151', alpha=0.3, linewidth=0.5)

    fig.tight_layout(pad=1.5)
    return chart_to_image(fig, 16, 5.5)


# ─── PDF Page Template ────────────────────────────────────────────────
def page_bg(canvas, doc):
    """Dark background + header/footer on every page."""
    canvas.saveState()
    canvas.setFillColor(BG_DARK)
    canvas.rect(0, 0, W, H, fill=1, stroke=0)

    # Top accent line
    canvas.setStrokeColor(ACCENT)
    canvas.setLineWidth(2)
    canvas.line(MARGIN, H - MARGIN + 8, W - MARGIN, H - MARGIN + 8)

    # Header
    canvas.setFillColor(GRAY_500)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(MARGIN, H - MARGIN + 14, "POLYMARKET PAPER TRADER")
    canvas.drawRightString(W - MARGIN, H - MARGIN + 14,
                           datetime.now(timezone.utc).strftime("%B %d, %Y"))

    # Footer
    canvas.setFillColor(GRAY_600)
    canvas.setFont("Helvetica", 7)
    canvas.drawCentredString(W / 2, MARGIN - 14,
                             f"Page {doc.page} · Generated {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    canvas.drawRightString(W - MARGIN, MARGIN - 14, "paper trading · not financial advice")

    canvas.restoreState()


# ─── Table Helpers ────────────────────────────────────────────────────
def styled_table(headers, rows, col_widths=None, highlight_col=None):
    """Create a dark-themed table."""
    content_width = W - 2 * MARGIN

    all_rows = [headers] + rows
    if col_widths is None:
        col_widths = [content_width / len(headers)] * len(headers)

    styles = make_styles()

    # Convert to Paragraphs for wrapping
    table_data = []
    for i, row in enumerate(all_rows):
        styled_row = []
        for j, cell in enumerate(row):
            if i == 0:
                # Header
                styled_row.append(Paragraph(f'<b>{cell}</b>',
                    ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=8,
                                   textColor=GRAY_400, alignment=TA_CENTER if j > 0 else TA_LEFT)))
            else:
                color = WHITE
                if highlight_col is not None and j == highlight_col:
                    val = str(cell)
                    if val.startswith('+') or val.startswith('$+') or val == 'W':
                        color = ACCENT_GREEN
                    elif val.startswith('-') or val.startswith('$-') or val == 'L':
                        color = ACCENT_RED
                styled_row.append(Paragraph(str(cell),
                    ParagraphStyle("td", fontName="Helvetica", fontSize=8,
                                   textColor=color, alignment=TA_CENTER if j > 0 else TA_LEFT)))
        table_data.append(styled_row)

    t = Table(table_data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), BG_CARD),
        ('BACKGROUND', (0, 1), (-1, -1), BG_CARD_ALT),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [BG_CARD_ALT, BG_CARD]),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('LINEBELOW', (0, 0), (-1, 0), 1, GRAY_700),
        ('LINEBELOW', (0, -1), (-1, -1), 1, GRAY_700),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ROUNDEDCORNERS', [6, 6, 6, 6]),
    ]))
    return t


def strategy_badge(strategy):
    """Return colored strategy name."""
    color = STRATEGY_COLORS.get(strategy, '#ffffff')
    return f'<font color="{color}"><b>{strategy.upper()}</b></font>'


# ─── Report Generation ────────────────────────────────────────────────
def generate_report(output_path=None):
    REPORT_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    if output_path is None:
        output_path = REPORT_DIR / f"report_{now.strftime('%Y%m%d_%H%M')}.pdf"
    else:
        output_path = Path(output_path)

    ledgers = {s: load_ledger(s) for s in STRATEGIES}
    styles = make_styles()
    content_width = W - 2 * MARGIN

    doc = SimpleDocTemplate(
        str(output_path), pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN + 8, bottomMargin=MARGIN + 4,
    )

    story = []

    # ── Title Block ──
    story.append(Spacer(1, 4))
    story.append(Paragraph("Polymarket Paper Trader", styles["title"]))
    story.append(Spacer(1, 4))

    # Subtitle with timestamp
    hour_str = now.strftime("%H:%M UTC · %B %d, %Y")
    story.append(Paragraph(f"Hourly Performance Report · {hour_str}", styles["subtitle"]))
    story.append(Spacer(1, 4))
    story.append(AccentLine(content_width, ACCENT, 2))
    story.append(Spacer(1, 16))

    # ── Combined Stats Row ──
    total_pnl = sum(ledgers[s]["stats"]["total_pnl"] for s in STRATEGIES)
    total_trades = sum(ledgers[s]["stats"]["total_trades"] for s in STRATEGIES)
    total_wins = sum(ledgers[s]["stats"]["wins"] for s in STRATEGIES)
    total_open = sum(len(ledgers[s]["open_positions"]) for s in STRATEGIES)
    total_fees = sum(ledgers[s]["stats"].get("total_fees", 0) for s in STRATEGIES)
    overall_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0

    pnl_color = ACCENT_GREEN if total_pnl >= 0 else ACCENT_RED
    pnl_str = f"${total_pnl:+,.2f}"

    # Stat cards as a table
    card_w = content_width / 5
    stat_data = [
        [StatCard(pnl_str, "Total P&L", pnl_color, card_w - 4, 56),
         StatCard(str(total_trades), "Trades", ACCENT_BLUE, card_w - 4, 56),
         StatCard(f"{overall_wr:.0f}%", "Win Rate",
                  ACCENT_GREEN if overall_wr >= 50 else ACCENT_RED, card_w - 4, 56),
         StatCard(str(total_open), "Open", ACCENT_AMBER, card_w - 4, 56),
         StatCard(f"${total_fees:.2f}", "Fees Paid", GRAY_400, card_w - 4, 56)],
    ]
    stat_table = Table(stat_data, colWidths=[card_w] * 5)
    stat_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2),
    ]))
    story.append(stat_table)
    story.append(Spacer(1, 20))

    # ── Charts ──
    story.append(Paragraph("Performance", styles["h3"]))
    story.append(make_pnl_chart(ledgers))
    story.append(Spacer(1, 12))
    story.append(make_bar_chart(ledgers))
    story.append(Spacer(1, 16))

    # ── Strategy Breakdown Table ──
    story.append(Paragraph("Strategy Breakdown", styles["h3"]))
    story.append(Spacer(1, 4))

    headers = ["Strategy", "P&L", "Trades", "Wins", "Losses", "Win %", "Avg P&L", "Fees"]
    rows = []
    for s in STRATEGIES:
        st = ledgers[s]["stats"]
        wr = f"{st['wins']/st['total_trades']*100:.0f}%" if st["total_trades"] > 0 else "—"
        avg = f"${st['total_pnl']/st['total_trades']:+.2f}" if st["total_trades"] > 0 else "—"
        pnl_display = f"${st['total_pnl']:+.2f}"
        rows.append([
            strategy_badge(s), pnl_display,
            str(st["total_trades"]), str(st["wins"]), str(st["losses"]),
            wr, avg, f"${st.get('total_fees', 0):.2f}"
        ])

    cw = content_width
    col_widths = [cw*0.16, cw*0.13, cw*0.10, cw*0.09, cw*0.09, cw*0.10, cw*0.13, cw*0.10]
    story.append(styled_table(headers, rows, col_widths, highlight_col=1))
    story.append(Spacer(1, 20))

    # ── Recent Trades ──
    all_resolved = []
    for s in STRATEGIES:
        for t in ledgers[s]["trades"]:
            t["_strategy"] = s
            all_resolved.append(t)
    all_resolved.sort(key=lambda t: t.get("resolved_at", t["timestamp"]), reverse=True)
    recent = all_resolved[:15]

    story.append(Paragraph("Recent Trades", styles["h3"]))
    story.append(Spacer(1, 4))

    if not recent:
        story.append(Spacer(1, 20))
        story.append(Paragraph(
            '<font color="#4b5563"><i>No trades resolved yet. '
            'Markets open ~14:00 UTC / 9:00 AM ET.</i></font>',
            ParagraphStyle("empty", fontName="Helvetica", fontSize=11,
                           textColor=GRAY_500, alignment=TA_CENTER)))
        story.append(Spacer(1, 20))
    else:
        headers = ["Time", "Strategy", "Market", "Side", "Entry", "Result", "P&L"]
        rows = []
        for t in recent:
            ts = t.get("resolved_at", t["timestamp"])
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                time_str = dt.strftime("%H:%M")
            except:
                time_str = ts[:16]

            mkt = t.get("market", "")
            if " - " in mkt:
                mkt = mkt.split(" - ", 1)[1][:28]

            result = "W" if t["outcome"] == "win" else "L"
            pnl_str = f"${t.get('pnl', 0):+.2f}"

            rows.append([
                time_str,
                strategy_badge(t["_strategy"]),
                mkt,
                t["side"],
                f"{t['entry_price']:.2f}",
                result,
                pnl_str,
            ])

        cw = content_width
        col_widths = [cw*0.08, cw*0.13, cw*0.30, cw*0.08, cw*0.09, cw*0.08, cw*0.12]
        story.append(styled_table(headers, rows, col_widths, highlight_col=6))

    story.append(Spacer(1, 16))

    # ── Open Positions ──
    all_open = []
    for s in STRATEGIES:
        for t in ledgers[s]["open_positions"]:
            t["_strategy"] = s
            all_open.append(t)

    if all_open:
        story.append(Paragraph(f"Open Positions ({len(all_open)})", styles["h3"]))
        story.append(Spacer(1, 4))

        headers = ["Strategy", "Market", "Side", "Entry", "Shares", "Cost", "EV"]
        rows = []
        for t in all_open:
            mkt = t.get("market", "")
            if " - " in mkt:
                mkt = mkt.split(" - ", 1)[1][:30]
            rows.append([
                strategy_badge(t["_strategy"]),
                mkt,
                t["side"],
                f"${t['entry_price']:.2f}",
                f"{t['shares']:.1f}",
                f"${t['cost']:.2f}",
                f"${t.get('net_ev', 0):+.2f}",
            ])

        cw = content_width
        col_widths = [cw*0.13, cw*0.30, cw*0.08, cw*0.10, cw*0.10, cw*0.10, cw*0.10]
        story.append(styled_table(headers, rows, col_widths, highlight_col=6))
        story.append(Spacer(1, 16))

    # ── Footer Note ──
    story.append(Spacer(1, 8))
    story.append(AccentLine(content_width, GRAY_700, 0.5))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        '<font color="#4b5563">Paper trading simulation · No real money at risk · '
        'Strategies: momentum (CEX signal), spread (both-side arb), fade (mean reversion) · '
        'Data: Polymarket Gamma API + Binance</font>',
        ParagraphStyle("footer", fontName="Helvetica", fontSize=7,
                       textColor=GRAY_600, alignment=TA_CENTER, leading=10)))

    # Build
    doc.build(story, onFirstPage=page_bg, onLaterPages=page_bg)
    print(f"📄 Report saved: {output_path}")
    return str(output_path)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else None
    path = generate_report(out)
    print(path)
