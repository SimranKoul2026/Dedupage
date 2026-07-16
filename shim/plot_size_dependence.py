"""Figure 2: size-dependence of page-dedup. Untouched-page fraction vs DB size,
three real cross-app measurements. Single series -> no legend (title names it),
direct-labeled points, log-x."""
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, FixedLocator, NullLocator

ROOT = Path(__file__).resolve().parent.parent

# (app, DB pages, DB size label, untouched %) — real measured points
PTS = [
    ("KOReader\nstats", 10, "40 KB", 60.0),
    ("AnkiDroid\nstudy", 500, "2.0 MB", 96.6),
    ("AntennaPod\nplayback", 813, "3.3 MB", 99.1),
]

BLUE, INK, INK2, MUTED, GRID, BASE, SURF = \
    "#2a78d6", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"

plt.rcParams.update({"font.family": "sans-serif",
                     "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"]})
fig, ax = plt.subplots(figsize=(8.2, 5.0), dpi=300)
fig.patch.set_facecolor(SURF); ax.set_facecolor(SURF)

xs = [p[1] for p in PTS]
ys = [p[3] for p in PTS]
ax.plot(xs, ys, "-", color=BLUE, linewidth=2, zorder=2)
ax.plot(xs, ys, "o", color=BLUE, markersize=10, markeredgecolor=SURF,
        markeredgewidth=1.5, zorder=3)

# direct labels: one clean staggered label per point (below-right / below-left / above-right)
lab = [
    ("%.0f%%\nKOReader · 10 pg · 40 KB" % 60.0, (16, -8), "left", "top"),
    ("%.1f%%\nAnkiDroid · 500 pg · 2.0 MB" % 96.6, (-16, -8), "right", "top"),
    ("%.1f%%\nAntennaPod · 813 pg · 3.3 MB" % 99.1, (16, 6), "left", "bottom"),
]
for (name, pg, sz, pct), (txt, (dx, dy), ha, va) in zip(PTS, lab):
    ax.annotate(txt, (pg, pct), xytext=(dx, dy), textcoords="offset points",
                ha=ha, va=va, fontsize=8.5, color=INK2, linespacing=1.35)

ax.set_xscale("log")
ax.set_xlim(6, 3200)
ax.xaxis.set_major_locator(FixedLocator([10, 100, 1000]))
ax.xaxis.set_minor_locator(NullLocator())
ax.xaxis.set_major_formatter(FuncFormatter(lambda n, _: "%g" % n))
ax.set_ylim(50, 106)
ax.yaxis.set_major_locator(FixedLocator([50, 60, 70, 80, 90, 100]))
ax.yaxis.set_major_formatter(FuncFormatter(lambda n, _: "%g%%" % n))

ax.set_xlabel("Database size  (pages, log scale)", fontsize=10, color=INK2)
ax.set_ylabel("Untouched pages per incremental change", fontsize=10, color=INK2)
ax.set_title("Page-dedup effectiveness rises with database size",
             fontsize=12.5, color=INK, fontweight="bold", pad=14, loc="left")

ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
ax.set_axisbelow(True)
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
for s in ("left", "bottom"):
    ax.spines[s].set_color(BASE)
ax.tick_params(length=0, colors=MUTED, labelsize=9)

ax.annotate("Small DBs: fixed overhead pages\n(header, freelist, b-tree roots)\nchurn on every write and dominate",
            (10, 60.0), xytext=(24, 66), textcoords="data", fontsize=7.6,
            color=MUTED, ha="left", va="center",
            arrowprops=dict(arrowstyle="-", color=GRID, lw=1))

fig.text(0.008, -0.02,
         "Three real cross-app measurements (higher is better). Different apps / change sizes, "
         "so this indicates a trend, not a controlled sweep (future work).",
         fontsize=6.8, color=MUTED, ha="left")

fig.tight_layout()
png = ROOT / "figure_size_dependence.png"
fig.savefig(png, bbox_inches="tight", facecolor=SURF)
fig.savefig(ROOT / "figure_size_dependence.pdf", bbox_inches="tight", facecolor=SURF)
print("wrote", png.name, "and .pdf")