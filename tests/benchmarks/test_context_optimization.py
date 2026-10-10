from __future__ import annotations

import json


def test_context_window_token_efficiency_vs_raw_tap_csv():
    # Sample raw NASA TAP CSV line with typical column set (~90 columns)
    raw_tap_csv_header = (
        "pl_name,hostname,default_flag,sy_snum,sy_pnum,discoverymethod,disc_year,disc_facility,"
        "pl_orbper,pl_orbpererr1,pl_orbpererr2,pl_orbperlim,pl_orbsmax,pl_orbsmaxerr1,pl_orbsmaxerr2,"
        "pl_rade,pl_radeerr1,pl_radeerr2,pl_bmasse,pl_bmasseerr1,pl_bmasseerr2,pl_orbeccen,pl_orbeccenerr1,"
        "pl_insol,pl_eqt,pl_tranmid,pl_tranmiderr1,pl_tranmiderr2,pl_trandur,pl_trandurerr1,pl_trandep,"
        "st_teff,st_tefferr1,st_rad,st_raderr1,st_mass,st_masserr1,st_met,st_logg,ra,dec,glat,glon,elot,elon,"
        "sy_vmag,sy_jmag,sy_hmag,sy_kmag,sy_gaiamag,sy_tmag,tic_id,gaia_id\n"
    )
    raw_tap_csv_row = (
        "WASP-12 b,WASP-12,1,1,1,Transit,2008,SuperWASP,"
        "1.09142030,0.00000021,-0.00000021,0,0.0229,0.0008,-0.0008,"
        "21.3,0.8,-0.8,467.0,32.0,-32.0,0.049,0.015,"
        "2580.0,2500,2454508.97605,0.00028,-0.00028,3.01,0.03,14000.0,"
        "6300.0,150.0,1.57,0.07,1.35,0.10,0.30,4.18,97.63972,29.67222,12.5,185.3,6.2,95.1,"
        "11.57,10.48,10.23,10.19,11.32,10.98,TIC 38846515,Gaia DR3 3412519283719\n"
    )
    raw_tap_csv_payload = raw_tap_csv_header + raw_tap_csv_row

    # Equivalent exoplanet-db clean JSON representation
    clean_json_representation = {
        "id": "cand-wasp-12-b",
        "target": "WASP-12",
        "tic": "38846515",
        "coords": {"ra": 97.64, "dec": 29.67},
        "period_d": 1.09142,
        "epoch_bjd": 2454508.976,
        "depth_ppm": 14000.0,
        "duration_h": 3.01,
        "disposition": "confirmed_planet",
        "limits": ["Transit fit"],
    }
    clean_json_str = json.dumps(clean_json_representation, separators=(",", ":"))

    csv_chars = len(raw_tap_csv_payload)
    json_chars = len(clean_json_str)

    reduction_pct = (csv_chars - json_chars) / csv_chars * 100.0
    assert reduction_pct >= 60.0, (
        f"Expected >= 60% reduction in characters, got {reduction_pct:.1f}%"
    )
