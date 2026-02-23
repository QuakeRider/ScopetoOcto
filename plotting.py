#!/usr/bin/env python3
"""
Plotting module for ScopetoOcto — generates diagnostic figures and summary
statistics from downloaded QuakeScope picks.

Run via:
    python workflow.py --plot ./quakescope_output

Station source of truth: derived exclusively from pick files (never from
station_metadata.csv, which contains stations that may have no picks).
Station coordinates are recovered from pyocto_stations.csv via the CRS
stored in crs_info.json.

Output layout (inside <output_dir>/):
    plots/
        per_station_figures.pdf          — all station figures in one PDF
        stations/<NET_STA_>.png          — individual station PNGs
        summary/
            summary_fig1_geographic.png/pdf
            summary_fig3_temporal_stats.png/pdf
            heatmaps.pdf                 — all heatmaps in one PDF
            heatmap_*.png                — individual heatmap PNGs
    summaries/
        station_summary.csv
        network_summary.csv
        summary_report.txt

Author: Grant
"""

import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")  # Non-interactive — must come before pyplot import
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.colors import LogNorm

try:
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    HAS_CARTOPY = True
except ImportError:
    HAS_CARTOPY = False
    logging.getLogger(__name__).warning(
        "cartopy not installed — geographic plots will use plain scatter plots."
    )

try:
    # Ensure the PROJ data directory is discoverable before importing pyproj.
    # In conda environments the database lives at $PREFIX/share/proj, but
    # pyproj may not find it automatically when PROJ_DATA/PROJ_LIB are unset.
    _proj_candidates = [
        Path(sys.prefix) / "share" / "proj",
        Path(sys.prefix) / "Library" / "share" / "proj",  # Windows conda
    ]
    for _p in _proj_candidates:
        if _p.exists():
            os.environ.setdefault("PROJ_DATA", str(_p))
            os.environ.setdefault("PROJ_LIB",  str(_p))
            break

    from pyproj import Transformer, CRS as ProjCRS
    HAS_PYPROJ = True
except ImportError:
    HAS_PYPROJ = False
    logging.getLogger(__name__).warning(
        "pyproj not installed — station coordinates will use a linear approximation."
    )

logger = logging.getLogger(__name__)

# Colour palette (blue / red-orange / green)
_P_COLOR   = "#2166ac"
_S_COLOR   = "#d6604d"
_TOT_COLOR = "#4d9221"


# ---------------------------------------------------------------------------
# Main plotter class
# ---------------------------------------------------------------------------

