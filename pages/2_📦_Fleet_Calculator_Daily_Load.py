import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import sys
import os
import math

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.data_loader import (
    build_master_table, load_assumptions, load_vehicle_database,
    cases_per_truck, best_truck_for_tonnage_limit, get_month_options,
    fleet_totals_by_ownership, simulate_daily_allocation
)

st.set_page_config(page_title="Fleet Calculator | Daily Load", page_icon="📦", layout="wide")
st.markdown("<style>.block-container { padding-top: 1rem; padding-bottom: 0.5rem; }</style>", unsafe_allow_html=True)

st.title("📦 Monthly Fleet Calculator — Daily Load Basis")
st.caption("Use this when you already know (or want to plan) each distributor's **daily** dispatch load — "
           "no delivery-frequency assumption is applied.")
st.write("---")

try:
    master = build_master_table()
    veh_block, freq_block = load_assumptions()
    veh_db = load_vehicle_database()
except Exception as e:
    st.error(f"⚠️ Could not load data: {e}")
    st.stop()

MONTHS = get_month_options()
fleet_tonnages = sorted(veh_db["CapacityTonnage"].dropna().unique())
own_total_default, fixed_total_default = fleet_totals_by_ownership(veh_db)

# ---------------- SIDEBAR CONFIG ----------------
st.sidebar.header("⚙️ Calculator Settings")
sel_month = st.sidebar.selectbox("Target Month", MONTHS, index=MONTHS.index("JULY"))
working_days = st.sidebar.number_input("Working days in month", min_value=1, max_value=31, value=26)

st.sidebar.write("---")
st.sidebar.write("**Truck size ↔ Case capacity** (editable)")
edited_veh_block = st.sidebar.data_editor(
    veh_block, num_rows="dynamic", use_container_width=True, key="veh_block_daily"
)

st.sidebar.write("---")
st.sidebar.header("🔄 Fleet Priority & TAT")
own_total = st.sidebar.number_input("Total Own Fleet (trucks)", min_value=0, value=own_total_default, step=1, key="own_daily")
fixed_total = st.sidebar.number_input("Total Fixed/Bachat Fleet (trucks)", min_value=0, value=fixed_total_default, step=1, key="fixed_daily")
return_rate_pct = st.sidebar.slider("% of dispatched trucks back the NEXT day", 0, 100, 50, 5, key="rr_daily")
tat_days = st.sidebar.number_input("Full turn-around time (days until ALL are back)", min_value=1, max_value=10, value=2, step=1, key="tat_daily")

st.sidebar.write("---")
districts = sorted(master["District"].dropna().unique())
sel_districts = st.sidebar.multiselect("Filter: District", districts, default=[])
search_term = st.sidebar.text_input("🔍 Search distributor", "")

# ---------------- FILTER ----------------
f = master.copy()
if sel_districts:
    f = f[f["District"].isin(sel_districts)]
if search_term:
    s = search_term.lower()
    f = f[f["Distributor"].str.lower().str.contains(s) | f["DBR CODE"].astype(str).str.lower().str.contains(s)]

f = f.dropna(subset=[sel_month]).copy()

# ---------------- CORE CALCULATION ----------------
f["MonthlyTarget"] = f[sel_month].round(0)
f["DailyAvgLoad"] = np.ceil(f["MonthlyTarget"] / working_days)

f["RecommendedTruckTonnage"] = f["MaxVehicleTonnage"].apply(
    lambda x: best_truck_for_tonnage_limit(x, fleet_tonnages)
)
f["TruckCaseCapacity"] = f["RecommendedTruckTonnage"].apply(
    lambda t: round(cases_per_truck(t, edited_veh_block))
)
f["TrucksPerDay"] = np.ceil(f["DailyAvgLoad"] / f["TruckCaseCapacity"]).replace([np.inf, -np.inf], np.nan)
f["VehiclesPerMonth"] = f["TrucksPerDay"] * working_days
f["MTD Target (cumulative)"] = f["MonthlyTarget"]  # full month reference for MTD tracking table below

