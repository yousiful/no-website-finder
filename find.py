"""Find local businesses on Google Maps that have no website (or only a Facebook, Instagram, or Yelp page).

Usage:
    python find.py "mobile detailing Austin TX" "mobile detailing Miami FL"
    python find.py --queries-file queries.txt --max-per-search 20 --out leads.csv

Options:
    --queries-file FILE    Text file with one search query per line
    --max-per-search N     Max listings to scan per query (default: 60)
    --out FILE             Output CSV path (default: leads.csv)
    --headful              Show the browser window (useful for debugging)
    --delay SECONDS        Base delay between requests, in seconds (default: 1.5)

Output columns:
    query, name, category, phone, site_status, website, address, rating, new_listing, maps_url
"""
import argparse
import csv
import os
import re
import sys
import time
import random
import urllib.parse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Playwright is not installed. Run: pip install playwright && playwright install chromium")

# URLs that look like websites but are just directory / social pages
SOCIAL = re.compile(
    r"facebook\.com|instagram\.com|yelp\.com|linktr\.ee|nextdoor\.com"
    r"|business\.site|g\.page|tiktok\.com|twitter\.com|x\.com",
    re.I,
)

# Patterns Google uses when it thinks you're automated
BLOCKED = re.compile(r"sorry\.google\.com|/sorry/|detected unusual traffic", re.I)


def parse_args():
    p = argparse.ArgumentParser(
        description="Find businesses on Google Maps with no real website."
    )
    p.add_argument("queries", nargs="*", help="One or more search queries, e.g. \"mobile detailing\" \"Austin TX\"")
    p.add_argument("--queries-file", metavar="FILE", help="Text file with one query per line")
    p.add_argument("--max-per-search", type=int, default=60, metavar="N",
                   help="Max listings to look at per query (default: 60)")
    p.add_argument("--out", default="leads.csv", metavar="FILE",
                   help="Output CSV file (default: leads.csv)")
    p.add_argument("--headful", action="store_true",
                   help="Show the browser window")
    p.add_argument("--delay", type=float, default=1.5, metavar="SECONDS",
                   help="Base seconds to wait between requests (default: 1.5)")
    return p.parse_args()


def load_queries(args):
    queries = list(args.queries or [])
    if args.queries_file:
        with open(args.queries_file, encoding="utf-8") as f:
            queries += [line.strip() for line in f if line.strip()]
    if not queries:
        sys.exit("No queries given. Pass them as arguments or use --queries-file.")
    return queries


def jitter(base):
    """Sleep for base ± 40 % to look less robotic."""
    time.sleep(base * random.uniform(0.6, 1.4))


def check_for_block(page, url=""):
    """Return True if Google is showing a CAPTCHA / unusual traffic page."""
    if BLOCKED.search(page.url):
        return True
    if BLOCKED.search(url):
        return True
    if page.locator("text=unusual traffic").count():
        return True
    return False


def launch_browser(pw, headless):
    """Try real Chrome first; fall back to bundled Chromium."""
    try:
        browser = pw.chromium.launch(channel="chrome", headless=headless)
        print("Using system Chrome.")
        return browser
    except Exception:
        print("System Chrome not found. Using bundled Playwright Chromium.")
        return pw.chromium.launch(headless=headless)


def scroll_feed(page, max_cards, delay):
    """Scroll the Maps results feed until we have enough cards or reach the end."""
    for _ in range(20):
        cards = page.eval_on_selector_all(
            'div[role="feed"] div.Nv2PK',
            """els => els.map(e => {
                const a = e.querySelector('a[href*="/maps/place/"]');
                const s = e.querySelector('a[data-value="Website"]');
                const st = e.querySelector('[role=img][aria-label*="star"]');
                return {
                    url:   a  ? a.href.split('?')[0] : '',
                    site:  s  ? s.href : '',
                    stars: st ? parseFloat(st.getAttribute('aria-label')) : null
                };
            })""",
        )
        end = page.locator("text=You've reached the end of the list").count()
        if len(cards) >= max_cards or end:
            return cards
        page.eval_on_selector('div[role="feed"]', "f => f.scrollBy(0, 4000)")
        jitter(delay)
    return cards


