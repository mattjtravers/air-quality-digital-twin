# Figures

All figures 300 dpi PNG; the data behind each is in the matching CSV.

- **(a) `fig_a_study_area.png`** — Study area: PurpleAir sensors coloured by calibration status for the fit as of 2026-10-25T00 UTC, AirNow monitors (labelled) with 10 km matching radii, the 100 m fusion grid extent, and county boundaries (Census 2023 1:500k).
- **(b) `fig_b_timeseries.png`** — Hourly PM2.5 for fitted sensor purpleair:144004 and its matched monitor King Greenleaf Rec Center (0.51 km), 2026-10-18 to 2026-10-25: raw A/B mean, Barkjohn-corrected, locally calibrated, and the monitor's trusted hourly values.
- **(c) `fig_c_scatter.png`** — Calibration pairs for purpleair:144004 against King Greenleaf Rec Center in the fit window [2026-09-25, 2026-10-25): Barkjohn-corrected hourly mean vs monitor, with the stored OLS line and the 1:1 line.
- **(d) `fig_d_qc_rates.png`** — QC flag rates by source: share of PurpleAir snapshots and AirNow hourly observations carrying each flag, any flag (dark grey), and, for PurpleAir, a null Barkjohn-corrected value (light grey); bar labels are counts.
- **(e) `fig_e_histograms.png`** — Distributions for the 99 fitted sensors in the fit as of 2026-10-25T00: distance to the matched monitor and fitted OLS slope (pooled slope and slope 1 marked).
- **(f) `fig_f_surface.png`** — Fused surface for 2026-10-02 16:00 UTC (the highest-monitor-mean hour among kriged hours with variogram range >= 5 km and nugget <= sill/2): kriged PM2.5 and its standard deviation on the 100 m grid, with the hour's 127 kriging-input sensors (circles) and AirNow monitors (triangles).