# whole numbers only — no fractional cases or trucks
for c in ["MonthlyTarget", "DailyAvgLoad", "TruckCaseCapacity", "TrucksPerDay", "VehiclesPerMonth"]:
    f[c] = f[c].fillna(0).round(0).astype(int)

# ---------------- KPI CARDS ----------------
total_daily_load = f["DailyAvgLoad"].sum()
total_trucks_per_day = f["TrucksPerDay"].sum()
total_monthly_target = f["MonthlyTarget"].sum()
avg_util = (f["DailyAvgLoad"] / (f["TrucksPerDay"] * f["TruckCaseCapacity"])).replace([np.inf, -np.inf], np.nan).mean()

k1, k2, k3, k4 = st.columns(4)
k1.metric(f"{sel_month.title()} Target (cases)", f"{int(total_monthly_target):,}")
k2.metric("Total Daily Avg Load (cases/day)", f"{int(total_daily_load):,}")
k3.metric("Trucks Needed / Day (company-wide)", f"{int(total_trucks_per_day):,}")
k4.metric("Avg Truck Utilization", f"{int(round(avg_util*100))}%" if pd.notna(avg_util) else "—")

st.write("")

c1, c2 = st.columns(2)
with c1:
    st.subheader("🚚 Trucks Needed / Day by District")
    chart_df = f.groupby("District", as_index=False)["TrucksPerDay"].sum().sort_values("TrucksPerDay", ascending=False)
    if not chart_df.empty:
        fig = px.bar(chart_df, x="District", y="TrucksPerDay", color="TrucksPerDay", color_continuous_scale="Teal")
        fig.update_layout(showlegend=False, height=380, margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)

with c2:
    st.subheader("📊 Recommended Truck Size Mix")
    mix = f["RecommendedTruckTonnage"].value_counts().reset_index()
    mix.columns = ["Truck Tonnage", "Distributors"]
    if not mix.empty:
        fig2 = px.pie(mix, names="Truck Tonnage", values="Distributors", hole=0.45)
        fig2.update_layout(height=380, margin=dict(t=10))
        st.plotly_chart(fig2, use_container_width=True)

st.write("---")

top_cap_row = f.loc[f["MaxVehicleTonnage"].idxmax()] if f["MaxVehicleTonnage"].notna().any() else None
if top_cap_row is not None:
    st.info(f"🏆 **Highest max-capacity distributor:** {top_cap_row['Distributor']} "
            f"({top_cap_row['Town']}) — up to a **{int(top_cap_row['MaxVehicleTonnage'])}T** truck can reach this point.")

st.subheader("📋 Distributor-wise Daily Load & Truck Requirement")

display_cols = [
    "DBR CODE", "Distributor", "Town", "District", "MaxVehicleTonnage",
    "MonthlyTarget", "DailyAvgLoad", "RecommendedTruckTonnage", "TruckCaseCapacity",
    "TrucksPerDay", "VehiclesPerMonth"
]
st.dataframe(
    f[display_cols].sort_values("DailyAvgLoad", ascending=False).style.format({
        "MaxVehicleTonnage": "{:,.0f}", "MonthlyTarget": "{:,.0f}", "DailyAvgLoad": "{:,.0f}",
        "RecommendedTruckTonnage": "{:,.0f}", "TruckCaseCapacity": "{:,.0f}",
        "TrucksPerDay": "{:,.0f}", "VehiclesPerMonth": "{:,.0f}"
    }, na_rep="—"),
    use_container_width=True, height=420
)

