# Asbury Inventory Scraper

Group-wise VIN/inventory CSVs for all 133 Asbury Automotive rooftops.
Runs headless in GitHub Actions (manual dispatch) and uploads to Google Drive.

`Enterprise_Id = 6454b95ba`, `team_id` blank.

---

## Setup (one time)

1. Create a repo and drop these files in:

```
asbury_scrape.py
upload_to_drive.py
rooftops.csv
requirements.txt
.github/workflows/asbury.yml
```

2. Google Drive service account:
   - Google Cloud Console → create a service account → create a JSON key.
   - Share your target Drive folder with the service account email
     (`...@....iam.gserviceaccount.com`) as **Editor**.
   - Grab the folder id from its URL:
     `drive.google.com/drive/folders/`**`<THIS_PART>`**

3. Repo → Settings → Secrets and variables → Actions → add:

| Secret | Value |
|---|---|
| `GDRIVE_SA_JSON` | full contents of the service-account JSON |
| `GDRIVE_FOLDER_ID` | destination folder id |

---

## Running

Actions tab → **Asbury Inventory Scrape** → *Run workflow*.

| Input | Meaning |
|---|---|
| `mode` | `scrape` = full run, `fingerprint` = probe platforms only |
| `group` | one sub-group (`Nalley`, `Coggin`, `LHM`…), blank = all 133 |
| `limit` | cap rooftops — use `3` for a smoke test |
| `workers` | parallel workers, default 6 |
| `upload` | push results to Drive |

**Run this order the first time:**

1. `mode=scrape, group=Nalley, limit=3` — ~2 min, confirms plumbing.
2. `mode=fingerprint` — identifies the 11 unknown rooftops (see below).
3. `mode=scrape` — the full 133.

---

## Output

Written to `output/`, uploaded to Drive under `asbury_<run_number>/`, and
also kept as a workflow artifact for 30 days.

- `asbury_<Group>_<date>.csv` — one per sub-group (15 files)
- `asbury_ALL_<date>.csv` — everything combined
- `run_summary_<date>.csv` — `Batch, Run_Date, Site, Status, Rows, Secs, Script, Detail, Output_CSV`

31 columns, matching the Jenkins sample exactly.

---

## How extraction works

**DDC / Dealer.com — 117 rooftops.** DDC killed its legacy JSON API
(`/apis/widget/.../getInventory` now returns *"Legacy inventory endpoints
are deprecated and no longer return data"*). But the full 35-field payload
is still embedded in page HTML under an `"inventory":[...]` key. The script
finds it with a balanced-brace parser. No browser needed — plain `requests`.
This yields full field coverage: trim, bodyStyle, MSRP, inventoryDate,
image count, `ACTUAL_PHOTO` provider, `pictures.dealer.com` URLs.

**Everything else — 16 rooftops.** Falls back to schema.org JSON-LD, which
most dealer platforms emit. Fewer fields (no MSRP/bodyStyle/inventoryDate)
but valid, deduped VIN rows.

Pagination is `?start=N` in steps of 24; stops on a short page or when no
new VINs appear.

---

## Known issues

- **`parkplace.com` serves 9 rooftops** and **`lhmusedcars.com` serves 3**.
  They're scraped once per domain; split them afterwards using the
  `Rooftop_Account_Id` column, which is populated for exactly this purpose.
- **`coggindelandford.com`** backs both Coggin Deland Ford and
  Coggin Deland Ford Lincoln — same situation.
- **11 rooftops are unfingerprinted** (platform `U` in `rooftops.csv`):
  INFINITI of Tampa, Nalley INFINITI ×2, Plaza INFINITI, Stevinson Lexus ×2,
  Stevinson Toyota ×2, LHM Used Car Supermarket ×3. Run `mode=fingerprint`
  to identify them, then update the `Platform` column and re-run.
- `Cross_Listed_Rooftops` is computed across the whole run, so it's only
  complete on a full 133-rooftop run — not on a `--group` run.
- Asbury's own site sits behind a Vercel bot check, so rooftop discovery
  uses the baked-in `rooftops.csv` rather than live sitemap crawling.
