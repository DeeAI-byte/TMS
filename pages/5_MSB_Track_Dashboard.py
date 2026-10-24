import html
import os
import sys
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.data_loader import require_dashboard_password
from utils import msbtrack_api as msb

try:
    import folium
    from streamlit_folium import st_folium
    HAS_FOLIUM = True
except Exception:  # fall back to st.map()
    HAS_FOLIUM = False

st.set_page_config(page_title="MSB Track | Live Fleet", page_icon="🛰️", layout="wide")
require_dashboard_password()
st.markdown("<style>.block-container { padding-top: 1rem; padding-bottom: 0.5rem; }</style>", unsafe_allow_html=True)

REFRESH_SECONDS = 60

st.title("🛰️ MSB Track — Live Fleet Dashboard")
st.caption("Live GPS telemetry for the SLMG Beverage fleet, pulled from MSB Track.")

# ---------------- SIDEBAR ----------------
st.sidebar.header("🛰️ MSB Track")
auto_refresh = st.sidebar.toggle(f"Auto-refresh every {REFRESH_SECONDS}s", value=True, key="msb_auto_refresh")
if st.sidebar.button("🔄 Refresh now", use_container_width=True, key="msb_refresh_now"):
    msb.load_fleet_snapshot.clear()
    st.rerun()

cfg = msb.get_config()
st.sidebar.caption(
    f"Organisation ID: **{cfg.org_id}**  \n"
    f"Session cookie: **{'configured' if cfg.session_id else 'missing'}**  \n"
    f"Load-balancer cookies: **{'configured' if (cfg.awselb or cfg.awselbcors) else 'not set (optional)'}**"
)

if not cfg.is_configured:
    st.error("MSB Track session cookie is not configured.")
    st.markdown(
        "Add this in **Streamlit → Settings → Secrets** (or a local `.env`), using the cookie values from a "
        "logged-in MSB Track browser session (DevTools → Application → Cookies):"
    )
    st.code(
        'MSB_ORG_ID = "599"\n'
        'MSB_SESSION_ID = "<ASP.NET_SessionId value>"\n'
        'MSB_AWSELB = "<AWSELB value>"\n'
        'MSB_AWSELBCORS = "<AWSELBCORS value>"',
        language="toml",
    )
    st.stop()


# ---------------- HELPERS ----------------
def _fmt(value, suffix=""):
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NaT:
        return "—"
    if isinstance(value, float):
        return f"{value:,.1f}{suffix}"
    return f"{value}{suffix}"


def _popup_html(row) -> str:
    last = row["Last Update"]
    items = [
        ("Vehicle", row["Vehicle Number"]),
        ("Vehicle ID", row["Vehicle ID"]),
        ("Status", str(row["Device Status"]).title()),
        ("Speed", _fmt(row["Speed"], " km/h")),
        ("Ignition", row["Ignition"]),
        ("Motion", row["Motion"]),
        ("Odometer", _fmt(row["Distance"], " km")),
        ("Fuel", _fmt(row["Fuel"], " l")),
        ("Model", row["MakeModel"] or "—"),
        ("Last update", last.strftime("%d/%m/%Y %I:%M %p") if pd.notna(last) else "—"),
    ]
    rows = "".join(
        f"<tr><td style='padding-right:8px'><b>{html.escape(str(k))}</b></td><td>{html.escape(str(v))}</td></tr>"
        for k, v in items
    )
    return f"<table style='font-size:12px'>{rows}</table>"


def _render_map(df: pd.DataFrame):
    geo = df[df["Has Location"]].copy()
    if geo.empty:
        st.info("No vehicles currently have a usable GPS location.")
        return
    if not HAS_FOLIUM:
        st.map(geo.rename(columns={"Latitude": "lat", "Longitude": "lon"})[["lat", "lon"]])
        return
    m = folium.Map(location=[geo["Latitude"].mean(), geo["Longitude"].mean()], zoom_start=7, control_scale=True)
    for _, r in geo.iterrows():
        status = r["Device Status"]
        folium.Marker(
            location=[r["Latitude"], r["Longitude"]],
            tooltip=f"{r['Vehicle Number']} · {str(status).title()} · {_fmt(r['Speed'], ' km/h')}",
            popup=folium.Popup(_popup_html(r), max_width=280),
            icon=folium.Icon(color=msb.FOLIUM_COLORS.get(status, "black"), icon="truck", prefix="fa"),
        ).add_to(m)
    m.fit_bounds([[geo["Latitude"].min(), geo["Longitude"].min()], [geo["Latitude"].max(), geo["Longitude"].max()]])
    try:
        st_folium(m, height=520, use_container_width=True, returned_objects=[], key="msb_live_map")
    except TypeError:  # older streamlit-folium without use_container_width
        st_folium(m, height=520, width=1100, returned_objects=[], key="msb_live_map")


def _bar(df: pd.DataFrame, value_col: str, title: str, top_n: int, color: str):
    data = df.dropna(subset=[value_col])
    if data.empty:
        st.info(f"No data returned for {title.lower()}.")
        return
    data = data.sort_values(value_col, ascending=True).tail(top_n)
    fig = px.bar(data, x=value_col, y="Vehicle Number", orientation="h", title=title,
                 color_discrete_sequence=[color])
    fig.update_layout(height=max(320, 22 * len(data) + 120), margin=dict(t=50, b=10), yaxis_title=None)
    st.plotly_chart(fig, use_container_width=True)