with st.expander("ℹ️ Methodology"):
    st.markdown(f"""
    1. **Daily Avg Load** = Monthly Target ({sel_month.title()}) ÷ Working Days ({working_days})
    2. **Recommended Truck** = largest truck size in your fleet that is still ≤ the distributor's
       *Max Capacity Vehicle* limit (the biggest truck physically allowed to reach that point).
    3. **Truck Case Capacity** = interpolated from the Truck size ↔ Case capacity table in the sidebar
       (edit it if your real per-truck case capacities differ).
    4. **Trucks/Day** = ROUNDUP( Daily Avg Load ÷ Truck Case Capacity )
    5. **Trucks/Month** = Trucks/Day × Working Days (total vehicle dispatches needed across the month)

    This page assumes **flat daily dispatch** — no delivery-frequency logic. Use the
    *Frequency Based* calculator if deliveries don't happen every working day.
    """)

st.download_button(
    "⬇️ Download distributor table as CSV",
    f[display_cols].to_csv(index=False).encode("utf-8"),
    file_name=f"fleet_calculator_daily_load_{sel_month.lower()}.csv",
    mime="text/csv"
)

# =====================================================================================
# FLEET ALLOCATION SIMULATION — Own → Fixed/Bachat → Spot Hire, with TAT-based returns
# =====================================================================================
st.write("---")
st.header(f"🔄 Fleet Allocation — {sel_month.title()} — Priority + Turn-Around-Time (TAT)")
st.caption("Priority every working day: **Own fleet first → Fixed/Bachat next → Spot Hire (market)** "
           "covers any shortfall, accounting for trucks still in transit and expected returns.")

if int(total_trucks_per_day) > 1.5 * (int(own_total) + int(fixed_total)):
    st.warning(
        f"⚠️ This model assumes **every distributor needs a truck every working day**. "
        f"{sel_month.title()}'s daily need ({int(total_trucks_per_day):,} trucks/day) is well above your "
        f"combined Own + Fixed fleet ({int(own_total) + int(fixed_total):,}), which can make Spot Hire look "
        f"larger than what actually happens on the ground — many smaller distributors likely don't need "
        f"daily dispatch in reality. Cross-check with the **Fleet Calculator — Frequency Based** page, which "
        f"assigns delivery frequency by volume instead of assuming daily for everyone."
    )

# --- Default (standard) simulation: even daily requirement across all working days ---
default_daily_requirements = [int(total_trucks_per_day)] * int(working_days)
default_alloc_rows = simulate_daily_allocation(default_daily_requirements, int(own_total), int(fixed_total),
                                                return_rate=return_rate_pct / 100.0, tat_days=int(tat_days))
default_alloc_df = pd.DataFrame(default_alloc_rows)
for c in default_alloc_df.columns:
    if c != "Day":
        default_alloc_df[c] = default_alloc_df[c].round(0).astype(int)

m_vol = int(total_monthly_target)
avg_own_per_day = int(round(default_alloc_df["Own Used"].mean()))
avg_fixed_per_day = int(round(default_alloc_df["Fixed Used"].mean()))
avg_spot_per_day = int(round(default_alloc_df["Spot Hire Used"].mean()))
avg_total_per_day = avg_own_per_day + avg_fixed_per_day + avg_spot_per_day
total_spot_month = int(default_alloc_df["Spot Hire Used"].sum())
spot_days = int((default_alloc_df["Spot Hire Used"] > 0).sum())
max_spot_single_day = int(default_alloc_df["Spot Hire Used"].max())

with st.container(border=True):
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Monthly Volume (cases)", f"{m_vol:,}")
    s2.metric("🟦 Own Fleet (capacity)", f"{int(own_total):,}")
    s3.metric("🟧 Fixed/Bachat (capacity)", f"{int(fixed_total):,}")
    s4.metric("🟥 Spot Hire Needed This Month", f"{total_spot_month:,}")

    st.write(
        f"Your **Own ({int(own_total):,})** and **Fixed/Bachat ({int(fixed_total):,})** fleets are reused "
        f"daily and cover most of {sel_month.title()}'s demand (~{avg_own_per_day:,} Own + "
        f"{avg_fixed_per_day:,} Fixed/Bachat vehicles on a typical day). To fully cover the month, you'll "
        f"need to arrange **~{total_spot_month:,} Spot Hire vehicle bookings** from the market — "
        f"needed on **{spot_days} of {int(working_days)}** working days, up to "
        f"**{max_spot_single_day} vehicles** on the busiest day."
    )
    if total_spot_month == 0:
        st.success("✅ Own + Fixed fleet fully covers this month's demand — no spot hire needed.")

