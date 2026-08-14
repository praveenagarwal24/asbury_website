#!/usr/bin/env python3
"""
Asbury Automotive — group-wise inventory scraper.

Entry point is the Asbury sitemap; falls back to the baked-in manifest
(rooftops.csv) because asburyauto.com sits behind a Vercel bot check.

Platforms
---------
DDC/Dealer.com (117 rooftops)  -> primary, validated.
  DDC's legacy JSON API is dead ("Legacy inventory endpoints are deprecated
  and no longer return data"), but the full 35-field payload is still
  embedded in page HTML under an "inventory":[...] key.
DealerInspire / DealerOn / unknown -> JSON-LD (schema.org) extractor,
  which most dealer platforms emit. Lower field coverage but valid rows.

Modes
-----
  python asbury_scrape.py fingerprint   # probe every rooftop, report platform
  python asbury_scrape.py scrape        # full run -> group-wise CSVs
  python asbury_scrape.py scrape --group Nalley --limit 3
"""

import argparse
import concurrent.futures as cf
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ENTERPRISE_ID = "6454b95ba"
TEAM_ID = ""

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "rooftops.csv"
OUTDIR = HERE / "output"

HEADERS = ["Enterprise_Id", "team_id", "VIN", "Rooftop", "Rooftop_Account_Id",
           "Rooftop_Website", "City", "State", "Condition", "Year", "Make",
           "Model", "Trim", "Body_Style", "Mileage", "Exterior_Color",
           "Interior_Color", "Engine", "Transmission", "Drivetrain",
           "Fuel_Type", "MPG", "MSRP", "Price", "Stock_Number",
           "Inventory_Date", "Image_Count", "Image_Provider", "VLP_Image_URL",
           "VDP_URL", "Cross_Listed_Rooftops"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DDC_PATHS = [("New", "/new-inventory/index.htm"),
             ("Used", "/used-inventory/index.htm"),
             ("Certified", "/certified-inventory/index.htm")]

DI_PATHS = [("New", "/new-vehicles/"),
            ("Used", "/used-vehicles/"),
            ("Certified", "/used-vehicles/hcuv/")]

DEALERON_PATHS = [("New", "/searchnew.aspx"),
                  ("Used", "/searchused.aspx"),
                  ("All", "/searchall.aspx")]

PAGE_SIZE = 24
MAX_PAGES = 80


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------
def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def get(session, url, timeout=30, retries=2):
    for i in range(retries + 1):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 429):
                time.sleep(2 * (i + 1))
                continue
            return None
        except requests.RequestException:
            if i == retries:
                return None
            time.sleep(1.5 * (i + 1))
    return None


# --------------------------------------------------------------------------
# balanced-brace JSON slice
# --------------------------------------------------------------------------
def bal_at(text, i):
    depth = 0
    in_s = False
    esc = False
    for p in range(i, len(text)):
        c = text[p]
        if in_s:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_s = False
            continue
        if c == '"':
            in_s = True
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i:p + 1])
                except Exception:
                    return None
    return None


def pull_ddc_inventory(html):
    """Largest "inventory":[...] array whose records carry a vin."""
    best = None
    for m in re.finditer(r'"inventory"\s*:\s*\[', html):
        i = html.index("[", m.start())
        arr = bal_at(html, i)
        if isinstance(arr, list) and arr and isinstance(arr[0], dict) \
                and arr[0].get("vin"):
            if best is None or len(arr) > len(best):
                best = arr
    return best or []


def pull_jsonld_vehicles(html):
    """schema.org fallback — works on most non-DDC platforms."""
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
                         html, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue

        def walk(node):
            if isinstance(node, list):
                for x in node:
                    walk(x)
            elif isinstance(node, dict):
                if node.get("vehicleIdentificationNumber"):
                    out.append(node)
                for v in node.values():
                    walk(v)

        walk(data)
    # dedupe
    seen, uniq = set(), []
    for v in out:
        vin = v.get("vehicleIdentificationNumber")
        if vin and vin not in seen:
            seen.add(vin)
            uniq.append(v)
    return uniq


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def num(s):
    if s is None:
        return ""
    m = re.search(r"-?\d+(\.\d+)?", str(s).replace(",", ""))
    return m.group(0) if m else ""


