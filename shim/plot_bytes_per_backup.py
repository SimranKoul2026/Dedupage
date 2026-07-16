"""Publication figure: incremental bytes uploaded per method, per workload.
Grouped bars, log y. Palette = validated dataviz categorical slots 1-6 (light)."""
import csv
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, FixedLocator, NullLocator

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "benchmark_results.csv"

# validated categorical palette (light), slots 1-6, kept in slot order for CVD
COL = {
    "page_dedup_delta": "#2a78d6",  # blue   (ours)
    "page_dedup":       "#1baf7a",  # aqua
    "litestream":       "#eda100",  # yellow
    "rsync":            "#008300",  # green
    "fastcdc":          "#4a3aa7",  # violet
    "full_copy":        "#e34948",  # red
}
LABEL = {
    "page_dedup_delta": "page-dedup+Δ (ours)",
    "page_dedup": "page-dedup",
    "litestream": "Litestream",
    "rsync": "rsync",
    "fastcdc": "FastCDC",
    "full_copy": "full-copy",
}
METHODS = list(COL.keys())  # bar order = color/slot order

# which snapshots are the realistic incremental intervals per workload
INC = {
    "AnkiDroid (phone, mixed)": ({"snap2", "snap3"}, "AnkiDroid\n(study session)"),
    "AntennaPod (phone)": ({"ap_snap1"}, "AntennaPod\n(phone, playback)"),
    "AntennaPod (tablet, Dimensity)": ({"tab_snap1"}, "AntennaPod\n(tablet, playback)"),
    "SYNTHETIC content-recurrence (A/B toggle)": ({"r1", "r2", "r3", "r4"}, "Content-recurrence\n(synthetic)"),
}

# ink / chrome tokens (light)
INK, INK2, MUTED, GRID, BASE, SURF = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"

# --- aggregate ---
rows = list(csv.DictReader(open(CSV)))
data = {w: {m: 0.0 for m in METHODS} for w in INC}
for r in rows:
    w = r["workload"]
    if w in INC and r["snapshot"] in INC[w][0]:
        for m in METHODS:
            v = float(r[m])
            if v >= 0:
                data[w][m] += v

order = list(INC.keys())
xlabels = [INC[w][1] for w in order]


def fmt_bytes(n, _=None):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return ("%.0f %s" % (n, u)) if n >= 10 or u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0
    return "%.0f TB" % n


plt.rcParams.update({"font.family": "sans-serif",
                     "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
                     "svg.fonttype": "none"})
fig, ax = plt.subplots(figsize=(9.2, 5.0), dpi=300)
fig.patch.set_facecolor(SURF); ax.set_facecolor(SURF)

nG, nM = len(order), len(METHODS)
group_w = 0.82
bw = group_w / nM
import numpy as np
xc = np.arange(nG)

for i, m in enumerate(METHODS):
    xs = xc - group_w / 2 + bw * (i + 0.5)
    ys = [max(data[w][m], 1) for w in order]  # floor at 1 B for log
    bars = ax.bar(xs, ys, width=bw * 0.86, color=COL[m], label=LABEL[m],
                  edgecolor=SURF, linewidth=0.8, zorder=3)
    for x, y in zip(xs, ys):
        ax.annotate(fmt_bytes(y), (x, y), xytext=(0, 2.5), textcoords="offset points",
                    ha="center", va="bottom", rotation=90, fontsize=5.6,
                    color=INK2, fontweight="medium")

ax.set_yscale("log")
ax.set_ylim(300, 2e7)


def fmt_axis(n, _=None):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1000:
            return "%g %s" % (n, u)
        n /= 1000.0
    return "%g TB" % n


ax.yaxis.set_major_locator(FixedLocator([1e3, 1e4, 1e5, 1e6, 1e7]))
ax.yaxis.set_minor_locator(NullLocator())
ax.yaxis.set_major_formatter(FuncFormatter(fmt_axis))
ax.set_ylabel("Incremental backup uploaded  (log scale)", fontsize=10, color=INK2)
ax.set_xticks(xc); ax.set_xticklabels(xlabels, fontsize=9, color=INK)
ax.set_title("Bytes uploaded per incremental backup, by method",
             fontsize=12.5, color=INK, fontweight="bold", pad=32, loc="left")

# recessive chrome
ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
ax.set_axisbelow(True)
for s in ("top", "right", "left"):
    ax.spines[s].set_visible(False)
ax.spines["bottom"].set_color(BASE)
ax.tick_params(axis="both", length=0, colors=MUTED, labelsize=8)
ax.tick_params(axis="x", colors=INK, labelsize=9)

leg = ax.legend(ncol=6, loc="lower left", bbox_to_anchor=(0, 1.005),
                frameon=False, fontsize=8.5, handlelength=1.1, columnspacing=1.3,
                handletextpad=0.5)
for t in leg.get_texts():
    t.set_color(INK2)

fig.text(0.008, -0.02,
         "Lower is better. All payloads zlib-compressed (rsync -z), apples-to-apples. "
         "Real devices: Galaxy S25+ / Tab S10+. Bulk-import intervals excluded (see text).",
         fontsize=6.8, color=MUTED, ha="left")

fig.tight_layout()
out_png = ROOT / "figure_bytes_per_backup.png"
out_pdf = ROOT / "figure_bytes_per_backup.pdf"
fig.savefig(out_png, bbox_inches="tight", facecolor=SURF)
fig.savefig(out_pdf, bbox_inches="tight", facecolor=SURF)
print("wrote", out_png.name, "and", out_pdf.name)
# also dump the aggregated numbers for the caption/table
for w in order:
    print(w.split(" (")[0], {m: fmt_bytes(data[w][m]) for m in METHODS})