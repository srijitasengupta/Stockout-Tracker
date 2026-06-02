"""
ITC STOCKOUT AGENT
==================
Reads new stockout entries from Google Form responses,
fuzzy matches outlet name to PCP + Food Master Sheets,
finds the correct DS, and writes to Active tab.

Form fields expected:
  - Timestamp (auto)
  - Outlet Name
  - Food Products Missing
  - PCP Products Missing

Runs automatically on PythonAnywhere every 30 minutes.
"""

import gspread
import pandas as pd
from fuzzywuzzy import process
from google.oauth2.service_account import Credentials
from datetime import datetime

# ══════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════

SERVICE_ACCOUNT_FILE = '/home/srijitasengupta/stockout-agent-c2328822c592.json'

STOCKOUT_SHEET_ID = '1l8Car6S34plsRzN9sC4pgeDUioujx5C1zjX1tMv9CQI'
MASTER_PCP_ID     = '1oZLNvJGuJynCIhDqWcDCkwCMr07RgYmJHaCyrS2n-Qc'
MASTER_FOOD_ID    = '1lgy88OqF32B-cCej4pmUYY4N2LhRTMqt2WlmYY-2jZI'

WD_TABS = [
    'Gokul Enterprise',
    'Gokul Traders',
    'Orange City Distributors'
]

FORM_TAB   = 'Form responses 1'
ACTIVE_TAB = 'Active'
LOG_TAB    = 'Log'

MATCH_THRESHOLD = 75  # fuzzy match sensitivity 0-100

ACTIVE_HEADERS = [
    'Row ID', 'Form Timestamp', 'Outlet Name', 'Matched Outlet',
    'Products Missing', 'Category', 'DS Name', 'DS Mobile',
    'Beat', 'Beat ID', 'WD', 'Match Score',
    'Status', 'Logged At', 'Resolved At'
]

# ══════════════════════════════════════════════════════
# CONNECT TO GOOGLE
# ══════════════════════════════════════════════════════

def connect():
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    creds  = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=scopes
    )
    client = gspread.authorize(creds)
    return client

# ══════════════════════════════════════════════════════
# LOAD BOTH MASTER SHEETS
# ══════════════════════════════════════════════════════

def load_master(client):
    print("Loading master sheets...")
    all_dfs = []

    sheets = [
        {'id': MASTER_PCP_ID,  'type': 'PCP'},
        {'id': MASTER_FOOD_ID, 'type': 'Food'},
    ]

    for s in sheets:
        try:
            sheet = client.open_by_key(s['id'])
            print(f"\nLoading {s['type']}...")
            for tab in WD_TABS:
                try:
                    ws   = sheet.worksheet(tab)
                    data = ws.get_all_records()
                    df   = pd.DataFrame(data)
                    df['_source_type'] = s['type']
                    df['_source_wd']   = tab
                    all_dfs.append(df)
                    print(f"  {tab}: {len(df)} rows")
                except Exception as e:
                    print(f"  Could not load {tab}: {e}")
        except Exception as e:
            print(f"Could not open {s['type']} sheet: {e}")

    if not all_dfs:
        raise Exception("No data loaded from any master sheet")

    df = pd.concat(all_dfs, ignore_index=True)
    print(f"\nTotal rows: {len(df)}")
    return df

# ══════════════════════════════════════════════════════
# BUILD INDEX FOR FAST LOOKUP
# ══════════════════════════════════════════════════════

def build_index(master_df):
    print("Building outlet index...")
    master_df['_norm'] = (
        master_df['Customer Name']
        .astype(str)
        .str.lower()
        .str.strip()
        .str.replace(r'[^a-z0-9 ]', '', regex=True)
    )

    index = {}
    for _, row in master_df.iterrows():
        key = row['_norm']
        if key and key not in index:  # first occurrence wins
            index[key] = {
                'customer_name': str(row.get('Customer Name', '')),
                'ds_name':       str(row.get('Ds Name', '')),
                'ds_mobile':     str(row.get('Ds Mobile No', '')),
                'beat':          str(row.get('Beat', '')),
                'beat_id':       str(row.get('Beat ID', '')),
                'wd_name':       str(row.get('WD Name', '')),
                'source_type':   str(row.get('_source_type', '')),
            }

    keys = list(index.keys())
    print(f"Index: {len(index)} unique outlets")
    return index, keys

# ══════════════════════════════════════════════════════
# FUZZY MATCH OUTLET
# ══════════════════════════════════════════════════════