def ddc_attr(v, name):
    for a in v.get("attributes") or []:
        if a.get("name") == name:
            val = a.get("value")
            return val if val is not None else a.get("labeledValue", "")
    return ""


def ddc_msrp(v):
    for d in (v.get("pricing") or {}).get("dprice") or []:
        if re.search(r"msrp", str(d.get("typeClass", "")), re.I) or \
           re.search(r"MSRP", str(d.get("label", "")), re.I):
            return num(d.get("value"))
    return ""


def blank_row(rt):
    return {h: "" for h in HEADERS} | {
        "Enterprise_Id": ENTERPRISE_ID,
        "team_id": TEAM_ID,
        "Rooftop": rt["Rooftop"],
        "Rooftop_Website": rt["Domain"],
        "City": rt["City"],
        "State": rt["State"],
    }


# --------------------------------------------------------------------------
# normalizers
# --------------------------------------------------------------------------
def ddc_to_row(v, rt):
    imgs = v.get("images") if isinstance(v.get("images"), list) else []
    if v.get("certified"):
        cond = "Certified"
    elif re.search(r"new", str(v.get("condition") or v.get("type") or ""), re.I):
        cond = "New"
    else:
        cond = "Used"
    link = v.get("link") or ""
    if link and not link.startswith("http"):
        link = "https://www." + rt["Domain"] + link
    row = blank_row(rt)
    row.update({
        "VIN": v.get("vin", ""),
        "Rooftop_Account_Id": v.get("accountId", ""),
        "Condition": cond,
        "Year": v.get("year", ""),
        "Make": v.get("make", ""),
        "Model": v.get("model", ""),
        "Trim": v.get("trim", ""),
        "Body_Style": v.get("bodyStyle") or ddc_attr(v, "bodyStyle"),
        "Mileage": num(ddc_attr(v, "odometer")),
        "Exterior_Color": ddc_attr(v, "exteriorColor"),
        "Interior_Color": ddc_attr(v, "interiorColor"),
        "Engine": ddc_attr(v, "engine"),
        "Transmission": ddc_attr(v, "transmission"),
        "Drivetrain": ddc_attr(v, "driveLine"),
        "Fuel_Type": v.get("fuelType", ""),
        "MPG": ddc_attr(v, "mpg"),
        "MSRP": ddc_msrp(v),
        "Price": num((v.get("pricing") or {}).get("retailPrice")),
        "Stock_Number": v.get("stockNumber", ""),
        "Inventory_Date": v.get("inventoryDate", ""),
        "Image_Count": len(imgs),
        "Image_Provider": (imgs[0].get("provider", "") if imgs and isinstance(imgs[0], dict) else ""),
        "VLP_Image_URL": (imgs[0].get("uri", "") if imgs and isinstance(imgs[0], dict) else ""),
        "VDP_URL": link,
    })
    return row


def jsonld_to_row(v, rt, cond_hint=""):
    def nested(key, sub):
        o = v.get(key)
        return (o or {}).get(sub, "") if isinstance(o, dict) else (o or "")

    offers = v.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    img = v.get("image")
    if isinstance(img, list):
        img = img[0] if img else ""
    odo = v.get("mileageFromOdometer")
    odo_v = odo.get("value") if isinstance(odo, dict) else odo
    cond = cond_hint
    ic = str(v.get("itemCondition") or "")
    if not cond:
        cond = "New" if "New" in ic else "Used"

    row = blank_row(rt)
    row.update({
        "VIN": v.get("vehicleIdentificationNumber", ""),
        "Condition": cond,
        "Year": v.get("vehicleModelDate", "") or v.get("modelDate", ""),
        "Make": nested("brand", "name") or nested("manufacturer", "name"),
        "Model": v.get("model", "") if isinstance(v.get("model"), str) else nested("model", "name"),
        "Mileage": num(odo_v),
        "Exterior_Color": v.get("color", ""),
        "Interior_Color": v.get("vehicleInteriorColor", ""),
        "Engine": v.get("vehicleEngine", "") if isinstance(v.get("vehicleEngine"), str) else "",
        "Transmission": v.get("vehicleTransmission", ""),
        "Drivetrain": v.get("driveWheelConfiguration", "") if isinstance(v.get("driveWheelConfiguration"), str) else "",
        "Fuel_Type": v.get("fuelType", "") if isinstance(v.get("fuelType"), str) else "",
        "Price": num(offers.get("price")),
        "Stock_Number": v.get("sku", ""),
        "Image_Count": 1 if img else 0,
        "Image_Provider": "ACTUAL_PHOTO" if img and "pictures.dealer.com" in str(img) else "",
        "VLP_Image_URL": img or "",
        "VDP_URL": v.get("url", "") or offers.get("url", ""),
    })
    # trim: strip "New 2026 Honda Accord" prefix off name
    name = v.get("name", "") or ""
    pre = " ".join(x for x in [row["Condition"], str(row["Year"]), row["Make"], row["Model"]] if x)
    if name and pre and name.startswith(pre):
        row["Trim"] = name[len(pre):].strip()
    return row


