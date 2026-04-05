#!/usr/bin/env python3
"""
Dear Date Daily Analysis Script
Pulls analytics from PostHog + Supabase, runs rule-based analysis,
and writes a structured entry to a Notion database.
"""

import os
import sys
import json
from datetime import datetime, timedelta, timezone

import requests
from dateutil import parser as dateutil_parser

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POSTHOG_HOST = "https://us.posthog.com"
POSTHOG_PROJECT_ID = "355711"
POSTHOG_API_KEY = os.environ.get("POSTHOG_API_KEY", "")

SUPABASE_URL = "https://iflsmmrmlcngcvvweseo.supabase.co"
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = "99b63a76ad824864926bf22a274a4c71"

STRIPE_WEBHOOK_DONE_FLAG = os.path.join(
    os.path.dirname(__file__), "..", ".stripe_webhook_done"
)

TODAY = datetime.now(timezone.utc).date()
TODAY_ISO = TODAY.isoformat()
TODAY_DISPLAY = TODAY.strftime("%B %-d, %Y")  # e.g. "April 5, 2026"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def posthog_headers():
    return {"Authorization": f"Bearer {POSTHOG_API_KEY}"}


def supabase_headers():
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    }


def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def safe_get(url, headers, params=None, label="API"):
    """Make a GET request, return JSON or empty dict on failure."""
    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [WARN] {label} request failed: {e}")
        return {}


