"""Push no-website leads from a CSV into Kenji AI (GoHighLevel) as contacts.

Dry run is the default. Pass --push to actually create contacts.
Skips any phone number already in your Kenji account.
Tracks pushed phones in pushed.json so re-runs never duplicate.

Usage:
    python push_to_kenji.py examples/sample.csv        # dry run, shows what would happen
    python push_to_kenji.py leads.csv --push            # create contacts for real

Requirements:
    KENJI_API_KEY and KENJI_LOCATION_ID in .env (see README).

Tags applied: no-website, maps-finder
Source field: no-website-finder

NEVER enrolls contacts in workflows. NEVER sends messages.
"""
import argparse
import csv
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("requests is not installed. Run: pip install requests")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is optional; env vars can be set directly

API_BASE = "https://services.leadconnectorhq.com"
TAGS = ["no-website", "maps-finder"]
TRACK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pushed.json")


def get_config():
    key = os.environ.get("KENJI_API_KEY", "")
    loc = os.environ.get("KENJI_LOCATION_ID", "")
    if not key or not loc:
        sys.exit(
            "Set KENJI_API_KEY and KENJI_LOCATION_ID in your .env file.\n"
            "See README, Part 1, Step 3."
        )
    return key, loc


def headers(key):
    return {
        "Authorization": f"Bearer {key}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def api(method, path, key, **kwargs):
    """Single-method API call with simple rate-limit back-off."""
    for attempt in range(3):
        try:
            r = requests.request(
                method, API_BASE + path,
                headers=headers(key),
                timeout=30,
                **kwargs,
            )
            if r.status_code == 429:
                print("  rate limited, waiting 5 s...")
                time.sleep(5)
                continue
            return r
        except requests.RequestException as exc:
            if attempt == 2:
                raise
            print(f"  network error ({exc}), retrying...")
            time.sleep(3)
    raise RuntimeError(f"Request failed after retries: {path}")


def to_e164(raw):
    """Convert any US phone string to +1XXXXXXXXXX, or return '' if too short."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 10:
        return "+1" + digits[-10:]
    return ""


def parse_city_state(address):
    """Return (city, state) from a 'street, city, state zip' address string."""
    parts = [s.strip() for s in address.split(",")]
    if len(parts) >= 3:
        city = parts[-2]
        state = (parts[-1].split() or [""])[0]
        return city, state
    return "", ""


def already_in_kenji(phone, key, loc):
    """Return True if a contact with this phone already exists."""
    r = api("GET", "/contacts/search/duplicate", key,
            params={"locationId": loc, "number": phone})
    if r.ok:
        return bool((r.json().get("contact") or {}).get("id"))
    return False


def build_contact(row, loc):
    city, state = parse_city_state(row.get("address", ""))
    return {
        "locationId":   loc,
        "companyName":  row["name"],
        "firstName":    row["name"][:40],
        "phone":        to_e164(row["phone"]),
        "address1":     row.get("address", "").split(",")[0],
        "city":         city,
        "state":        state,
        "tags":         TAGS,
        "source":       "no-website-finder",
    }


def build_note(row):
    rating_line = str(row.get("rating", "")) or "none yet"
    new_flag = " (new listing, no reviews yet)" if row.get("new_listing") == "yes" else ""
    return (
        f"No-website lead from Google Maps ({row.get('query', '')}).\n"
        f"Category: {row.get('category', '')}\n"
        f"Site status: {row.get('site_status', '')} {row.get('website', '')}\n"
        f"Rating: {rating_line}{new_flag}\n"
        f"Address: {row.get('address', '')}\n"
        f"Maps: {row.get('maps_url', '')}\n\n"
        "Next step: call or email first. Only text a website preview after they say yes."
    )


def parse_args():
    p = argparse.ArgumentParser(
        description="Push no-website leads into Kenji AI as contacts."
    )
    p.add_argument("csv_file", help="CSV from find.py")
    p.add_argument("--push", action="store_true",
                   help="Actually create contacts. Without this flag the script only shows what it would do.")
    return p.parse_args()


def main():
    args = parse_args()
    dry = not args.push

    if dry:
        print("DRY RUN - nothing will be sent to Kenji. Pass --push to create contacts.")
    else:
        print("LIVE RUN - contacts will be created in Kenji.")

    key, loc = get_config()

    pushed = {}
    if os.path.exists(TRACK_FILE):
        with open(TRACK_FILE) as f:
            pushed = json.load(f)

    new_count = skipped_count = 0

    with open(args.csv_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            phone = to_e164(row.get("phone", ""))
            if not phone:
                print(f"  skip (no phone): {row.get('name', '?')}")
                continue
            if phone in pushed:
                print(f"  skip (already pushed): {row.get('name', '?')}")
                continue

            if already_in_kenji(phone, key, loc):
                skipped_count += 1
                pushed[phone] = "existing"
                print(f"  skip (already in Kenji): {row['name']}")
                continue

            contact = build_contact(row, loc)

            if dry:
                new_count += 1
                print(f"  would create: {row['name']}  {phone}")
                continue

            r = api("POST", "/contacts/", key, json=contact)
            if not r.ok:
                print(f"  FAIL {row['name']}: {r.status_code} {r.text[:150]}")
                continue

            contact_id = r.json()["contact"]["id"]
            note_body = build_note(row)
            api("POST", f"/contacts/{contact_id}/notes", key, json={"body": note_body})

            pushed[phone] = contact_id
            new_count += 1
            print(f"  created: {row['name']}")

            with open(TRACK_FILE, "w") as tf:
                json.dump(pushed, tf)

            time.sleep(0.5)

    if not dry:
        with open(TRACK_FILE, "w") as tf:
            json.dump(pushed, tf)

    verb = "would create" if dry else "created"
    print(f"\n{verb} {new_count} contact(s), skipped {skipped_count} already in Kenji.")
    if dry:
        print("Run with --push to send for real.")


if __name__ == "__main__":
    main()