# ---------------- DASHBOARD (auto-refreshing fragment) ----------------
def dashboard():
    try:
        snap = msb.load_fleet_snapshot(cfg.fingerprint())
    except msb.MSBAuthError as exc:
        st.error(f"🔐 {exc}")
        return
    except msb.MSBAPIError as exc:
        st.error(f"⚠️ Could not load MSB Track data: {exc}")
        return

    fleet = snap["fleet"]
    st.caption(f"Last refreshed {snap['fetched_at'].strftime('%d %b %Y, %I:%M:%S %p')} IST"
               + (f" · auto-refresh every {REFRESH_SECONDS}s" if auto_refresh else " · auto-refresh off"))
    for w in snap["warnings"]:
        st.warning(w)

    # ---- KPI cards ----
    kpis = msb.compute_kpis(fleet)
    help_text = {"Online Vehicles": "Reporting to MSB: running + idle + online."}
    for col, (label, value) in zip(st.columns(6), kpis.items()):
        col.metric(label, f"{value:,}", help=help_text.get(label))

    st.write("---")

    # ---- Live vehicle table ----
    st.subheader("📋 Live Vehicles")
    f1, f2, f3, f4 = st.columns([2, 2, 2, 1])
    query = f1.text_input("🔍 Search (vehicle number / ID / model)", key="msb_search")
    status_opts = [s for s in msb.STATUS_ORDER if s in set(fleet["Device Status"])]
    statuses = f2.multiselect("Status filter", status_opts, key="msb_status_filter")
    sort_col = f3.selectbox("Sort by", msb.LIVE_TABLE_COLUMNS, index=0, key="msb_sort_col")
    ascending = f4.radio("Order", ["Asc", "Desc"], horizontal=True, key="msb_sort_order") == "Asc"

    view = msb.filter_fleet(fleet, query, statuses).sort_values(sort_col, ascending=ascending, na_position="last")
    st.dataframe(
        view[msb.LIVE_TABLE_COLUMNS], use_container_width=True, hide_index=True, height=380,
        column_config={
            "Vehicle ID": st.column_config.NumberColumn(format="%d"),
            "Speed": st.column_config.NumberColumn(format="%.1f km/h"),
            "Latitude": st.column_config.NumberColumn(format="%.5f"),
            "Longitude": st.column_config.NumberColumn(format="%.5f"),
            "Distance": st.column_config.NumberColumn(format="%.1f km", help="Odometer reading"),
            "Fuel": st.column_config.NumberColumn(format="%.1f l"),
            "Last Update": st.column_config.DatetimeColumn(format="DD/MM/YYYY hh:mm A"),
        },
    )
    st.caption(f"Showing {len(view):,} of {len(fleet):,} vehicles")
    st.download_button(
        "⬇️ Export CSV", view[msb.LIVE_TABLE_COLUMNS].to_csv(index=False).encode("utf-8"),
        file_name=f"msb_fleet_{snap['fetched_at'].strftime('%Y%m%d_%H%M')}.csv", mime="text/csv",
        key="msb_csv_export",
    )

    st.write("---")

    # ---- Analytics ----
    st.subheader("📊 Analytics")
    top_n = st.slider("Vehicles shown per chart", 5, 60, 20, key="msb_top_n")
    a1, a2 = st.columns(2)
    with a1:
        counts = fleet["Device Status"].value_counts().rename_axis("Status").reset_index(name="Vehicles")
        fig = px.pie(counts, names="Status", values="Vehicles", hole=0.45, title="Vehicle Status Distribution",
                     color="Status", color_discrete_map=msb.STATUS_COLORS)
        fig.update_layout(height=360, margin=dict(t=50, b=10))
        st.plotly_chart(fig, use_container_width=True)
    with a2:
        _bar(snap["distance"], "Distance (km)", "Distance by Vehicle", top_n, "#3498db")
    a3, a4 = st.columns(2)
    with a3:
        _bar(snap["distance"], "Fuel (l)", "Fuel by Vehicle", top_n, "#e67e22")
    with a4:
        _bar(snap["overspeed"], "Overspeed Events", "Overspeed by Vehicle", top_n, "#e74c3c")

    st.write("---")

    # ---- Live map ----
    st.subheader("🗺️ Live Map")
    _render_map(view if (query or statuses) else fleet)

    # ---- Diagnostics ----
    with st.expander("🔧 Data diagnostics"):
        st.write("**Fetch timings (s):**", snap["timings"])
        st.write(f"Vehicles without a location: {int((~fleet['Has Location']).sum())} · "
                 f"without telemetry: {int(fleet['Telemetry Vehicle ID'].isna().sum())}")


fragment = st.fragment(run_every=REFRESH_SECONDS if auto_refresh else None)(dashboard)
fragment()

# ---------------- RAW ENDPOINT TESTER (for exploring the extra MSB endpoints) ----------------
with st.expander("🧪 Raw endpoint tester (VehicleDashboard, GetVehicleListData, VehicleExpand, index …)"):
    st.caption("Fetches a path on the MSB host using your configured session — handy for checking what an "
               "endpoint really returns before wiring it in.")
    default_path = f"/Home/VehicleDashboard?orgId={cfg.org_id}"
    raw_path = st.text_input("Path", value=default_path, key="msb_raw_path")
    if st.button("Fetch", key="msb_raw_fetch"):
        try:
            data = msb.MSBClient(cfg).get_json(raw_path)
            st.json(data[:20] if isinstance(data, list) else data)
        except Exception as exc:
            st.error(str(exc))
