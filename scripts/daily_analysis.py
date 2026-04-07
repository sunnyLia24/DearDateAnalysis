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
NOTION_ANALYTICS_PAGE_ID = "3397f77c9b3881f7b6e4e5889925e999"

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
        if not r.ok:
            print(f"  [WARN] {label} returned HTTP {r.status_code}: {r.text[:500]}")
            return {}
        return r.json()
    except Exception as e:
        print(f"  [WARN] {label} request failed: {e}")
        return {}


def safe_post(url, headers, json_body=None, label="API"):
    """Make a POST request, return JSON or empty dict on failure."""
    try:
        r = requests.post(url, headers=headers, json=json_body, timeout=30)
        if not r.ok:
            print(f"  [WARN] {label} returned HTTP {r.status_code}: {r.text[:500]}")
            return {}
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
    if not data:
        print("  [WARN] Could not check for duplicates — proceeding anyway")
        return False
    results = data.get("results", [])
    if results:
        print(f"  Found {len(results)} existing entry for today — skipping.")
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


def fetch_posthog_tab_views(date_from, date_to):
    """Fetch tab_viewed counts broken down by tab name property."""
    url = f"{POSTHOG_HOST}/api/projects/{POSTHOG_PROJECT_ID}/insights/trend/"
    params = {
        "events": json.dumps([{
            "id": "tab_viewed",
            "math": "total",
            "properties": [],
        }]),
        "breakdown": "tab name",
        "breakdown_type": "event",
        "date_from": date_from,
        "date_to": date_to,
    }
    data = safe_get(url, posthog_headers(), params, "PostHog tab views")
    tab_counts = {}
    for series_item in data.get("result", []):
        label = series_item.get("breakdown_value", series_item.get("label", "unknown"))
        total = sum(int(c) for c in series_item.get("data", []))
        if label and label != "unknown":
            tab_counts[label] = total
    return tab_counts


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

    try:
        r = requests.post(url, headers=notion_headers(), json=body, timeout=30)
        print(f"  Notion API response: HTTP {r.status_code}")
        if not r.ok:
            print(f"  ❌ Notion API error: {r.text[:1000]}")
            return False
        resp = r.json()
        if resp.get("id"):
            print(f"  ✅ Notion page created: {resp['id']}")
            return True
        else:
            print(f"  ❌ Unexpected Notion response (no id): {json.dumps(resp)[:500]}")
            return False
    except Exception as e:
        print(f"  ❌ Notion request exception: {e}")
        return False


# ---------------------------------------------------------------------------
# 5b. Analytics page — refresh "dear date — analytics"
# ---------------------------------------------------------------------------


def _clear_page_blocks(page_id):
    """Delete all existing content blocks from a Notion page."""
    url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
    data = safe_get(url, notion_headers(), label="Notion list blocks")
    block_ids = [b["id"] for b in data.get("results", []) if b.get("id")]
    for bid in block_ids:
        try:
            r = requests.delete(
                f"https://api.notion.com/v1/blocks/{bid}",
                headers=notion_headers(), timeout=15,
            )
            if not r.ok:
                print(f"  [WARN] Failed to delete block {bid}: HTTP {r.status_code}")
        except Exception as e:
            print(f"  [WARN] Failed to delete block {bid}: {e}")
    print(f"  Cleared {len(block_ids)} blocks from analytics page.")