def safe_post(url, headers, json_body=None, label="API"):
    """Make a POST request, return JSON or empty dict on failure."""
    try:
        r = requests.post(url, headers=headers, json=json_body, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [WARN] {label} request failed: {e}")
        return {}


# ---------------------------------------------------------------------------
# 1. Deduplication — check Notion for today's entry
# ---------------------------------------------------------------------------


def check_already_ran():
    """Query Notion for an entry with today's Analysis Date. Return True if found."""
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    body = {
        "filter": {
            "property": "Analysis Date",
            "date": {"equals": TODAY_ISO},
        }
    }
    data = safe_post(url, notion_headers(), body, "Notion dedup query")
    results = data.get("results", [])
    return len(results) > 0


# ---------------------------------------------------------------------------
# 2. PostHog — pull last 14 days of events (need 7-day + prior 7-day)
# ---------------------------------------------------------------------------

TRACKED_EVENTS = [
    "entry_logged",
    "entry_form_started",
    "upgrade_modal_opened",
    "upgrade_clicked",
    "tab_viewed",
    "feedback_submitted",
]


def fetch_posthog_trends(date_from, date_to):
    """
    Fetch daily event counts from PostHog Trends API.
    Returns {event_name: {date_str: count, ...}, ...}
    """
    url = f"{POSTHOG_HOST}/api/projects/{POSTHOG_PROJECT_ID}/insights/trend/"
    series = [{"id": e, "kind": "EventsNode", "math": "total"} for e in TRACKED_EVENTS]

    params = {
        "events": json.dumps([{"id": e, "math": "total"} for e in TRACKED_EVENTS]),
        "date_from": date_from,
        "date_to": date_to,
        "display": "ActionsLineGraph",
    }

    data = safe_get(url, posthog_headers(), params, "PostHog trends")
    result = {}
    for series_item in data.get("result", []):
        event_name = series_item.get("label", series_item.get("action", {}).get("id", "unknown"))
        days = series_item.get("days", [])
        counts = series_item.get("data", [])
        day_counts = {}
        for d, c in zip(days, counts):
            day_counts[d] = int(c)
        result[event_name] = day_counts
    return result


def fetch_posthog_dau(date_from, date_to):
    """Fetch DAU (unique users) per day using the events endpoint."""
    url = f"{POSTHOG_HOST}/api/projects/{POSTHOG_PROJECT_ID}/insights/trend/"
    params = {
        "events": json.dumps([{"id": "$pageview", "math": "dau"}]),
        "date_from": date_from,
        "date_to": date_to,
    }
    data = safe_get(url, posthog_headers(), params, "PostHog DAU")
    for series_item in data.get("result", []):
        days = series_item.get("days", [])
        counts = series_item.get("data", [])
        return {d: int(c) for d, c in zip(days, counts)}
    return {}


# ---------------------------------------------------------------------------
# 3. Supabase — user & entry counts
# ---------------------------------------------------------------------------


def fetch_supabase_user_count():
    """Get total user count from auth.users via Supabase REST (RPC or direct)."""
    # Use the REST API on the entries table as a proxy; auth.users may not
    # be directly queryable via anon key. We'll try both approaches.
    # Approach 1: RPC (if a function exists)
    # Approach 2: direct count on entries table
    url = f"{SUPABASE_URL}/rest/v1/entries?select=id&limit=0"
    headers = supabase_headers()
    headers["Prefer"] = "count=exact"
    try:
        r = requests.get(url, headers=headers, timeout=15)
        entry_count = int(r.headers.get("content-range", "0/0").split("/")[-1])
    except Exception:
        entry_count = 0
    return entry_count


def fetch_supabase_recent_entries():
    """Fetch entries from the last 7 days for body_feel distribution."""
    seven_days_ago = (TODAY - timedelta(days=7)).isoformat()
    url = (
        f"{SUPABASE_URL}/rest/v1/entries"
        f"?select=body_feel,created_at"
        f"&created_at=gte.{seven_days_ago}"
        f"&order=created_at.desc"
        f"&limit=500"
    )
    data = safe_get(url, supabase_headers(), label="Supabase entries")
    if isinstance(data, list):
        return data
    return []


# ---------------------------------------------------------------------------
# 4. Analysis — trends, flags, health score
# ---------------------------------------------------------------------------


def compute_metrics(trends, dau_data):
    """Extract key metrics from PostHog data."""
    last_7 = [(TODAY - timedelta(days=i)).isoformat() for i in range(7)]
    prior_7 = [(TODAY - timedelta(days=i)).isoformat() for i in range(7, 14)]

    def sum_period(event, days):
        event_data = trends.get(event, {})
        return sum(event_data.get(d, 0) for d in days)

    def daily_values(event, days):
        event_data = trends.get(event, {})
        return [event_data.get(d, 0) for d in days]

    dau_last_7 = [dau_data.get(d, 0) for d in last_7]
    dau_prior_7 = [dau_data.get(d, 0) for d in prior_7]

    metrics = {
        "dau_today": dau_data.get(TODAY_ISO, 0),
        "dau_last_7": dau_last_7,
        "dau_7d_avg": round(sum(dau_last_7) / max(len(dau_last_7), 1), 1),
        "dau_prior_7d_avg": round(sum(dau_prior_7) / max(len(dau_prior_7), 1), 1),
        "entries_logged_today": sum_period("entry_logged", [TODAY_ISO]),
        "entries_logged_7d": sum_period("entry_logged", last_7),
        "entries_logged_prior_7d": sum_period("entry_logged", prior_7),
        "entry_form_started_7d": sum_period("entry_form_started", last_7),
        "upgrade_modal_opened_7d": sum_period("upgrade_modal_opened", last_7),
        "upgrade_clicked_7d": sum_period("upgrade_clicked", last_7),
        "upgrade_modal_opened_prior_7d": sum_period("upgrade_modal_opened", prior_7),
        "upgrade_clicked_prior_7d": sum_period("upgrade_clicked", prior_7),
        "feedback_7d": sum_period("feedback_submitted", last_7),
        "entries_daily_last_7": daily_values("entry_logged", last_7),
    }

    # Funnel: form started → entry logged completion rate
    if metrics["entry_form_started_7d"] > 0:
        metrics["form_completion_rate"] = round(
            metrics["entries_logged_7d"] / metrics["entry_form_started_7d"] * 100, 1
        )
    else:
        metrics["form_completion_rate"] = 0.0

    return metrics


def compute_health(metrics):
    """Determine health status based on rules."""
    dau_last_7 = metrics["dau_last_7"]

    # Critical: DAU = 0 for 2+ consecutive days
    consecutive_zero = 0
    for v in dau_last_7:
        if v == 0:
            consecutive_zero += 1
        else:
            consecutive_zero = 0
        if consecutive_zero >= 2:
            return "🔴 Critical", "DAU has been 0 for 2+ consecutive days"

    # Critical: upgrade_modal shown but 0 clicks for 7 days
    if (
        metrics["upgrade_modal_opened_7d"] > 0
        and metrics["upgrade_clicked_7d"] == 0
    ):
        return "🔴 Critical", "Upgrade modal shown but zero clicks all week — broken funnel"

    # Healthy: DAU ≥ 3 and entries logged this week
    if metrics["dau_today"] >= 3 and metrics["entries_logged_7d"] > 0:
        return "🟢 Healthy", f"DAU today = {metrics['dau_today']}, entries this week = {metrics['entries_logged_7d']}"

    return "🟡 Needs Attention", "Metrics don't meet healthy thresholds — review below"


def generate_suggestions(metrics):
    """Rule-based ranked suggestions."""
    suggestions = []

    # Priority 1: Critical infrastructure
    if not os.path.exists(STRIPE_WEBHOOK_DONE_FLAG):
        suggestions.append({
            "priority": 1,
            "category": "Critical Infrastructure",
            "text": "Stripe webhook (auto-unlock Plus on payment) is not yet built",
            "necessary": "Yes — payments won't auto-unlock without it",
        })

    # Priority 2: Broken funnels
    if (
        metrics["upgrade_modal_opened_7d"] > 0
        and metrics["upgrade_clicked_7d"] == 0
    ):
        suggestions.append({
            "priority": 2,
            "category": "Broken Funnel",
            "text": "Fix upgrade funnel: modal is showing but nobody clicks — review pricing page",
            "necessary": "Yes — direct revenue impact",
        })

    # Priority 3: Engagement issues
    zero_entry_streak = 0
    for v in metrics["entries_daily_last_7"]:
        if v == 0:
            zero_entry_streak += 1
        else:
            zero_entry_streak = 0

    if zero_entry_streak >= 3:
        suggestions.append({
            "priority": 3,
            "category": "Engagement",
            "text": "Investigate entry drop-off: check if form is working and visible",
            "necessary": "Yes — core action is stalled for 3+ days",
        })

    # DAU trending down 3 days in a row
    dau = metrics["dau_last_7"]
    if len(dau) >= 3 and dau[0] < dau[1] < dau[2]:
        suggestions.append({
            "priority": 3,
            "category": "Engagement",
            "text": "Re-engagement needed: consider posting on Instagram or sending email",
            "necessary": "Only if DAU trend continues downward",
        })

    # Priority 4: Growth
    if metrics["dau_7d_avg"] == 0 or (
        metrics["dau_7d_avg"] <= metrics["dau_prior_7d_avg"]
        and metrics["dau_7d_avg"] < 1
    ):
        suggestions.append({
            "priority": 4,
            "category": "Growth",
            "text": "Top of funnel is dry: no new signups this week",
            "necessary": "Monitor — growth matters once core loop is solid",
        })

    suggestions.sort(key=lambda s: s["priority"])
    return suggestions


# ---------------------------------------------------------------------------
# 5. Notion — write structured entry
# ---------------------------------------------------------------------------


def build_notion_blocks(metrics, health_status, health_reason, suggestions, supabase_entry_count, recent_entries):
    """Build Notion page body as an array of block objects."""
    blocks = []

    def heading2(text):
        return {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }

    def paragraph(text):
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }

    def divider():
        return {"object": "block", "type": "divider", "divider": {}}

    # --- PostHog Snapshot ---
    blocks.append(heading2("📊 PostHog Snapshot (Last 7 Days)"))
    snapshot_lines = [
        f"DAU Today: {metrics['dau_today']}  |  7-Day Avg: {metrics['dau_7d_avg']}  |  Prior 7-Day Avg: {metrics['dau_prior_7d_avg']}",
        f"Entries Logged (7d): {metrics['entries_logged_7d']}  |  Prior 7d: {metrics['entries_logged_prior_7d']}",
        f"Form Started → Logged Rate: {metrics['form_completion_rate']}%",
        f"Upgrade Modal Opened (7d): {metrics['upgrade_modal_opened_7d']}  |  Upgrade Clicked: {metrics['upgrade_clicked_7d']}",
        f"Feedback Submitted (7d): {metrics['feedback_7d']}",
    ]
    blocks.append(paragraph("\n".join(snapshot_lines)))
    blocks.append(divider())

    # --- Supabase Snapshot ---
    blocks.append(heading2("🗄️ Supabase Snapshot"))
    body_feel_dist = {}
    for entry in recent_entries:
        bf = entry.get("body_feel", "unknown") or "unknown"
        body_feel_dist[bf] = body_feel_dist.get(bf, 0) + 1
    bf_str = ", ".join(f"{k}: {v}" for k, v in sorted(body_feel_dist.items())) or "No data"
    blocks.append(paragraph(
        f"Total Entries in DB: {supabase_entry_count}\n"
        f"Entries Last 7 Days (body_feel distribution): {bf_str}"
    ))
    blocks.append(divider())

    # --- Trend Analysis ---
    blocks.append(heading2("📈 Trend Analysis"))
    dau_delta = metrics["dau_7d_avg"] - metrics["dau_prior_7d_avg"]
    dau_dir = "↑" if dau_delta > 0 else ("↓" if dau_delta < 0 else "→")
    entries_delta = metrics["entries_logged_7d"] - metrics["entries_logged_prior_7d"]
    entries_dir = "↑" if entries_delta > 0 else ("↓" if entries_delta < 0 else "→")
    blocks.append(paragraph(
        f"DAU Trend: {dau_dir} ({dau_delta:+.1f} vs prior week)\n"
        f"Entries Trend: {entries_dir} ({entries_delta:+d} vs prior week)\n"
        f"Form Completion Rate: {metrics['form_completion_rate']}%"
    ))
    blocks.append(divider())

    # --- Health Score ---
    blocks.append(heading2("🏥 Health Score"))
    blocks.append(paragraph(f"{health_status}\nRationale: {health_reason}"))
    blocks.append(divider())

    # --- Suggestions ---
    blocks.append(heading2("💡 Ranked Suggestions"))
    if suggestions:
        for i, s in enumerate(suggestions, 1):
            blocks.append(paragraph(
                f"{i}. [{s['category']}] {s['text']}\n"
                f"   Is this necessary? {s['necessary']}"
            ))
    else:
        blocks.append(paragraph("No actionable suggestions today — metrics are stable."))

    return blocks