# --------------------------------------------------------------------------
# scrapers
# --------------------------------------------------------------------------
def scrape_paged(session, base, paths, parser, page_param="start", step=PAGE_SIZE):
    """Generic paginator. parser(html) -> list of raw records."""
    found = {}
    for label, path in paths:
        seen_here = set()
        for pg in range(MAX_PAGES):
            url = f"https://www.{base}{path}"
            if pg:
                sep = "&" if "?" in path else "?"
                url += f"{sep}{page_param}={pg * step}"
            html = get(session, url)
            if not html:
                break
            recs = parser(html)
            if not recs:
                break
            fresh = 0
            for r in recs:
                vin = r.get("vin") or r.get("vehicleIdentificationNumber")
                if not vin or vin in seen_here:
                    continue
                seen_here.add(vin)
                fresh += 1
                # certified record beats a plain used duplicate
                prev = found.get(vin)
                if prev is None or (r.get("certified") and not prev[0].get("certified")):
                    found[vin] = (r, label)
            if fresh == 0 or len(recs) < step:
                break
            time.sleep(0.25)
    return found


def scrape_rooftop(rt):
    t0 = time.time()
    session = make_session()
    plat = rt["Platform"]
    rows, detail = [], ""
    try:
        if plat == "D":
            found = scrape_paged(session, rt["Domain"], DDC_PATHS, pull_ddc_inventory)
            rows = [ddc_to_row(r, rt) for r, _ in found.values()]
            detail = "ddc-embedded-json"
        else:
            paths = DI_PATHS if plat == "I" else (DEALERON_PATHS if plat == "O" else DI_PATHS + DDC_PATHS)
            found = scrape_paged(session, rt["Domain"], paths, pull_jsonld_vehicles)
            rows = [jsonld_to_row(r, rt, lbl if lbl != "All" else "") for r, lbl in found.values()]
            detail = "jsonld-fallback"
        status = "OK" if rows else "EMPTY"
    except Exception as e:  # noqa: BLE001
        status = "ERROR"
        detail = f"{type(e).__name__}: {e}"[:160]
    return {
        "rooftop": rt, "rows": rows, "status": status, "detail": detail,
        "secs": round(time.time() - t0, 1),
    }


# --------------------------------------------------------------------------
# fingerprint mode
# --------------------------------------------------------------------------
def fingerprint(rt):
    session = make_session()
    dom = rt["Domain"]
    out = {"Rooftop": rt["Rooftop"], "Domain": dom, "verdict": "UNKNOWN", "notes": ""}
    html = get(session, f"https://www.{dom}/")
    if not html:
        out["verdict"] = "UNREACHABLE"
        return out
    sigs = []
    if pull_ddc_inventory(html) or "inventory-data-bus1" in html:
        sigs.append("DDC")
    if "dealerinspire" in html.lower() or "/hcuv/" in html:
        sigs.append("DealerInspire")
    if "dealeron" in html.lower() or "searchall.aspx" in html.lower():
        sigs.append("DealerOn")
    if "dealereprocess" in html.lower():
        sigs.append("DealerEProcess")
    if "vincue" in html.lower():
        sigs.append("VinCue")
    if "convertus" in html.lower():
        sigs.append("Convertus")
    jl = pull_jsonld_vehicles(html)
    out["verdict"] = "/".join(sigs) if sigs else "UNKNOWN"
    out["notes"] = f"jsonld_vehicles_on_home={len(jl)}"
    # probe inventory paths
    hits = []
    for label, p in DDC_PATHS + DI_PATHS + DEALERON_PATHS:
        h = get(session, f"https://www.{dom}{p}", timeout=20, retries=0)
        if h:
            n_ddc = len(pull_ddc_inventory(h))
            n_jl = len(pull_jsonld_vehicles(h))
            if n_ddc or n_jl:
                hits.append(f"{p}[ddc={n_ddc},jsonld={n_jl}]")
    out["paths"] = " ".join(hits)
    return out