def _bar(pct, width=20):
    """Render a text progress bar."""
    filled = round(pct / 100 * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def build_analytics_blocks(metrics, suggestions, tab_counts):
    """Build the content blocks for the analytics page."""
    blocks = []

    def heading2(text):
        return {
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }

    def paragraph(text):
        return {
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }

    def divider():
        return {"object": "block", "type": "divider", "divider": {}}

    # Header
    blocks.append(paragraph(
        f"\U0001f48c dear date analytics \u00b7 last refreshed {TODAY_DISPLAY} "
        f"\u00b7 updates every morning at 9 AM ET"
    ))
    blocks.append(divider())

    # At a Glance
    blocks.append(heading2("\u26a1 At a glance \u2014 last 7 days"))

    dau_signal = f"today: {metrics['dau_today']}, 7d avg: {metrics['dau_7d_avg']}"
    form_signal = ""
    if metrics['dau_7d_avg'] > 0:
        form_signal = f"{round(metrics['entry_form_started_7d'] / (metrics['dau_7d_avg'] * 7), 1)} attempts per user" if metrics['dau_7d_avg'] > 0 else ""

    entries_signal = ""
    if metrics['entries_logged_7d'] == 0:
        entries_signal = "\U0001f6a8 zero entries this week"
    elif metrics['entries_logged_7d'] < 3:
        entries_signal = "\U0001f6a8 very low"

    glance_lines = [
        f"Metric              | Value  | Signal",
        f"--------------------|--------|---------------------------",
        f"Avg daily users     | {metrics['dau_7d_avg']}    | {dau_signal}",
        f"Forms started       | {metrics['entry_form_started_7d']}     | {form_signal}",
        f"Entries logged      | {metrics['entries_logged_7d']}     | {entries_signal}",
        f"Upgrade clicks      | {metrics['upgrade_clicked_7d']}     | modal opened: {metrics['upgrade_modal_opened_7d']}",
    ]
    blocks.append(paragraph("\n".join(glance_lines)))
    blocks.append(divider())

    # Form Funnel
    blocks.append(heading2("\U0001f53d Form funnel"))
    form_started = metrics["entry_form_started_7d"]
    entries_logged = metrics["entries_logged_7d"]
    upgrade_clicked = metrics["upgrade_clicked_7d"]

    if form_started > 0:
        entry_rate = round(entries_logged / form_started * 100)
        entry_bar = _bar(entry_rate)
        flag = " \U0001f6a8" if entry_rate < 30 else ""
    else:
        entry_rate = 0
        entry_bar = _bar(0)
        flag = ""

    funnel_lines = [
        f"Step             | Count | Bar                  | Rate",
        f"-----------------|-------|----------------------|------",
        f"Form started     | {form_started:>5} | {_bar(100)}  | 100%",
        f"Entry logged     | {entries_logged:>5} | {entry_bar}  | {entry_rate}%{flag}",
        f"Upgrade clicked  | {upgrade_clicked:>5} | {_bar(min(100, (upgrade_clicked / max(form_started, 1)) * 100))}  | \u2014",
    ]
    blocks.append(paragraph("\n".join(funnel_lines)))

    if form_started > 0 and entry_rate < 30:
        blocks.append(paragraph(
            f"\U0001f6a8 {100 - entry_rate}% of users who open the form never finish it. "
            f"Consider adding step_completed PostHog events to identify the drop-off point."
        ))
    blocks.append(divider())

    # Where Users Go (tab views)
    blocks.append(heading2("\U0001f4d1 Where users go"))
    if tab_counts:
        max_views = max(tab_counts.values()) if tab_counts else 1
        tab_lines = [
            "Tab              | Views | Bar",
            "-----------------|-------|----------------------",
        ]
        for tab_name, views in sorted(tab_counts.items(), key=lambda x: -x[1]):
            pct = round(views / max_views * 100)
            tab_lines.append(f"{tab_name:<16} | {views:>5} | {_bar(pct)} {pct}%")
        blocks.append(paragraph("\n".join(tab_lines)))
    else:
        blocks.append(paragraph("No tab_viewed events recorded this week."))
    blocks.append(divider())

    # Issues to fix (from suggestions)
    blocks.append(heading2("\U0001f6a8 Issues to fix"))
    if suggestions:
        for i, s in enumerate(suggestions, 1):
            severity = "\U0001f534" if s["priority"] <= 2 else ("\U0001f7e1" if s["priority"] == 3 else "\U0001f535")
            blocks.append(paragraph(
                f"{severity} #{i} \u00b7 {s['text']}\n"
                f"   Necessary? {s['necessary']}"
            ))
    else:
        blocks.append(paragraph("No critical issues detected today."))
    blocks.append(divider())

    # Today's suggestion
    blocks.append(heading2("\U0001f4a1 Today's suggestion"))
    top = suggestions[0]["text"] if suggestions else "No action needed today \u2014 metrics are stable."
    blocks.append(paragraph(f"\u2728 {top}"))
    blocks.append(divider())

    # Footer links
    blocks.append(paragraph(
        "\u2192 \U0001f4cb Daily Analysis Log \u00b7 \U0001f916 Automation Setup"
    ))

    return blocks


def refresh_analytics_page(metrics, suggestions, tab_counts):
    """Clear and rewrite the 'dear date — analytics' Notion page."""
    print("  Clearing old analytics page content...")
    _clear_page_blocks(NOTION_ANALYTICS_PAGE_ID)

    print("  Writing new analytics content...")
    blocks = build_analytics_blocks(metrics, suggestions, tab_counts)

    # Notion API allows max 100 blocks per append call
    url = f"https://api.notion.com/v1/blocks/{NOTION_ANALYTICS_PAGE_ID}/children"
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i + 100]
        try:
            r = requests.patch(url, headers=notion_headers(), json={"children": chunk}, timeout=30)
            print(f"  Analytics page append (blocks {i}-{i+len(chunk)-1}): HTTP {r.status_code}")
            if not r.ok:
                print(f"  ❌ Error: {r.text[:500]}")
                return False
        except Exception as e:
            print(f"  ❌ Exception appending blocks: {e}")
            return False

    print("  ✅ Analytics page refreshed.")
    return True


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

    # Step 1: Dedup check (only for database entry — analytics page always refreshes)
    print("Checking for existing entry in Notion...")
    already_ran = check_already_ran()

    # Step 2: Fetch PostHog data (14 days for comparison)
    date_from = (TODAY - timedelta(days=13)).isoformat()
    date_to = TODAY_ISO
    print("Fetching PostHog trends...")
    trends = fetch_posthog_trends(date_from, date_to)
    print("Fetching PostHog DAU...")
    dau_data = fetch_posthog_dau(date_from, date_to)
    print("Fetching PostHog tab views...")
    tab_counts = fetch_posthog_tab_views(date_from, date_to)

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

    # Step 6: Write to Notion database (skip if already ran today)
    if already_ran:
        print("\nDatabase entry already exists for today — skipping write.")
        success = True
    else:
        print("\nWriting to Notion database...")
        success = write_to_notion(
            metrics, health_status, health_reason, suggestions,
            supabase_entry_count, recent_entries,
        )

    # Step 7: Always refresh analytics page
    print("\nRefreshing analytics page...")
    analytics_ok = refresh_analytics_page(metrics, suggestions, tab_counts)

    if success and analytics_ok:
        print("\n✅ Daily analysis complete! Database entry + analytics page refreshed.")
    elif success:
        print("\n⚠️  Database entry OK but analytics page refresh failed.")
    else:
        print("\n⚠️  Analysis computed but Notion write failed. Check logs above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