def find_outlet(name, index, keys):
    norm = name.lower().strip()
    norm = ''.join(c for c in norm if c.isalnum() or c == ' ')

    # Exact match first
    if norm in index:
        return index[norm], 100

    # Fuzzy match
    result = process.extractOne(norm, keys)
    if result and result[1] >= MATCH_THRESHOLD:
        print(f"  Fuzzy: '{name}' → '{index[result[0]]['customer_name']}' ({result[1]}%)")
        return index[result[0]], result[1]

    print(f"  No match for '{name}' (best: {result[1] if result else 0}%)")
    return None, 0

# ══════════════════════════════════════════════════════
# GET NEW FORM ENTRIES (unprocessed only)
# ══════════════════════════════════════════════════════

def get_new_entries(client):
    sheet      = client.open_by_key(STOCKOUT_SHEET_ID)
    form_tab   = sheet.worksheet(FORM_TAB)
    active_tab = sheet.worksheet(ACTIVE_TAB)

    form_rows   = form_tab.get_all_records()
    active_rows = active_tab.get_all_records()

    # Collect already-processed timestamps
    done = set(str(r.get('Form Timestamp', '')) for r in active_rows)

    new = [r for r in form_rows if str(r.get('Timestamp', '')) not in done]
    print(f"New entries to process: {len(new)}")
    return new, sheet

# ══════════════════════════════════════════════════════
# ENSURE ACTIVE TAB HAS HEADERS
# ══════════════════════════════════════════════════════

def ensure_headers(active_tab):
    existing = active_tab.get_all_values()
    if not existing or existing[0] != ACTIVE_HEADERS:
        active_tab.clear()
        active_tab.append_row(ACTIVE_HEADERS)
        print("Active tab headers written")

# ══════════════════════════════════════════════════════
# WRITE ONE ROW TO ACTIVE TAB
# ══════════════════════════════════════════════════════

def write_row(active_tab, row_id, entry, match, score, category, products):
    now = datetime.now().strftime('%d/%m/%Y %H:%M')
    if match:
        row = [
            row_id,
            str(entry.get('Timestamp', '')),
            str(entry.get('Outlet Name', '')),
            match['customer_name'],
            products,
            category,
            match['ds_name'],
            match['ds_mobile'],
            match['beat'],
            match['beat_id'],
            match['wd_name'],
            f"{score}%",
            'Open',
            now,
            ''
        ]
    else:
        row = [
            row_id,
            str(entry.get('Timestamp', '')),
            str(entry.get('Outlet Name', '')),
            'NOT FOUND',
            products,
            category,
            'UNKNOWN', '', '', '', '',
            f"{score}%",
            'Unmatched',
            now,
            ''
        ]
    active_tab.append_row(row)

# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════

def main():
    print("=" * 50)
    print(f"STOCKOUT AGENT — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 50)

    client    = connect()
    print("Connected ✓")

    master_df          = load_master(client)
    index, keys        = build_index(master_df)
    new_entries, sheet = get_new_entries(client)

    if not new_entries:
        print("No new entries. Done.")
        return

    active_tab = sheet.worksheet(ACTIVE_TAB)
    ensure_headers(active_tab)

    # Get current row count for unique row IDs
    existing_count = len(active_tab.get_all_values())

    processed = 0
    for entry in new_entries:
        outlet = str(entry.get('Outlet Name', '')).strip()
        food   = str(entry.get('Food Products Missing', '')).strip()
        pcp    = str(entry.get('PCP Products Missing', '')).strip()

        if not outlet:
            print("Skipping — empty outlet name")
            continue

        print(f"\nProcessing: {outlet}")
        match, score = find_outlet(outlet, index, keys)

        # Write Food row if food products mentioned
        if food:
            existing_count += 1
            row_id = f"SO-{existing_count:04d}"
            write_row(active_tab, row_id, entry, match, score, 'Food', food)
            print(f"  Food row written → {row_id}")

        # Write PCP row if pcp products mentioned
        if pcp:
            existing_count += 1
            row_id = f"SO-{existing_count:04d}"
            write_row(active_tab, row_id, entry, match, score, 'PCP', pcp)
            print(f"  PCP row written → {row_id}")

        # If neither filled
        if not food and not pcp:
            existing_count += 1
            row_id = f"SO-{existing_count:04d}"
            write_row(active_tab, row_id, entry, match, score, 'Unknown', 'Not specified')
            print(f"  Unknown category row written → {row_id}")

        processed += 1

    print(f"\n✓ Done — {processed} entries processed")

if __name__ == '__main__':
    main()