def write_to_notion(metrics, health_status, health_reason, suggestions, supabase_entry_count, recent_entries):
    """Create a new page in the Notion database."""
    top_suggestion = suggestions[0]["text"] if suggestions else "No action needed today"

    url = "https://api.notion.com/v1/pages"
    body = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Date": {
                "title": [{"text": {"content": TODAY_DISPLAY}}]
            },
            "Health": {
                "select": {"name": health_status}
            },
            "Daily Active Users": {
                "number": metrics["dau_today"]
            },
            "Entries Logged": {
                "number": metrics["entries_logged_today"]
            },
            "Upgrade Clicks": {
                "number": metrics["upgrade_clicked_7d"]
            },
            "Top Suggestion": {
                "rich_text": [{"text": {"content": top_suggestion[:2000]}}]
            },
            "Action Status": {
                "select": {"name": "📋 Pending"}
            },
            "Instagram Notes": {
                "rich_text": []
            },
            "Skip Reason": {
                "rich_text": []
            },
            "Analysis Date": {
                "date": {"start": TODAY_ISO}
            },
        },
        "children": build_notion_blocks(
            metrics, health_status, health_reason, suggestions,
            supabase_entry_count, recent_entries
        ),
    }

    resp = safe_post(url, notion_headers(), body, "Notion create page")
    if resp.get("id"):
        print(f"  ✅ Notion page created: {resp['id']}")
        return True
    else:
        print(f"  ❌ Failed to create Notion page: {resp}")
        return False