def scrape_listing(page, card, query, delay):
    """Open one Maps listing page and extract contact details."""
    page.goto(card["url"] + "?hl=en", timeout=45000)
    page.wait_for_selector("h1", timeout=15000)
    jitter(delay)

    if check_for_block(page):
        return None, True  # (row, blocked)

    if page.locator("text=Permanently closed").count() or page.locator("text=Temporarily closed").count():
        return None, False

    def attr(sel):
        loc = page.locator(sel)
        if loc.count():
            return loc.first.get_attribute("aria-label") or ""
        return ""

    phone_raw = attr('button[data-item-id^="phone:"]')
    phone = re.sub(r"^Phone:\s*", "", phone_raw).strip()
    if not phone:
        return None, False

    site_loc = page.locator('a[data-item-id="authority"]')
    site = site_loc.first.get_attribute("href") if site_loc.count() else ""
    if site and not SOCIAL.search(site):
        # Has a real website after all; skip
        return None, False

    cat_loc = page.locator('button[jsaction*="category"]')
    cat = cat_loc.first.inner_text() if cat_loc.count() else ""

    address_raw = attr('button[data-item-id="address"]')
    address = re.sub(r"^Address:\s*", "", address_raw).strip()

    name = page.locator("h1").first.inner_text().strip()

    row = {
        "query":       query,
        "name":        name,
        "category":    cat,
        "phone":       phone,
        "site_status": "social only" if site else "none",
        "website":     site or "",
        "address":     address,
        "rating":      card["stars"] or "",
        "new_listing": "yes" if not card["stars"] else "",
        "maps_url":    card["url"],
    }
    return row, False


FIELDNAMES = ["query", "name", "category", "phone", "site_status", "website",
              "address", "rating", "new_listing", "maps_url"]


def save(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    args = parse_args()
    queries = load_queries(args)

    rows = []
    seen = set()
    if os.path.exists(args.out):
        rows = list(csv.DictReader(open(args.out, encoding="utf-8")))
        seen = {r["maps_url"] for r in rows}
        print(f"Resuming: {len(rows)} rows already in {args.out}")

    blocked_global = False

    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=not args.headful)
        page = browser.new_page(locale="en-US", viewport={"width": 1300, "height": 900})

        for query in queries:
            if blocked_global:
                print("Stopped early: Google detected unusual traffic. Wait a few hours before retrying.")
                break

            search_url = "https://www.google.com/maps/search/" + urllib.parse.quote_plus(query) + "?hl=en"
            page.goto(search_url, timeout=60000)

            if check_for_block(page):
                print("Google is blocking requests. Stopping now. Wait a few hours before retrying.")
                blocked_global = True
                break

            try:
                page.wait_for_selector('div[role="feed"]', timeout=20000)
            except Exception:
                print(f"No results feed for: {query}")
                continue

            all_cards = scroll_feed(page, args.max_per_search, args.delay)

            candidates = [
                c for c in all_cards[: args.max_per_search]
                if c["url"] and c["url"] not in seen and (not c["site"] or SOCIAL.search(c["site"]))
            ]
            print(f"{query}: {len(all_cards)} listings, {len(candidates)} without a real website")

            for card in candidates:
                seen.add(card["url"])
                try:
                    row, blocked = scrape_listing(page, card, query, args.delay)
                except Exception as exc:
                    print(f"  skip ({type(exc).__name__})")
                    continue

                if blocked:
                    print("Google is blocking requests. Stopping now. Wait a few hours before retrying.")
                    blocked_global = True
                    break

                if row:
                    rows.append(row)
                    status_label = row["site_status"]
                    rating_label = str(row["rating"]) if row["rating"] else "no reviews yet"
                    print(f"  {len(rows)}. {row['name']} | {status_label} | {rating_label}")

                save(rows, args.out)
                jitter(args.delay)

        browser.close()

    save(rows, args.out)
    print(f"\nDone. {len(rows)} leads saved to {args.out}")


if __name__ == "__main__":
    main()
