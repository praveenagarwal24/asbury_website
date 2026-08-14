#!/usr/bin/env python3
"""Upload output/*.csv to a Google Drive folder using a service account.

Env:
  GDRIVE_SA_JSON    service-account JSON (raw string, from repo secret)
  GDRIVE_FOLDER_ID  destination folder id
  DRIVE_SUBFOLDER   optional; creates/uses a dated subfolder
"""
import json
import os
import sys
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
OUTDIR = Path(__file__).resolve().parent / "output"


def get_service():
    raw = os.environ.get("GDRIVE_SA_JSON")
    if not raw:
        sys.exit("GDRIVE_SA_JSON not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def ensure_folder(svc, name, parent):
    q = (f"name='{name}' and '{parent}' in parents and "
         "mimeType='application/vnd.google-apps.folder' and trashed=false")
    res = svc.files().list(q=q, fields="files(id)", supportsAllDrives=True,
                           includeItemsFromAllDrives=True).execute()
    if res.get("files"):
        return res["files"][0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent]}
    return svc.files().create(body=meta, fields="id",
                              supportsAllDrives=True).execute()["id"]


def main():
    folder = os.environ.get("GDRIVE_FOLDER_ID")
    if not folder:
        sys.exit("GDRIVE_FOLDER_ID not set")
    svc = get_service()

    sub = os.environ.get("DRIVE_SUBFOLDER")
    if sub:
        folder = ensure_folder(svc, sub, folder)

    files = sorted(OUTDIR.glob("*.csv"))
    if not files:
        sys.exit("no CSVs in output/")

    for p in files:
        media = MediaFileUpload(str(p), mimetype="text/csv", resumable=False)
        # replace same-named file in folder if present
        q = f"name='{p.name}' and '{folder}' in parents and trashed=false"
        prev = svc.files().list(q=q, fields="files(id)", supportsAllDrives=True,
                                includeItemsFromAllDrives=True).execute().get("files")
        if prev:
            svc.files().update(fileId=prev[0]["id"], media_body=media,
                               supportsAllDrives=True).execute()
            action = "updated"
        else:
            svc.files().create(body={"name": p.name, "parents": [folder]},
                               media_body=media, fields="id",
                               supportsAllDrives=True).execute()
            action = "created"
        print(f"{action}: {p.name} ({p.stat().st_size:,} bytes)")

    print(f"\nUploaded {len(files)} file(s) to folder {folder}")


if __name__ == "__main__":
    main()