# --- Advanced: let the user customize day-by-day if the load isn't perfectly flat ---
with st.expander("🛠️ Advanced — customize day-by-day requirement (if daily load actually varies)"):
    st.caption("Defaults to the flat 'Trucks Needed / Day' figure repeated across all working days. "
               "Edit any day to reflect real variation — the chart & table below will update.")

    _cache_key_daily = (sel_month, int(working_days), int(total_trucks_per_day))
    if st.session_state.get("_cache_key_daily") != _cache_key_daily:
        st.session_state.daily_alloc_df = pd.DataFrame({
            "Day": list(range(1, int(working_days) + 1)),
            "Trucks Required": default_daily_requirements
        })
        st.session_state._cache_key_daily = _cache_key_daily

    edited_alloc_df = st.data_editor(
        st.session_state.daily_alloc_df, num_rows="fixed", use_container_width=True,
        key="daily_alloc_editor", hide_index=True,
        column_config={
            "Day": st.column_config.NumberColumn(disabled=True),
            "Trucks Required": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
        }
    )
    daily_requirements = [int(round(x)) for x in edited_alloc_df["Trucks Required"].tolist()]

    alloc_rows = simulate_daily_allocation(daily_requirements, int(own_total), int(fixed_total),
                                            return_rate=return_rate_pct / 100.0, tat_days=int(tat_days))
    alloc_df = pd.DataFrame(alloc_rows)
    for c in alloc_df.columns:
        if c != "Day":
            alloc_df[c] = alloc_df[c].round(0).astype(int)

    total_own_used_a = int(alloc_df["Own Used"].sum())
    total_fixed_used_a = int(alloc_df["Fixed Used"].sum())
    total_spot_used_a = int(alloc_df["Spot Hire Used"].sum())

    st.caption("Sums below are across all days shown in this custom table (not per-day averages).")
    a1, a2, a3 = st.columns(3)
    a1.metric("Own Fleet Used (sum)", f"{total_own_used_a:,}")
    a2.metric("Fixed/Bachat Used (sum)", f"{total_fixed_used_a:,}")
    a3.metric("Spot Hire Needed (sum)", f"{total_spot_used_a:,}")

    chart_alloc = alloc_df.melt(id_vars=["Day"], value_vars=["Own Used", "Fixed Used", "Spot Hire Used"],
                                 var_name="Source", value_name="Trucks")
    fig_alloc = px.bar(chart_alloc, x="Day", y="Trucks", color="Source", barmode="stack",
                        color_discrete_map={"Own Used": "#1b4fd2", "Fixed Used": "#f2a20c", "Spot Hire Used": "#d93838"})
    fig_alloc.update_layout(height=380, margin=dict(t=10))
    st.plotly_chart(fig_alloc, use_container_width=True)

    st.dataframe(alloc_df, use_container_width=True, height=380)

    st.download_button(
        "⬇️ Download customized allocation table as CSV",
        alloc_df.to_csv(index=False).encode("utf-8"),
        file_name=f"fleet_allocation_daily_load_{sel_month.lower()}.csv",
        mime="text/csv"
    )

with st.expander("ℹ️ Allocation Methodology"):
    st.markdown(f"""
    **Priority every day:** Own fleet → Fixed/Bachat fleet → Spot Hire (uncapped, market-arranged same-day).

    **Returns logic:** a truck dispatched today is unavailable today. **{return_rate_pct}%** of that day's
    dispatched batch is assumed back and available again the next day; the remaining share is back in the
    yard by day **{tat_days}** after dispatch. So a distributor's demand on Day 2 is first covered by trucks
    idle from Day 1, plus the returning share of Day 1's dispatch — spot hire only kicks in once Own + Fixed
    availability is exhausted.

    All figures are whole truck counts.
    """)