# ---------------------------------------------------------------------------
# 6. Stdout summary
# ---------------------------------------------------------------------------


def print_summary(metrics, health_status, health_reason, suggestions, supabase_entry_count):
    """Print a clean summary for GitHub Actions logs."""
    print("\n" + "=" * 60)
    print(f"  Dear Date Daily Analysis — {TODAY_DISPLAY}")
    print("=" * 60)
    print(f"\n  Health: {health_status}")
    print(f"  Reason: {health_reason}")
    print(f"\n  DAU Today:              {metrics['dau_today']}")
    print(f"  DAU 7-Day Avg:          {metrics['dau_7d_avg']}")
    print(f"  Entries Logged Today:   {metrics['entries_logged_today']}")
    print(f"  Entries Logged (7d):    {metrics['entries_logged_7d']}")
    print(f"  Form Completion Rate:   {metrics['form_completion_rate']}%")
    print(f"  Upgrade Modal (7d):     {metrics['upgrade_modal_opened_7d']}")
    print(f"  Upgrade Clicks (7d):    {metrics['upgrade_clicked_7d']}")
    print(f"  Total Entries in DB:    {supabase_entry_count}")
    print(f"\n  Suggestions:")
    if suggestions:
        for i, s in enumerate(suggestions, 1):
            print(f"    {i}. [{s['category']}] {s['text']}")
    else:
        print("    No actionable suggestions today.")
    print("\n" + "=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting Dear Date daily analysis...")

    # Validate env vars
    missing = []
    if not POSTHOG_API_KEY:
        missing.append("POSTHOG_API_KEY")
    if not SUPABASE_ANON_KEY:
        missing.append("SUPABASE_ANON_KEY")
    if not NOTION_TOKEN:
        missing.append("NOTION_TOKEN")
    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        sys.exit(1)

    # Step 1: Dedup check
    print("Checking for existing entry in Notion...")
    if check_already_ran():
        print("Already ran today, skipping.")
        sys.exit(0)

    # Step 2: Fetch PostHog data (14 days for comparison)
    date_from = (TODAY - timedelta(days=13)).isoformat()
    date_to = TODAY_ISO
    print("Fetching PostHog trends...")
    trends = fetch_posthog_trends(date_from, date_to)
    print("Fetching PostHog DAU...")
    dau_data = fetch_posthog_dau(date_from, date_to)

    # Step 3: Fetch Supabase data
    print("Fetching Supabase data...")
    supabase_entry_count = fetch_supabase_user_count()
    recent_entries = fetch_supabase_recent_entries()

    # Step 4: Compute metrics & analysis
    print("Computing metrics...")
    metrics = compute_metrics(trends, dau_data)
    health_status, health_reason = compute_health(metrics)
    suggestions = generate_suggestions(metrics)

    # Step 5: Print summary to stdout
    print_summary(metrics, health_status, health_reason, suggestions, supabase_entry_count)

    # Step 6: Write to Notion
    print("\nWriting to Notion...")
    success = write_to_notion(
        metrics, health_status, health_reason, suggestions,
        supabase_entry_count, recent_entries,
    )

    if success:
        print("\n✅ Daily analysis complete!")
    else:
        print("\n⚠️  Analysis computed but Notion write failed. Check logs above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
