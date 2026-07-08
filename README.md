# 🚛 Logistics TMS — Multi-Page Streamlit App

A 5-dashboard Transport Management System built on top of your existing Route
Creation app, using your real data:
- `locations.xlsx` — Warehouse/Plant/DBR (town) coordinates
- `Distributor_wise_Max_Vehicle_Capacity.xlsx` — distributor max-vehicle limits,
  owned/fixed fleet, truck-size assumptions, and monthly (Jan–Dec) targets

## Pages

| # | Page | Purpose |
|---|------|---------|
| 1 | **Overview** (app.py) | KPI cards, charts, district/town/transporter slicers, distributor search |
| 2 | **Route Creation** | Your existing live app — warehouse → DBR route optimizer on the map |
| 3 | **Fleet Calculator – Daily Load** | Trucks needed per day/month (no frequency), plus Priority + TAT allocation |
| 4 | **Fleet Calculator – Frequency Based** | Trucks needed via volume→frequency brackets, with real day-of-week distributor scheduling (not flat averaging) and the same Priority + TAT allocation |
| 5 | **Live Fleet Tracker** | Reads a live Google Sheet gate-out log to show real available Own/Fixed fleet right now, for same-day spot hire decisions |

## Project structure

```
tms/
├── app.py                                   # Overview dashboard (main entry point)
├── pages/
│   ├── 1_🛠️_Route_Creation.py
│   ├── 2_📦_Fleet_Calculator_Daily_Load.py
│   ├── 3_📅_Fleet_Calculator_Frequency_Based.py
│   └── 4_📡_Live_Fleet_Tracker.py
├── utils/
│   └── data_loader.py                        # shared data loading/cleaning — single source of truth
├── data/
│   ├── locations.xlsx
│   └── Distributor_wise_Max_Vehicle_Capacity.xlsx
└── requirements.txt
```

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Push to your existing GitHub repo

Since you already have a live Route Creation app on GitHub, drop these files into
that repo. `app.py` is now the Overview dashboard (your old Route Creation script
has moved to `pages/1_🛠️_Route_Creation.py`, unchanged), then:

```bash
git add .
git commit -m "Add TMS: overview dashboard + daily-load and frequency-based fleet calculators"
git push
```

If you're on **Streamlit Community Cloud**, just repoint the deploy's "Main file
path" to `app.py` — the `pages/` folder is auto-detected and becomes the
sidebar navigation.

## Data assumptions you can tune (all editable in-app, sidebar)

- **Truck size ↔ case capacity** table (starts from your Assumptions sheet: 20T=1000,
  15T=800, 9T=600, 4T=350 cases; other tonnages are interpolated).
- **MTD volume → frequency** brackets (< 40,000 → Twice/Week, 40,000–100,000 →
  Thrice/Week, > 100,000 → Daily), and how many "trips/week" each label means.
- **Working days/month** (daily-load calculator) and **working days/week +
  weeks/month** (frequency calculator).

## How the Priority + TAT allocation simulator works

Every day, demand is filled in this order: **Own fleet → Fixed/Bachat fleet → Spot Hire**
(market-arranged same-day, uncapped). A truck used today is unavailable today; a
configurable % of that day's dispatched batch is assumed back the **next** day, and
100% is back by a configurable full turn-around-time (default 2 days). This means
tomorrow's "available" fleet = trucks never dispatched + trucks already fully returned
+ the portion of yesterday's dispatch expected back today — so the calculator won't
over-call spot hire just because trucks were busy yesterday. All outputs are whole
truck counts, no decimals.

## Notes on data matching

- All 180 DBR points in `locations.xlsx` match the "Area/Town" column in the
  `DB capacity` sheet — used to link routing points to distributor capacity data.
- 161 of ~182 distributor names in `DB Target` matched `DB capacity`'s
  `Agency/Route` column directly; a handful have minor naming differences
  (e.g. "Replace" suffixes, spacing). Rows that don't match will show blank
  monthly targets — worth a quick manual name cleanup in the source Excel if
  you want 100% coverage.
