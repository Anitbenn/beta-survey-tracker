"""
Beta Survey Tracker — GitHub Actions Refresh Script
=====================================================
Pulls survey completion data from Qualtrics and writes completions.json.
Runs as a GitHub Actions scheduled job (replaces Lambda + EventBridge).

Required environment variables (set as GitHub Secrets):
    QUALTRICS_API_TOKEN
    QUALTRICS_DATACENTER_ID
"""

import json
import os
import sys
import time
import zipfile
import csv
import io
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_TOKEN = os.environ.get("QUALTRICS_API_TOKEN", "")
DATACENTER = os.environ.get("QUALTRICS_DATACENTER_ID", "")

if not API_TOKEN or not DATACENTER:
    print("ERROR: QUALTRICS_API_TOKEN and QUALTRICS_DATACENTER_ID must be set.")
    print("Add them as GitHub Secrets in your repository settings.")
    sys.exit(1)

BASE_URL = f"https://{DATACENTER}.qualtrics.com/API/v3"
HEADERS = {
    "X-API-TOKEN": API_TOKEN,
    "Content-Type": "application/json",
}

EXPORT_POLL_INTERVAL = 3
EXPORT_POLL_MAX_WAIT = 120
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Qualtrics Response Export
# ---------------------------------------------------------------------------

def api_request(method, url, json_body=None):
    """Make an API request with retries."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, headers=HEADERS, json=json_body, timeout=30)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"  HTTP {resp.status_code} — retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
            else:
                raise
    return None


def create_response_export(survey_id, question_ids):
    """Step 1: Start a response export."""
    url = f"{BASE_URL}/surveys/{survey_id}/export-responses"
    payload = {
        "format": "csv",
        "questionIds": question_ids,
        "compress": True,
        "includeDisplayOrder": False,
        "limit": None,
        "useLabels": True,
    }
    result = api_request("POST", url, payload)
    progress_id = result["result"]["progressId"]
    print(f"  Export started — progressId={progress_id}")
    return progress_id


def poll_export_progress(survey_id, progress_id):
    """Step 2: Poll until the export is complete."""
    url = f"{BASE_URL}/surveys/{survey_id}/export-responses/{progress_id}"
    elapsed = 0
    while elapsed < EXPORT_POLL_MAX_WAIT:
        result = api_request("GET", url)
        status = result["result"]["status"]
        pct = result["result"].get("percentComplete", "?")

        if status == "complete":
            file_id = result["result"]["fileId"]
            print(f"  Export complete — fileId={file_id}")
            return file_id
        elif status == "failed":
            raise RuntimeError(f"Export failed for survey {survey_id}")

        print(f"  Export: {pct}% complete…")
        time.sleep(EXPORT_POLL_INTERVAL)
        elapsed += EXPORT_POLL_INTERVAL

    raise TimeoutError(f"Export timed out after {EXPORT_POLL_MAX_WAIT}s")


def download_export(survey_id, file_id):
    """Step 3: Download the ZIP file and extract the CSV."""
    url = f"{BASE_URL}/surveys/{survey_id}/export-responses/{file_id}/file"
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_filename = zf.namelist()[0]
        csv_bytes = zf.read(csv_filename)
        return csv_bytes.decode("utf-8-sig")


def extract_aliases_from_csv(csv_text, alias_question_id):
    """Parse CSV and extract unique aliases from the alias question column."""
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)

    if len(rows) < 4:
        return []

    header_row = rows[0]

    # Find the alias column
    alias_col_idx = None
    for idx, col_name in enumerate(header_row):
        if col_name.strip() == alias_question_id:
            alias_col_idx = idx
            break

    if alias_col_idx is None:
        for idx, col_name in enumerate(header_row):
            if alias_question_id in col_name.strip():
                alias_col_idx = idx
                break

    if alias_col_idx is None:
        print(f"  WARNING: Could not find column for '{alias_question_id}' in headers")
        return []

    # Extract aliases (skip 3 header rows)
    aliases = set()
    for row in rows[3:]:
        if alias_col_idx < len(row):
            val = row[alias_col_idx].strip().lower()
            if val:
                aliases.add(val)

    return sorted(aliases)


def process_survey(survey_id, alias_question_id):
    """Full export pipeline for one survey."""
    print(f"\nProcessing survey {survey_id}…")
    try:
        progress_id = create_response_export(survey_id, [alias_question_id])
        file_id = poll_export_progress(survey_id, progress_id)
        csv_text = download_export(survey_id, file_id)
        aliases = extract_aliases_from_csv(csv_text, alias_question_id)
        print(f"  Found {len(aliases)} completed aliases")
        return aliases
    except Exception as e:
        print(f"  ERROR: {e}")
        return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Read config
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    alias_qid = config["aliasQuestionId"]
    programs = config["programs"]

    print(f"Alias question ID: {alias_qid}")
    print(f"Programs: {len(programs)}")

    # Process each survey
    completions = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "programs": []
    }

    total_surveys = 0
    total_aliases = 0

    for program in programs:
        print(f"\n{'='*50}")
        print(f"Program: {program['name']}")
        print(f"{'='*50}")

        prog_data = {
            "name": program["name"],
            "surveys": []
        }

        for survey in program["surveys"]:
            total_surveys += 1
            aliases = process_survey(survey["id"], alias_qid)
            total_aliases += len(aliases)

            prog_data["surveys"].append({
                "id": survey["id"],
                "label": survey["label"],
                "due": survey["due"],
                "completedAliases": aliases
            })

        completions["programs"].append(prog_data)

    # Write completions.json
    output_path = os.path.join(os.path.dirname(__file__), "completions.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(completions, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*50}")
    print(f"DONE — {total_surveys} surveys, {total_aliases} total alias entries")
    print(f"Written to: {output_path}")
    print(f"Timestamp: {completions['lastUpdated']}")


if __name__ == "__main__":
    main()