# --------------------------------------------------------------------------
# manifest / output
# --------------------------------------------------------------------------
def load_manifest():
    rows = []
    with open(MANIFEST, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="|"):
            if r.get("Domain"):
                rows.append(r)
    return rows


def add_cross_listing(all_rows):
    by_vin = {}
    for r in all_rows:
        by_vin.setdefault(r["VIN"], set()).add(r["Rooftop"])
    for r in all_rows:
        others = sorted(by_vin.get(r["VIN"], set()) - {r["Rooftop"]})
        r["Cross_Listed_Rooftops"] = "; ".join(others)


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        w.writeheader()
        for r in rows:
            w.writerow({h: r.get(h, "") for h in HEADERS})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["scrape", "fingerprint"])
    ap.add_argument("--group", help="only this sub-group")
    ap.add_argument("--limit", type=int, help="cap rooftops (smoke test)")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    manifest = load_manifest()
    if args.group:
        manifest = [r for r in manifest if r["Group"].lower() == args.group.lower()]
    if args.limit:
        manifest = manifest[:args.limit]

    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    OUTDIR.mkdir(parents=True, exist_ok=True)

    if args.mode == "fingerprint":
        res = []
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            for out in ex.map(fingerprint, manifest):
                res.append(out)
                print(f"{out['Domain']:42s} {out['verdict']:22s} {out.get('paths','')}", flush=True)
        p = OUTDIR / f"fingerprint_{run_date}.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["Rooftop", "Domain", "verdict", "notes", "paths"])
            w.writeheader()
            for r in res:
                w.writerow({k: r.get(k, "") for k in w.fieldnames})
        print(f"\nWrote {p}")
        return

    all_rows, summary = [], []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(scrape_rooftop, rt): rt for rt in manifest}
        done = 0
        for fut in cf.as_completed(futs):
            r = fut.result()
            done += 1
            rt = r["rooftop"]
            all_rows.extend(r["rows"])
            summary.append({
                "Batch": rt["Group"], "Run_Date": run_date, "Site": rt["Rooftop"],
                "Status": r["status"], "Rows": len(r["rows"]), "Secs": r["secs"],
                "Script": "asbury_scrape.py", "Detail": r["detail"],
                "Output_CSV": f"asbury_{rt['Group']}_{run_date}.csv",
            })
            print(f"[{done}/{len(manifest)}] {rt['Rooftop'][:44]:44s} "
                  f"{r['status']:6s} {len(r['rows']):5d} rows  {r['secs']}s", flush=True)

    add_cross_listing(all_rows)

    groups = {}
    for r in all_rows:
        g = next((m["Group"] for m in manifest if m["Rooftop"] == r["Rooftop"]), "Other")
        groups.setdefault(g, []).append(r)

    written = []
    for g, rows in sorted(groups.items()):
        p = OUTDIR / f"asbury_{g}_{run_date}.csv"
        write_csv(p, rows)
        written.append(p)
        print(f"  -> {p.name}: {len(rows)} rows")

    p_all = OUTDIR / f"asbury_ALL_{run_date}.csv"
    write_csv(p_all, all_rows)
    written.append(p_all)

    p_sum = OUTDIR / f"run_summary_{run_date}.csv"
    with open(p_sum, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Batch", "Run_Date", "Site", "Status",
                                          "Rows", "Secs", "Script", "Detail", "Output_CSV"])
        w.writeheader()
        w.writerows(sorted(summary, key=lambda x: (x["Batch"], x["Site"])))
    written.append(p_sum)

    ok = sum(1 for s in summary if s["Status"] == "OK")
    print(f"\nTOTAL {len(all_rows)} VINs | {ok}/{len(summary)} rooftops OK "
          f"| {len(groups)} group CSVs")
    print(f"Summary: {p_sum.name}")


if __name__ == "__main__":
    sys.exit(main())