class PicksPlotter:
    """Load downloaded pick files and generate all diagnostic figures."""

    def __init__(self, output_dir: str):
        self.output_dir        = Path(output_dir)
        self.picks_dir         = self.output_dir / "picks"
        self.plots_dir         = self.output_dir / "plots"
        self.station_plots_dir = self.plots_dir / "stations"
        self.summary_plots_dir = self.plots_dir / "summary"
        self.summaries_dir     = self.output_dir / "summaries"

        # Populated by load_*()
        self.all_picks:         pd.DataFrame = None
        self.station_picks:     dict         = {}   # station_id → DataFrame
        self.station_locations: dict         = {}   # station_id → (lat, lon, elev_km)
        self.run_config:        dict         = {}

        try:
            plt.style.use("seaborn-v0_8-whitegrid")
        except OSError:
            plt.style.use("seaborn-whitegrid")

    # ------------------------------------------------------------------
    # Directory setup
    # ------------------------------------------------------------------

    def _setup_directories(self):
        for d in [
            self.plots_dir, self.station_plots_dir,
            self.summary_plots_dir, self.summaries_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_run_config(self):
        p = self.output_dir / "run_config.json"
        if p.exists():
            with open(p) as f:
                self.run_config = json.load(f)

    def _load_station_locations(self):
        """Convert pyocto projected (km) coordinates back to geographic lat/lon."""
        pyocto_path = self.output_dir / "pyocto_stations.csv"
        crs_path    = self.output_dir / "crs_info.json"

        if not pyocto_path.exists():
            logger.warning("pyocto_stations.csv not found — geographic plots unavailable.")
            return

        pyocto_df = pd.read_csv(pyocto_path)
        crs_info  = {}
        if crs_path.exists():
            with open(crs_path) as f:
                crs_info = json.load(f)

        # Preferred path: pyproj CRS round-trip
        if HAS_PYPROJ and "crs_wkt" in crs_info:
            try:
                proj_crs    = ProjCRS.from_wkt(crs_info["crs_wkt"])
                # pyocto stores km; underlying CRS is in metres
                transformer = Transformer.from_crs(proj_crs, "EPSG:4326", always_xy=True)
                xs_m = pyocto_df["x"].values * 1_000.0
                ys_m = pyocto_df["y"].values * 1_000.0
                lons, lats = transformer.transform(xs_m, ys_m)
                for i, row in pyocto_df.iterrows():
                    self.station_locations[row["station"]] = (
                        float(lats[i]), float(lons[i]), float(row["z"])
                    )
                logger.info(
                    f"Loaded locations for {len(self.station_locations)} stations via pyproj."
                )
                return
            except Exception as exc:
                logger.warning(f"pyproj CRS conversion failed ({exc}); using linear fallback.")

        self._fallback_locations(pyocto_df, crs_info)

    def _fallback_locations(self, pyocto_df: pd.DataFrame, crs_info: dict):
        """Approximate lat/lon from projected km coordinates using the stored bounds."""
        bounds     = crs_info.get("bounds", {})
        center_lat = (bounds.get("min_lat", 0) + bounds.get("max_lat", 0)) / 2
        center_lon = (bounds.get("min_lon", 0) + bounds.get("max_lon", 0)) / 2
        cos_lat    = np.cos(np.radians(center_lat)) or 1.0
        for _, row in pyocto_df.iterrows():
            lat = center_lat + row["y"] / 111.0
            lon = center_lon + row["x"] / (111.0 * cos_lat)
            self.station_locations[row["station"]] = (lat, lon, float(row["z"]))
        logger.info(
            f"Loaded locations for {len(self.station_locations)} stations via linear approx."
        )

    @staticmethod
    def _read_date_dir(date_dir: Path):
        """Read all pick CSVs under a single date directory.

        Expects *date_dir* to sit at depth YYYY/MM/DD inside the picks
        directory.  The date string is reconstructed from the three path
        components so it is always in ``YYYY-MM-DD`` form regardless of
        directory depth.

        Returns a DataFrame with a ``_date_str`` column, or None if the
        directory contained no readable data.
        """
        date_str = f"{date_dir.parent.parent.name}-{date_dir.parent.name}-{date_dir.name}"
        chunks   = []
        for fp in sorted(date_dir.glob("*.csv")):
            try:
                df = pd.read_csv(fp)
                if not df.empty:
                    df["_date_str"] = date_str
                    chunks.append(df)
            except Exception as exc:
                logging.getLogger(__name__).warning(f"Skipping {fp}: {exc}")
        return pd.concat(chunks, ignore_index=True) if chunks else None

    def _load_all_picks(self):
        """Scan picks/YYYY/MM/DD/*.csv and build self.all_picks.

        Date directories (the leaf DD directories under YYYY/MM/) are
        processed in parallel via a ThreadPoolExecutor, which gives a large
        speed-up when there are hundreds of date directories each containing
        thousands of station CSV files.
        """
        if not self.picks_dir.exists():
            raise FileNotFoundError(f"Picks directory not found: {self.picks_dir}")

        # Discover leaf day-directories at picks_dir/YYYY/MM/DD that
        # contain at least one CSV file.
        date_dirs = sorted(
            d for d in self.picks_dir.glob("*/*/*")
            if d.is_dir() and any(d.glob("*.csv"))
        )
        if not date_dirs:
            raise FileNotFoundError(f"No pick CSV files found in {self.picks_dir}")

        n_files = sum(len(list(d.glob("*.csv"))) for d in date_dirs)
        logger.info(f"Reading {n_files:,} pick file(s) across {len(date_dirs)} date directories…")

        n_workers = min(32, os.cpu_count() or 4)
        logger.info(f"Loading picks with {n_workers} parallel workers…")

        chunks    = []
        done      = 0
        n_dirs    = len(date_dirs)
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = {executor.submit(self._read_date_dir, d): d for d in date_dirs}
            for fut in as_completed(futures):
                result = fut.result()
                if result is not None:
                    chunks.append(result)
                done += 1
                if done % max(1, n_dirs // 10) == 0 or done == n_dirs:
                    logger.info(f"  Loaded {done}/{n_dirs} date directories…")

        if not chunks:
            raise ValueError("All pick files were empty or unreadable.")

        self.all_picks = pd.concat(chunks, ignore_index=True)

        # Parse timestamps
        self.all_picks["datetime"] = pd.to_datetime(
            self.all_picks["time"], unit="s", utc=True
        )
        self.all_picks["date"] = pd.to_datetime(self.all_picks["_date_str"]).dt.date
        self.all_picks["hour"] = self.all_picks["datetime"].dt.hour
        self.all_picks.drop(columns=["_date_str"], inplace=True)

        # Decompose station identifier  (NET.STA.)
        self.all_picks["network"]      = self.all_picks["station"].str.split(".").str[0]
        self.all_picks["station_code"] = self.all_picks["station"].str.split(".").str[1]

        # Index by station for per-station iteration
        for sta, grp in self.all_picks.groupby("station"):
            self.station_picks[sta] = grp.reset_index(drop=True)

        logger.info(
            f"Loaded {len(self.all_picks):,} picks across "
            f"{len(self.station_picks)} station(s) in "
            f"{self.all_picks['network'].nunique()} network(s)."
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _full_date_range(picks: pd.DataFrame):
        """Return a complete array of dates from first to last pick date."""
        dates = sorted(picks["date"].unique())
        return pd.date_range(dates[0], dates[-1], freq="D").date

    @staticmethod
    def _tick_dates(dates, ax, max_ticks: int = 8):
        """Apply sensible date tick marks to an axes with integer x positions."""
        n    = len(dates)
        step = max(1, n // max_ticks)
        ax.set_xticks(range(0, n, step))
        ax.set_xticklabels(
            [str(dates[i]) for i in range(0, n, step)],
            rotation=45, ha="right", fontsize=7,
        )

    # ------------------------------------------------------------------
    # Per-station figure (8-panel)
    # ------------------------------------------------------------------

    def _station_figure(self, sta_id: str, picks: pd.DataFrame) -> plt.Figure:
        fig = plt.figure(figsize=(18, 22))
        fig.suptitle(f"Station: {sta_id}", fontsize=15, fontweight="bold", y=0.99)

        full_dates = self._full_date_range(picks)
        p = picks[picks["phase"] == "P"]
        s = picks[picks["phase"] == "S"]

        def daily(df):
            return df.groupby("date").size().reindex(full_dates, fill_value=0)

        dp = daily(p)
        ds = daily(s)
        dt = daily(picks)

        # ── Panel 1: Daily pick counts (grouped bar) ─────────────────────
        ax1 = fig.add_subplot(4, 2, 1)
        x = np.arange(len(full_dates))
        w = 0.28
        ax1.bar(x - w, dt, w, color=_TOT_COLOR, alpha=0.8, label="Total")
        ax1.bar(x,     dp, w, color=_P_COLOR,   alpha=0.8, label="P")
        ax1.bar(x + w, ds, w, color=_S_COLOR,   alpha=0.8, label="S")
        ax1.set_title("Daily Pick Counts")
        ax1.set_ylabel("Count")
        self._tick_dates(full_dates, ax1)
        ax1.legend(fontsize=8)

        # ── Panel 2: Daily P/S ratio ──────────────────────────────────────
        ax2 = fig.add_subplot(4, 2, 2)
        ratio = (dp / ds.replace(0, np.nan)).dropna()
        if not ratio.empty:
            # Map ratio index (dates) back to integer positions
            date_pos = {d: i for i, d in enumerate(full_dates)}
            rx = [date_pos[d] for d in ratio.index if d in date_pos]
            ax2.plot(rx, ratio.values, color="purple",
                     linewidth=1.5, marker="o", markersize=3)
            ax2.axhline(1.0, color="gray", linestyle="--", linewidth=0.8,
                        label="P:S = 1")
            ax2.legend(fontsize=8)
        ax2.set_title("Daily P/S Ratio")
        ax2.set_ylabel("P / S")
        self._tick_dates(full_dates, ax2)

        # ── Panel 3: Confidence distribution ─────────────────────────────
        ax3 = fig.add_subplot(4, 2, 3)
        bins = np.linspace(0, 1, 41)
        if not p.empty:
            ax3.hist(p["probability"], bins=bins, alpha=0.6,
                     color=_P_COLOR, label=f"P  (n={len(p):,})")
        if not s.empty:
            ax3.hist(s["probability"], bins=bins, alpha=0.6,
                     color=_S_COLOR, label=f"S  (n={len(s):,})")
        ax3.set_title("Pick Confidence Distribution")
        ax3.set_xlabel("Confidence")
        ax3.set_ylabel("Count")
        ax3.legend(fontsize=8)

        # ── Panel 4: Hourly pick-rate heatmap (hour × date) ───────────────
        ax4 = fig.add_subplot(4, 2, 4)
        hmat    = np.zeros((24, len(full_dates)))
        d_idx   = {d: i for i, d in enumerate(full_dates)}
        for (d, h), cnt in picks.groupby(["date", "hour"]).size().items():
            di = d_idx.get(d)
            if di is not None:
                hmat[h, di] = cnt
        vmax4 = hmat.max() or 1
        im4 = ax4.imshow(hmat, aspect="auto", origin="lower",
                          cmap="YlOrRd", interpolation="nearest",
                          vmin=0, vmax=vmax4)
        ax4.set_title("Hourly Pick Rate (all phases)")
        ax4.set_xlabel("Date")
        ax4.set_ylabel("Hour of Day (UTC)")
        ax4.set_yticks(range(0, 24, 4))
        self._tick_dates(full_dates, ax4)
        plt.colorbar(im4, ax=ax4, label="Count", shrink=0.85)

        # ── Panel 5: Cumulative picks ─────────────────────────────────────
        ax5 = fig.add_subplot(4, 2, 5)
        for df_ph, color, label in [
            (picks, _TOT_COLOR, "Total"),
            (p,     _P_COLOR,   "P"),
            (s,     _S_COLOR,   "S"),
        ]:
            if not df_ph.empty:
                srt = df_ph.sort_values("datetime")
                ax5.plot(srt["datetime"].values,
                         np.arange(1, len(srt) + 1),
                         color=color, linewidth=1.2, label=label)
        ax5.set_title("Cumulative Picks Over Time")
        ax5.set_ylabel("Cumulative Count")
        ax5.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        ax5.tick_params(axis="x", rotation=45)
        ax5.legend(fontsize=8)

        # ── Panel 6: Amplitude distribution (log scale) ───────────────────
        ax6 = fig.add_subplot(4, 2, 6)
        amp_series = [
            (p["amplitude"].dropna(), _P_COLOR, "P"),
            (s["amplitude"].dropna(), _S_COLOR, "S"),
        ]
        pos_all = pd.concat([a for a, *_ in amp_series])[
            lambda x: x > 0
        ] if any(len(a) for a, *_ in amp_series) else pd.Series(dtype=float)

        if not pos_all.empty:
            lo   = pos_all.quantile(0.01)
            hi   = pos_all.quantile(0.99)
            bins = np.logspace(np.log10(max(lo, 1e-12)),
                               np.log10(max(hi, 1e-11)), 40)
            for amp, color, label in amp_series:
                pos = amp[amp > 0]
                if not pos.empty:
                    ax6.hist(pos, bins=bins, alpha=0.6,
                             color=color, label=f"{label} (n={len(pos):,})")
            ax6.set_xscale("log")
            ax6.set_title("Amplitude Distribution (log scale)")
            ax6.set_xlabel("Amplitude")
            ax6.set_ylabel("Count")
            ax6.legend(fontsize=8)
        else:
            ax6.text(0.5, 0.5, "No amplitude data", ha="center", va="center",
                     transform=ax6.transAxes, fontsize=11, color="gray")
            ax6.set_title("Amplitude Distribution")
            ax6.axis("off")

        # ── Panel 7: Pick confidence scatter over time ────────────────────
        ax7 = fig.add_subplot(4, 2, 7)
        max_pts = 5_000
        for df_ph, color, label in [(p, _P_COLOR, "P"), (s, _S_COLOR, "S")]:
            if not df_ph.empty:
                smp = df_ph.sample(min(max_pts, len(df_ph)), random_state=42)
                ax7.scatter(smp["datetime"], smp["probability"],
                            c=color, alpha=0.15, s=2, label=label,
                            rasterized=True)
        ax7.set_title("Pick Confidence Over Time")
        ax7.set_ylabel("Confidence")
        ax7.set_ylim(0, 1)
        ax7.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        ax7.tick_params(axis="x", rotation=45)
        ax7.legend(fontsize=8, markerscale=6)

        # ── Panel 8: Summary statistics text ─────────────────────────────
        ax8 = fig.add_subplot(4, 2, 8)
        ax8.axis("off")
        dates_active = sorted(picks["date"].unique())
        n_active = len(dates_active)
        n_span   = len(full_dates)
        pct      = 100 * n_active / n_span if n_span else 0
        ps_r     = len(p) / len(s) if len(s) > 0 else float("nan")
        mp       = p["probability"].mean() if not p.empty else float("nan")
        ms       = s["probability"].mean() if not s.empty else float("nan")

        loc_str = ""
        if sta_id in self.station_locations:
            la, lo, z_km = self.station_locations[sta_id]
            elev_m = -z_km * 1_000
            loc_str = f"\nLat: {la:.4f}°  Lon: {lo:.4f}°  Elev: {elev_m:.0f} m"

        txt = (
            f"Station:      {sta_id}{loc_str}\n\n"
            f"Total picks:  {len(picks):>10,}\n"
            f"  P picks:    {len(p):>10,}\n"
            f"  S picks:    {len(s):>10,}\n"
            f"P/S ratio:    {ps_r:>10.3f}\n\n"
            f"Active days:  {n_active:>3} / {n_span:<3}  ({pct:.1f}%)\n"
            f"First pick:   {dates_active[0]}\n"
            f"Last pick:    {dates_active[-1]}\n\n"
            f"Mean conf P:  {mp:.3f}\n"
            f"Mean conf S:  {ms:.3f}"
        )
        ax8.text(0.05, 0.95, txt, transform=ax8.transAxes, fontsize=9.5,
                 va="top", fontfamily="monospace",
                 bbox=dict(boxstyle="round", facecolor="lightyellow", alpha=0.9))
        ax8.set_title("Station Summary")

        fig.tight_layout(rect=[0, 0, 1, 0.985])
        return fig

    def _generate_per_station_figures(self):
        logger.info(f"Generating per-station figures ({len(self.station_picks)} stations)…")
        pdf_path = self.plots_dir / "per_station_figures.pdf"
        with PdfPages(pdf_path) as pdf:
            for sta_id in sorted(self.station_picks):
                picks = self.station_picks[sta_id]
                logger.debug(f"  {sta_id}  ({len(picks):,} picks)")
                try:
                    fig = self._station_figure(sta_id, picks)
                    safe = sta_id.replace(".", "_").strip("_")
                    network = sta_id.split(".")[0]
                    net_dir = self.station_plots_dir / network
                    net_dir.mkdir(parents=True, exist_ok=True)
                    fig.savefig(
                        net_dir / f"{safe}.png",
                        dpi=150, bbox_inches="tight",
                    )
                    pdf.savefig(fig, bbox_inches="tight")
                    plt.close(fig)
                except Exception as exc:
                    logger.error(f"Failed to plot {sta_id}: {exc}")
                    plt.close("all")
        logger.info(f"Per-station PDF → {pdf_path}")

    # ------------------------------------------------------------------
    # Summary Figure 1 — Geographic overview (2×2)
    # ------------------------------------------------------------------

    def _summary_fig1_geographic(self) -> plt.Figure:
        fig = plt.figure(figsize=(20, 16))
        fig.suptitle("Geographic Summary", fontsize=16, fontweight="bold")

        sta_counts = self.all_picks.groupby("station").size()
        sta_ids, lats, lons, counts = [], [], [], []
        for sid, cnt in sta_counts.items():
            if sid in self.station_locations:
                la, lo, _ = self.station_locations[sid]
                sta_ids.append(sid)
                lats.append(la)
                lons.append(lo)
                counts.append(cnt)

        lats   = np.array(lats,   dtype=float)
        lons   = np.array(lons,   dtype=float)
        counts = np.array(counts, dtype=float)

        # Map extent
        if len(lats):
            lat_pad = max(0.5, (lats.max() - lats.min()) * 0.12)
            lon_pad = max(0.5, (lons.max() - lons.min()) * 0.12)
            extent  = [lons.min() - lon_pad, lons.max() + lon_pad,
                       lats.min() - lat_pad, lats.max() + lat_pad]
        else:
            extent = [-180, 180, -90, 90]

        norm_map = (
            LogNorm(vmin=max(1, counts.min()), vmax=max(2, counts.max()))
            if len(counts) else None
        )

        # ── Panel 1: Station map coloured by pick count ───────────────────
        if HAS_CARTOPY:
            ax1 = fig.add_subplot(2, 2, 1, projection=ccrs.PlateCarree())
            ax1.set_extent(extent, crs=ccrs.PlateCarree())
            ax1.add_feature(cfeature.LAND,      facecolor="#f0f0f0")
            ax1.add_feature(cfeature.OCEAN,     facecolor="#c6e2f5")
            ax1.add_feature(cfeature.COASTLINE, linewidth=0.8)
            ax1.add_feature(cfeature.BORDERS,   linewidth=0.5, linestyle=":")
            ax1.add_feature(cfeature.STATES,    linewidth=0.3, linestyle=":")
            if len(lats):
                sc = ax1.scatter(
                    lons, lats, c=counts,
                    s=40,           # static marker size
                    cmap="YlOrRd", norm=norm_map,
                    transform=ccrs.PlateCarree(),
                    zorder=5, edgecolors="k", linewidths=0.3,
                )
                plt.colorbar(sc, ax=ax1, label="Total Picks (log scale)", shrink=0.7)
            ax1.gridlines(draw_labels=True, linewidth=0.3, alpha=0.5)
        else:
            ax1 = fig.add_subplot(2, 2, 1)
            if len(lats):
                sc = ax1.scatter(lons, lats, c=counts, s=40,
                                 cmap="YlOrRd", norm=norm_map,
                                 edgecolors="k", linewidths=0.3)
                plt.colorbar(sc, ax=ax1, label="Total Picks (log scale)", shrink=0.7)
            ax1.set_xlabel("Longitude")
            ax1.set_ylabel("Latitude")
        ax1.set_title("Station Map — Coloured by Pick Count")

        # ── Panel 2: Station active-day bar chart (top 40) ────────────────
        ax2 = fig.add_subplot(2, 2, 2)
        active_days = (
            self.all_picks.groupby("station")["date"]
            .nunique()
            .sort_values(ascending=False)
        )
        top_n  = min(40, len(active_days))
        colors = plt.cm.viridis(np.linspace(0.2, 0.9, top_n))[::-1]
        ax2.barh(range(top_n), active_days.values[:top_n], color=colors)
        ax2.set_yticks(range(top_n))
        ax2.set_yticklabels(active_days.index[:top_n], fontsize=6)
        ax2.set_xlabel("Days with Picks")
        ax2.set_title(f"Station Active Days (top {top_n})")
        ax2.invert_yaxis()

        # ── Panel 3: Geographic pick-density hexbin ───────────────────────
        if len(lats) > 1:
            # Expand each station position by its pick count for density weighting
            int_counts = np.maximum(counts, 1).astype(int)
            exp_lons   = np.repeat(lons, int_counts)
            exp_lats   = np.repeat(lats, int_counts)

            if HAS_CARTOPY:
                ax3 = fig.add_subplot(2, 2, 3, projection=ccrs.PlateCarree())
                ax3.set_extent(extent, crs=ccrs.PlateCarree())
                ax3.add_feature(cfeature.LAND,      facecolor="#f0f0f0")
                ax3.add_feature(cfeature.OCEAN,     facecolor="#c6e2f5")
                ax3.add_feature(cfeature.COASTLINE, linewidth=0.8)
                ax3.add_feature(cfeature.STATES,    linewidth=0.3, linestyle=":")
                hb = ax3.hexbin(
                    exp_lons, exp_lats,
                    gridsize=30, cmap="hot_r", mincnt=1, norm=LogNorm(),
                    transform=ccrs.PlateCarree(), zorder=5,
                )
                plt.colorbar(hb, ax=ax3, label="Pick Density (log)", shrink=0.7)
                ax3.gridlines(draw_labels=True, linewidth=0.3, alpha=0.5)
            else:
                ax3 = fig.add_subplot(2, 2, 3)
                hb = ax3.hexbin(exp_lons, exp_lats, gridsize=30,
                                cmap="hot_r", mincnt=1, norm=LogNorm())
                plt.colorbar(hb, ax=ax3, label="Pick Density (log)", shrink=0.7)
                ax3.set_xlabel("Longitude")
                ax3.set_ylabel("Latitude")
        else:
            ax3 = fig.add_subplot(2, 2, 3)
            ax3.text(0.5, 0.5, "Insufficient location data for density plot",
                     ha="center", va="center", transform=ax3.transAxes, color="gray")
            ax3.axis("off")
        ax3.set_title("Geographic Pick Density Heatmap")

        # ── Panel 4: Pick count distribution across stations ──────────────
        ax4 = fig.add_subplot(2, 2, 4)
        ax4.hist(sta_counts.values, bins=50, color="steelblue",
                 alpha=0.8, edgecolor="white", log=True)
        ax4.axvline(sta_counts.median(), color="red", linestyle="--", linewidth=1.5,
                    label=f"Median ({sta_counts.median():.0f})")
        ax4.axvline(sta_counts.mean(), color="orange", linestyle="--", linewidth=1.5,
                    label=f"Mean ({sta_counts.mean():.0f})")
        ax4.set_xlabel("Total Picks per Station")
        ax4.set_ylabel("Station Count (log)")
        ax4.set_title("Pick Count Distribution Across Stations")
        ax4.legend(fontsize=9)

        fig.tight_layout(rect=[0, 0, 1, 0.96])
        return fig

    # ------------------------------------------------------------------
    # Summary Figure 2 — Heatmaps
    # ------------------------------------------------------------------

    def _network_heatmap_fig(
        self,
        network: str,
        net_picks: pd.DataFrame,
        year: int = None,
    ) -> plt.Figure:
        """Picks-per-station-per-day heatmap for a single network (or one year)."""
        data         = net_picks
        title_suffix = ""
        if year is not None:
            data         = data[data["datetime"].dt.year == year]
            title_suffix = f" — {year}"

        stations = sorted(data["station"].unique())
        dates    = sorted(data["date"].unique())
        if not stations or not dates:
            return None

        matrix = np.zeros((len(stations), len(dates)))
        s_idx  = {s: i for i, s in enumerate(stations)}
        d_idx  = {d: i for i, d in enumerate(dates)}
        for (sta, d), cnt in data.groupby(["station", "date"]).size().items():
            matrix[s_idx[sta], d_idx[d]] = cnt

        fig_h = max(6.0,  min(60.0,  0.22 * len(stations) + 3.0))
        fig_w = max(14.0, min(60.0,  0.08 * len(dates)    + 4.0))
        fig, ax = plt.subplots(figsize=(fig_w, fig_h))

        vmax = matrix.max() or 1
        im = ax.imshow(matrix, aspect="auto", cmap="YlOrRd",
                       interpolation="nearest",
                       norm=LogNorm(vmin=1, vmax=vmax))
        plt.colorbar(im, ax=ax, label="Picks per Day (log)", shrink=0.8)

        ax.set_yticks(range(len(stations)))
        lbl_sz = max(4, min(9, int(200 / len(stations))))
        ax.set_yticklabels([s.split(".")[1] for s in stations], fontsize=lbl_sz)
        self._tick_dates(dates, ax, max_ticks=20)
        ax.set_xlabel("Date")
        ax.set_ylabel("Station")
        ax.set_title(
            f"Network {network} — Picks per Station per Day{title_suffix}"
        )
        fig.tight_layout()
        return fig

    def _network_aggregated_heatmap(self) -> plt.Figure:
        """One row per network — total picks across all stations per day."""
        networks = sorted(self.all_picks["network"].unique())
        dates    = sorted(self.all_picks["date"].unique())

        matrix = np.zeros((len(networks), len(dates)))
        n_idx  = {n: i for i, n in enumerate(networks)}
        d_idx  = {d: i for i, d in enumerate(dates)}
        for (net, d), cnt in self.all_picks.groupby(["network", "date"]).size().items():
            matrix[n_idx[net], d_idx[d]] = cnt

        fig_h = max(4.0, min(40.0, len(networks) * 0.45 + 3.0))
        fig_w = max(14.0, min(60.0, 0.08 * len(dates) + 4.0))
        fig, ax = plt.subplots(figsize=(fig_w, fig_h))

        vmax = matrix.max() or 1
        im = ax.imshow(matrix, aspect="auto", cmap="YlOrRd",
                       interpolation="nearest",
                       norm=LogNorm(vmin=1, vmax=vmax))
        plt.colorbar(im, ax=ax, label="Total Picks per Day (log)", shrink=0.8)
        ax.set_yticks(range(len(networks)))
        ax.set_yticklabels(networks, fontsize=9)
        self._tick_dates(dates, ax, max_ticks=20)
        ax.set_xlabel("Date")
        ax.set_ylabel("Network")
        ax.set_title("All Networks — Total Picks per Day (Network-Aggregated)")
        fig.tight_layout()
        return fig

    def _all_station_heatmap(self) -> plt.Figure:
        """Combined heatmap with every station, sorted by network then pick count."""
        sta_totals = self.all_picks.groupby("station").size().rename("total")
        sta_net    = self.all_picks.groupby("station")["network"].first()
        sta_info   = pd.concat([sta_net, sta_totals], axis=1)
        sta_info   = sta_info.sort_values(["network", "total"], ascending=[True, False])
        stations   = sta_info.index.tolist()

        dates  = sorted(self.all_picks["date"].unique())
        matrix = np.zeros((len(stations), len(dates)))
        s_idx  = {s: i for i, s in enumerate(stations)}
        d_idx  = {d: i for i, d in enumerate(dates)}
        for (sta, d), cnt in self.all_picks.groupby(["station", "date"]).size().items():
            if sta in s_idx and d in d_idx:
                matrix[s_idx[sta], d_idx[d]] = cnt

        # Compact rows (0.12 in/station), capped at 120 in
        fig_h = max(8.0,  min(120.0, 0.12 * len(stations) + 3.0))
        fig_w = max(14.0, min(60.0,  0.08 * len(dates)    + 4.0))
        fig, ax = plt.subplots(figsize=(fig_w, fig_h))

        vmax = matrix.max() or 1
        im = ax.imshow(matrix, aspect="auto", cmap="YlOrRd",
                       interpolation="nearest",
                       norm=LogNorm(vmin=1, vmax=vmax))
        plt.colorbar(im, ax=ax, label="Picks per Day (log)", shrink=0.8)

        if len(stations) <= 200:
            # Label individual stations
            ax.set_yticks(range(len(stations)))
            lbl_sz = max(3, min(8, int(500 / len(stations))))
            ax.set_yticklabels([s.split(".")[1] for s in stations], fontsize=lbl_sz)
        else:
            # Too many to label — draw white network-boundary lines + network labels
            ax.set_yticks([])
            current_net = None
            for i, sta in enumerate(stations):
                net = sta.split(".")[0]
                if net != current_net:
                    if i > 0:
                        ax.axhline(i - 0.5, color="white", linewidth=0.8, alpha=0.6)
                    ax.text(-0.5, i, net, ha="right", va="center",
                            fontsize=7, transform=ax.transData)
                    current_net = net

        self._tick_dates(dates, ax, max_ticks=20)
        ax.set_xlabel("Date")
        ax.set_ylabel("Station" if len(stations) <= 200 else "← Network boundary")
        ax.set_title(
            f"All Stations ({len(stations)}) — Picks per Day "
            f"(sorted by network, then pick count)"
        )
        fig.tight_layout()
        return fig

    def _generate_heatmap_figures(self):
        pdf_path = self.summary_plots_dir / "heatmaps.pdf"
        figures  = []

        # 1. Network-aggregated (always first — best overview)
        figures.append(("heatmap_all_networks_aggregated", self._network_aggregated_heatmap()))

        # 2. All-station combined (sorted by network/count)
        figures.append(("heatmap_all_stations_combined", self._all_station_heatmap()))

        # 3. Per-network (split by year when the download spans multiple years)
        for network, net_picks in self.all_picks.groupby("network"):
            years = sorted(net_picks["datetime"].dt.year.unique())
            if len(years) > 1:
                for yr in years:
                    fig = self._network_heatmap_fig(network, net_picks, year=yr)
                    if fig:
                        figures.append((f"heatmap_{network}_{yr}", fig))
            else:
                fig = self._network_heatmap_fig(network, net_picks)
                if fig:
                    figures.append((f"heatmap_{network}", fig))

        with PdfPages(pdf_path) as pdf:
            for name, fig in figures:
                if fig is None:
                    continue
                try:
                    fig.savefig(
                        self.summary_plots_dir / f"{name}.png",
                        dpi=150, bbox_inches="tight",
                    )
                    pdf.savefig(fig, bbox_inches="tight")
                    plt.close(fig)
                except Exception as exc:
                    logger.error(f"Failed to save heatmap '{name}': {exc}")
                    plt.close("all")

        logger.info(f"Heatmap PDF → {pdf_path}")

    # ------------------------------------------------------------------
    # Summary Figure 3 — Temporal & Statistical (3×2)
    # ------------------------------------------------------------------

    def _summary_fig3_temporal(self) -> plt.Figure:
        fig = plt.figure(figsize=(20, 18))
        fig.suptitle("Temporal & Statistical Summary", fontsize=16, fontweight="bold")

        p = self.all_picks[self.all_picks["phase"] == "P"]
        s = self.all_picks[self.all_picks["phase"] == "S"]

        all_dates = sorted(self.all_picks["date"].unique())
        dp = p.groupby("date").size().reindex(all_dates, fill_value=0)
        ds = s.groupby("date").size().reindex(all_dates, fill_value=0)
        dt = self.all_picks.groupby("date").size().reindex(all_dates, fill_value=0)
        x  = range(len(all_dates))

        # ── Panel 1: Total picks per day time series ──────────────────────
        ax1 = fig.add_subplot(3, 2, 1)
        ax1.fill_between(x, dt.values, alpha=0.15, color=_TOT_COLOR)
        ax1.plot(x, dt.values, color=_TOT_COLOR, lw=1.2, label="Total")
        ax1.plot(x, dp.values, color=_P_COLOR,   lw=1.0, label="P")
        ax1.plot(x, ds.values, color=_S_COLOR,   lw=1.0, label="S")
        ax1.set_title("Total Picks per Day (All Stations)")
        ax1.set_ylabel("Pick Count")
        self._tick_dates(all_dates, ax1)
        ax1.legend(fontsize=9)

        # ── Panel 2: Daily P/S ratio across all stations ──────────────────
        ax2 = fig.add_subplot(3, 2, 2)
        ratio = (dp / ds.replace(0, np.nan)).dropna()
        if not ratio.empty:
            date_pos = {d: i for i, d in enumerate(all_dates)}
            rx = [date_pos[d] for d in ratio.index if d in date_pos]
            ax2.fill_between(rx, ratio.values, 1.0, alpha=0.2, color="purple")
            ax2.plot(rx, ratio.values, color="purple", lw=1.2)
            ax2.axhline(1.0, color="gray", linestyle="--", lw=0.8)
        ax2.set_title("Daily P/S Ratio (All Stations)")
        ax2.set_ylabel("P / S")
        self._tick_dates(all_dates, ax2)

        # ── Panel 3: Overall confidence distribution ───────────────────────
        ax3 = fig.add_subplot(3, 2, 3)
        bins = np.linspace(0, 1, 51)
        if not p.empty:
            ax3.hist(p["probability"], bins=bins, alpha=0.6, color=_P_COLOR,
                     label=f"P  (n={len(p):,})", density=True)
        if not s.empty:
            ax3.hist(s["probability"], bins=bins, alpha=0.6, color=_S_COLOR,
                     label=f"S  (n={len(s):,})", density=True)
        ax3.set_title("Pick Confidence Distribution (All Stations)")
        ax3.set_xlabel("Confidence")
        ax3.set_ylabel("Density")
        ax3.legend(fontsize=9)

        # ── Panel 4: Network-level P and S totals ─────────────────────────
        ax4 = fig.add_subplot(3, 2, 4)
        net_p = p.groupby("network").size().sort_values(ascending=False)
        net_s = s.groupby("network").size().reindex(net_p.index, fill_value=0)
        xn = np.arange(len(net_p))
        w  = 0.4
        ax4.bar(xn - w / 2, net_p.values, w, color=_P_COLOR, alpha=0.85, label="P")
        ax4.bar(xn + w / 2, net_s.values, w, color=_S_COLOR, alpha=0.85, label="S")
        ax4.set_xticks(xn)
        ax4.set_xticklabels(net_p.index, rotation=45, ha="right", fontsize=8)
        ax4.set_title("Total P and S Picks by Network")
        ax4.set_ylabel("Pick Count")
        ax4.set_yscale("log")
        ax4.legend(fontsize=9)

        # ── Panel 5: Top stations by pick count (stacked P/S) ─────────────
        ax5 = fig.add_subplot(3, 2, 5)
        sta_total = self.all_picks.groupby("station").size().sort_values(ascending=False)
        top_n     = min(30, len(sta_total))
        top_stas  = sta_total.index[:top_n]
        top_p     = p.groupby("station").size().reindex(top_stas, fill_value=0)
        top_s     = s.groupby("station").size().reindex(top_stas, fill_value=0)
        xs = np.arange(top_n)
        ax5.bar(xs, top_p.values,
                color=_P_COLOR, alpha=0.85, label="P")
        ax5.bar(xs, top_s.values, bottom=top_p.values,
                color=_S_COLOR, alpha=0.85, label="S")
        ax5.set_xticks(xs)
        ax5.set_xticklabels(
            [st.split(".")[1] for st in top_stas], rotation=90, fontsize=6
        )
        ax5.set_title(f"Top {top_n} Stations by Pick Count (Stacked P/S)")
        ax5.set_ylabel("Pick Count")
        ax5.legend(fontsize=9)

        # ── Panel 6: Active stations per day ──────────────────────────────
        ax6 = fig.add_subplot(3, 2, 6)
        daily_sta = (
            self.all_picks.groupby("date")["station"]
            .nunique()
            .reindex(all_dates, fill_value=0)
        )
        ax6.fill_between(x, daily_sta.values, alpha=0.25, color="teal")
        ax6.plot(x, daily_sta.values, color="teal", lw=1.2)
        ax6.set_title("Active Stations per Day")
        ax6.set_ylabel("Station Count")
        self._tick_dates(all_dates, ax6)

        fig.tight_layout(rect=[0, 0, 1, 0.96])
        return fig

    # ------------------------------------------------------------------
    # Text / CSV summaries
    # ------------------------------------------------------------------

    def _generate_text_summaries(self):
        p_all = self.all_picks[self.all_picks["phase"] == "P"]
        s_all = self.all_picks[self.all_picks["phase"] == "S"]

        # ── Per-station CSV ───────────────────────────────────────────────
        rows = []
        for sta_id, picks in self.station_picks.items():
            p = picks[picks["phase"] == "P"]
            s = picks[picks["phase"] == "S"]
            dates_active = sorted(picks["date"].unique())
            full_dr      = self._full_date_range(picks)
            row = {
                "station":              sta_id,
                "network":              sta_id.split(".")[0],
                "station_code":         sta_id.split(".")[1],
                "total_picks":          len(picks),
                "p_picks":              len(p),
                "s_picks":              len(s),
                "ps_ratio":             len(p) / len(s) if len(s) else float("nan"),
                "mean_prob_p":          p["probability"].mean()  if not p.empty else float("nan"),
                "std_prob_p":           p["probability"].std()   if not p.empty else float("nan"),
                "mean_prob_s":          s["probability"].mean()  if not s.empty else float("nan"),
                "std_prob_s":           s["probability"].std()   if not s.empty else float("nan"),
                "mean_amp_p":           p["amplitude"].mean()    if (not p.empty and p["amplitude"].notna().any()) else float("nan"),
                "mean_amp_s":           s["amplitude"].mean()    if (not s.empty and s["amplitude"].notna().any()) else float("nan"),
                "active_days":          len(dates_active),
                "span_days":            len(full_dr),
                "uptime_pct":           100 * len(dates_active) / len(full_dr) if len(full_dr) > 0 else float("nan"),
                "picks_per_active_day": len(picks) / len(dates_active) if dates_active else float("nan"),
                "first_pick":           str(dates_active[0]),
                "last_pick":            str(dates_active[-1]),
            }
            if sta_id in self.station_locations:
                la, lo, z = self.station_locations[sta_id]
                row["latitude"]     = la
                row["longitude"]    = lo
                row["elevation_km"] = z
            rows.append(row)

        sta_df  = pd.DataFrame(rows).sort_values("total_picks", ascending=False)
        sta_csv = self.summaries_dir / "station_summary.csv"
        sta_df.to_csv(sta_csv, index=False, float_format="%.4f")
        logger.info(f"Station summary CSV → {sta_csv}")

        # ── Per-network CSV ───────────────────────────────────────────────
        net_rows = []
        for net, grp in self.all_picks.groupby("network"):
            gp        = grp[grp["phase"] == "P"]
            gs        = grp[grp["phase"] == "S"]
            sta_sizes = grp.groupby("station").size()
            net_rows.append({
                "network":                   net,
                "n_stations":                grp["station"].nunique(),
                "total_picks":               len(grp),
                "p_picks":                   len(gp),
                "s_picks":                   len(gs),
                "ps_ratio":                  len(gp) / len(gs) if len(gs) else float("nan"),
                "mean_prob_p":               gp["probability"].mean() if not gp.empty else float("nan"),
                "std_prob_p":                gp["probability"].std()  if not gp.empty else float("nan"),
                "mean_prob_s":               gs["probability"].mean() if not gs.empty else float("nan"),
                "std_prob_s":                gs["probability"].std()  if not gs.empty else float("nan"),
                "mean_picks_per_station":    sta_sizes.mean(),
                "median_picks_per_station":  sta_sizes.median(),
                "max_picks_station":         sta_sizes.max(),
                "min_picks_station":         sta_sizes.min(),
                "station_days":              grp.groupby(["station", "date"]).ngroups,
            })

        net_df  = pd.DataFrame(net_rows).sort_values("total_picks", ascending=False)
        net_csv = self.summaries_dir / "network_summary.csv"
        net_df.to_csv(net_csv, index=False, float_format="%.4f")
        logger.info(f"Network summary CSV → {net_csv}")

        self._write_text_report(sta_df, net_df)

    def _write_text_report(self, sta_df: pd.DataFrame, net_df: pd.DataFrame):
        rpt       = self.summaries_dir / "summary_report.txt"
        total     = len(self.all_picks)
        n_p       = (self.all_picks["phase"] == "P").sum()
        n_s       = (self.all_picks["phase"] == "S").sum()
        all_dates = sorted(self.all_picks["date"].unique())
        sep  = "=" * 74
        thin = "-" * 74

        with open(rpt, "w") as f:
            f.write(sep + "\n")
            f.write("  QUAKESCOPE DOWNLOAD SUMMARY REPORT\n")
            f.write(f"  Generated : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
            f.write(f"  Directory : {self.output_dir}\n")
            f.write(sep + "\n\n")

            f.write("OVERALL\n" + thin + "\n")
            f.write(f"  Total picks          : {total:>12,}\n")
            f.write(f"  P picks              : {n_p:>12,}\n")
            f.write(f"  S picks              : {n_s:>12,}\n")
            f.write(f"  P/S ratio            : {n_p / n_s if n_s else float('nan'):>12.3f}\n")
            f.write(f"  Networks             : {self.all_picks['network'].nunique():>12}\n")
            f.write(f"  Stations             : {self.all_picks['station'].nunique():>12}\n")
            f.write(f"  Date range           : {all_dates[0]}  →  {all_dates[-1]}\n")
            f.write(f"  Days with any data   : {len(all_dates):>12}\n\n")

            # Network table
            f.write("NETWORK SUMMARY\n" + thin + "\n")
            hdr = (
                f"{'Network':<12} {'Stas':>5} {'Total':>10} {'P':>10} {'S':>10} "
                f"{'P/S':>6} {'Conf_P':>7} {'Conf_S':>7}\n"
            )
            f.write(hdr + thin + "\n")
            for _, r in net_df.iterrows():
                f.write(
                    f"{r['network']:<12} {int(r['n_stations']):>5} {int(r['total_picks']):>10,} "
                    f"{int(r['p_picks']):>10,} {int(r['s_picks']):>10,} "
                    f"{r['ps_ratio']:>6.2f} {r['mean_prob_p']:>7.3f} {r['mean_prob_s']:>7.3f}\n"
                )
            f.write("\n")

            # Top 20 stations
            f.write("TOP 20 STATIONS BY PICK COUNT\n" + thin + "\n")
            hdr2 = (
                f"{'Station':<22} {'Total':>8} {'P':>8} {'S':>8} "
                f"{'P/S':>6} {'Days':>6} {'Uptime%':>8} {'Conf_P':>7} {'Conf_S':>7}\n"
            )
            f.write(hdr2 + thin + "\n")
            for _, r in sta_df.head(20).iterrows():
                f.write(
                    f"{r['station']:<22} {int(r['total_picks']):>8,} {int(r['p_picks']):>8,} "
                    f"{int(r['s_picks']):>8,} {r['ps_ratio']:>6.2f} {int(r['active_days']):>6} "
                    f"{r['uptime_pct']:>7.1f}% {r['mean_prob_p']:>7.3f} {r['mean_prob_s']:>7.3f}\n"
                )
            f.write("\n")

            # Bottom 20 stations
            f.write("BOTTOM 20 STATIONS BY PICK COUNT\n" + thin + "\n")
            f.write(hdr2 + thin + "\n")
            for _, r in sta_df.tail(20).iterrows():
                f.write(
                    f"{r['station']:<22} {int(r['total_picks']):>8,} {int(r['p_picks']):>8,} "
                    f"{int(r['s_picks']):>8,} {r['ps_ratio']:>6.2f} {int(r['active_days']):>6} "
                    f"{r['uptime_pct']:>7.1f}% {r['mean_prob_p']:>7.3f} {r['mean_prob_s']:>7.3f}\n"
                )
            f.write("\n")

        logger.info(f"Text report → {rpt}")

    # ------------------------------------------------------------------
    # Save helper
    # ------------------------------------------------------------------

    def _save_summary_fig(self, fig: plt.Figure, stem: str):
        """Save a figure as both a PNG and a single-page PDF."""
        fig.savefig(
            self.summary_plots_dir / f"{stem}.png", dpi=150, bbox_inches="tight"
        )
        with PdfPages(self.summary_plots_dir / f"{stem}.pdf") as pdf:
            pdf.savefig(fig, bbox_inches="tight")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self):
        """Execute the full plotting and summary pipeline."""
        self._setup_directories()
        self._load_run_config()

        logger.info("Loading picks…")
        self._load_all_picks()

        logger.info("Loading station locations…")
        self._load_station_locations()

        logger.info("Writing text/CSV summaries…")
        self._generate_text_summaries()

        logger.info("Generating per-station figures…")
        self._generate_per_station_figures()

        logger.info("Generating summary Figure 1 (Geographic)…")
        fig1 = self._summary_fig1_geographic()
        self._save_summary_fig(fig1, "summary_fig1_geographic")
        plt.close(fig1)

        logger.info("Generating summary Figure 2 (Heatmaps)…")
        self._generate_heatmap_figures()

        logger.info("Generating summary Figure 3 (Temporal/Statistical)…")
        fig3 = self._summary_fig3_temporal()
        self._save_summary_fig(fig3, "summary_fig3_temporal_stats")
        plt.close(fig3)

        print(f"\nPlots saved to      : {self.plots_dir}")
        print(f"Summaries saved to  : {self.summaries_dir}")


# ---------------------------------------------------------------------------
# Convenience entry point (called from workflow.py)
# ---------------------------------------------------------------------------

def run_plotting(output_dir: str):
    """Entry point called by workflow.py --plot."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    plotter = PicksPlotter(output_dir)
    plotter.run()
