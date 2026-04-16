#!/usr/bin/env python3
"""
Dear Date — Daily Analytics Runner
Runs each morning via GitHub Actions.
Pulls PostHog data → scores health → asks Claude for a suggestion → logs to Notion.
"""
import os, requests
from datetime import date

# ─── Config ───────────────────────────────────────────────────────────────────
POSTHOG_HOST        = "https://us.posthog.com"
POSTHOG_PROJECT_ID  = "355711"
POSTHOG_API_KEY     = os.environ["POSTHOG_API_KEY"]
NOTION_TOKEN        = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID  = "99b63a76ad824864926bf22a274a4c71"
NOTION_PAGE_ID      = "3397f77c9b3881f7b6e4e5889925e999"  # live analytics dashboard
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
TODAY               = date.today().isoformat()
WEBHOOK_URL         = "https://iflsmmrmlcngcvvweseo.supabase.co/functions/v1/stripe-webhook"
# ──────────────────────────────────────────────────────────────────────────────


def posthog_query(query: dict) -> dict:
    r = requests.post(
        f"{POSTHOG_HOST}/api/projects/{POSTHOG_PROJECT_ID}/query",
        headers={"Authorization": f"Bearer {POSTHOG_API_KEY}", "Content-Type": "application/json"},
        json={"query": query},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_metric_total(event: str, math: str = "dau", days: int = 7) -> int:
    result = posthog_query({
        "kind": "TrendsQuery",
        "series": [{"kind": "EventsNode", "event": event, "math": math}],
        "dateRange": {"date_from": f"-{days}d"},
        "interval": "day",
    })
    data = result.get("results", [{}])[0].get("data", [])
    return int(sum(data))


def get_form_completion_rate() -> float:
    result = posthog_query({
        "kind": "FunnelsQuery",
        "series": [
            {"kind": "EventsNode", "event": "entry_form_started"},
            {"kind": "EventsNode", "event": "entry_logged"},
        ],
        "dateRange": {"date_from": "-7d"},
        "funnelsFilter": {"funnelWindowInterval": 7, "funnelWindowIntervalUnit": "day"},
    })
    steps = result.get("results", [])
    if len(steps) < 2 or steps[0].get("count", 0) == 0:
        return 0.0
    return round(steps[1]["count"] / steps[0]["count"] * 100, 1)


def check_webhook_live() -> bool:
    """Returns True if the Supabase stripe-webhook function is responding correctly.
    A 400 'Missing stripe-signature header' means the function is live and healthy.
    A 404 or connection error means it's down.
    """
    try:
        r = requests.post(
            WEBHOOK_URL,
            json={"test": True},
            timeout=10,
        )
        # 400 = function is live, correctly rejecting unsigned requests
        # 405 = also live (method not allowed)
        return r.status_code in (400, 405)
    except Exception:
        return False


def score_health(dau: int, entries: int, form_rate: float, upgrade_clicks: int) -> str:
    if dau == 0 and entries == 0:
        return "🔴 Critical"
    if form_rate >= 30 and dau >= 5:
        return "🟢 Healthy"
    return "🟡 Needs Attention"


def build_issues(metrics: dict, webhook_live: bool) -> list[dict]:
    """Dynamically build the issues list based on actual state."""
    issues = []

    # Webhook check — based on real HTTP probe, not assumption
    if not webhook_live:
        issues.append({
            "severity": "🔴",
            "text": "Stripe webhook (Supabase Edge Function) is not responding — check function logs at supabase.com/dashboard/project/iflsmmrmlcngcvvweseo/functions",
            "necessary": "Yes — payments won't auto-unlock without it",
        })

    # Form drop-off
    if metrics["form_started"] > 0 and metrics["form_rate"] < 50:
        issues.append({
            "severity": "🟡",
            "text": f"Form completion rate is low ({metrics['form_rate']}%) — investigate which step is causing drop-off",
            "necessary": "Yes — core action is stalling",
        })
    elif metrics["entries"] == 0:
        issues.append({
            "severity": "🟡",
            "text": "Zero entries logged this week — check if form is visible and working",
            "necessary": "Yes — core action is stalled",
        })

    # Top of funnel
    if metrics["dau"] == 0:
        issues.append({
            "severity": "🔵",
            "text": "No active users this week — top of funnel is dry",
            "necessary": "Monitor — growth matters once core loop is solid",
        })

    return issues


def format_bar(value: int, total: int, width: int = 20) -> str:
    if total == 0:
        return "░" * width
    filled = round((value / total) * width)
    return "█" * filled + "░" * (width - filled)


def generate_suggestion(metrics: dict, webhook_live: bool) -> str:
    webhook_status = "deployed and live at Supabase (confirmed responding)" if webhook_live else "NOT responding — check Supabase function logs"
    prompt = f"""You are the growth advisor for Dear Date, a private dating journal web app.
Here are today's PostHog metrics (last 7 days):
- Daily active users (avg): {metrics['dau']}
- Forms started: {metrics['form_started']}
- Entries logged: {metrics['entries']}
- Form completion rate: {metrics['form_rate']}%
- Upgrade modal opened: {metrics['upgrade_modals']}
- Upgrade clicked: {metrics['upgrade_clicks']}
- Tab views: {metrics['tab_views']}

Infrastructure status:
- Stripe webhook: {webhook_status}
- Plus status: stored in Supabase subscriptions table, auto-unlocked via webhook

Do NOT suggest building the Stripe webhook — it is already built and deployed.
Write exactly ONE concrete, specific, actionable improvement for the engineer to do today.
One sentence. No preamble. No asterisks. No markdown."""

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 150,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"].strip()


def build_page_content(metrics: dict, issues: list, suggestion: str, webhook_live: bool) -> str:
    today_display = date.today().strftime("%B %d, %Y")

    # Form funnel bar chart
    form_total = metrics["form_started"] or 1
    form_bar = format_bar(metrics["form_started"], form_total)
    entry_bar = format_bar(metrics["entries"], form_total)
    upgrade_bar = format_bar(metrics["upgrade_clicks"], form_total)
    form_rate_str = f"{metrics['form_rate']}%" if metrics["form_started"] > 0 else "—"
    upgrade_rate_str = f"{round(metrics['upgrade_clicks'] / metrics['form_started'] * 100)}%" if metrics["form_started"] > 0 else "—"

    # Issues section
    if issues:
        issues_lines = []
        for i, issue in enumerate(issues, 1):
            issues_lines.append(f"{issue['severity']} #{i} · {issue['text']}<br>   Necessary? {issue['necessary']}")
        issues_text = "\n".join(issues_lines)
    else:
        issues_text = "✅ No critical issues detected today."

    webhook_indicator = "✅ live" if webhook_live else "🔴 not responding"

    return f"""💌 dear date analytics · last refreshed {today_display} · updates every morning at 9 AM ET
---
## ⚡ At a glance — last 7 days
Metric              \\| Value  \\| Signal<br>--------------------\\|--------\\|---------------------------<br>Avg daily users     \\| {metrics['dau']}    \\| today: {metrics['dau']}, 7d avg: {metrics['dau']}<br>Forms started       \\| {metrics['form_started']}     \\| <br>Entries logged      \\| {metrics['entries']}     \\| {'🚨 zero entries this week' if metrics['entries'] == 0 else '✅'}<br>Upgrade clicks      \\| {metrics['upgrade_clicks']}     \\| modal opened: {metrics['upgrade_modals']}
---
## 🔽 Form funnel
Step             \\| Count \\| Bar                  \\| Rate<br>-----------------\\|-------\\|----------------------\\|------<br>Form started     \\|     {metrics['form_started']} \\| {form_bar}  \\| 100%<br>Entry logged     \\|     {metrics['entries']} \\| {entry_bar}  \\| {form_rate_str}<br>Upgrade clicked  \\|     {metrics['upgrade_clicks']} \\| {upgrade_bar}  \\| {upgrade_rate_str}
---
## 📑 Where users go
{'No tab_viewed events recorded this week.' if metrics['tab_views'] == 0 else f"{metrics['tab_views']} tab views recorded this week."}
---
## ⚙️ Infrastructure
Stripe webhook (Supabase Edge Function): {webhook_indicator}
---
## 🚨 Issues to fix
{issues_text}
---
## 💡 Today's suggestion
✨ {suggestion}
---
→ 📋 Daily Analysis Log · 🤖 Automation Setup"""


def update_notion_page(content: str):
    """Overwrite the live analytics dashboard page content."""
    # First, get current page blocks to delete them
    r = requests.get(
        f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": "2022-06-28",
        },
        timeout=30,
    )
    r.raise_for_status()
    blocks = r.json().get("results", [])

    # Delete existing blocks
    for block in blocks:
        requests.delete(
            f"https://api.notion.com/v1/blocks/{block['id']}",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2022-06-28",
            },
            timeout=10,
        )

    # Write new content as a single paragraph block (Notion plain text)
    requests.patch(
        f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        json={
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": content[:2000]}}]
                    },
                }
            ]
        },
        timeout=30,
    )


def write_to_notion_db(metrics: dict, health: str, suggestion: str):
    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        json={
            "parent": {"database_id": NOTION_DATABASE_ID},
            "properties": {
                "Date":               {"title": [{"text": {"content": TODAY}}]},
                "Analysis Date":      {"date": {"start": TODAY}},
                "Health":             {"select": {"name": health}},
                "Daily Active Users": {"number": metrics["dau"]},
                "Entries Logged":     {"number": metrics["entries"]},
                "Upgrade Clicks":     {"number": metrics["upgrade_clicks"]},
                "Top Suggestion":     {"rich_text": [{"text": {"content": suggestion}}]},
                "Action Status":      {"select": {"name": "📋 Pending"}},
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    print(f"Notion DB row created: {r.json()['url']}")


def main():
    print(f"=== Dear Date Daily Analysis | {TODAY} ===")

    print("Checking Stripe webhook status...")
    webhook_live = check_webhook_live()
    print(f"  Webhook live: {webhook_live}")

    print("Fetching PostHog metrics...")
    metrics = {
        "dau":            get_metric_total("tab_viewed",           math="dau",   days=7),
        "form_started":   get_metric_total("entry_form_started",   math="total", days=7),
        "entries":        get_metric_total("entry_logged",         math="total", days=7),
        "upgrade_modals": get_metric_total("upgrade_modal_opened", math="total", days=7),
        "upgrade_clicks": get_metric_total("upgrade_clicked",      math="total", days=7),
        "tab_views":      get_metric_total("tab_viewed",           math="total", days=7),
        "form_rate":      get_form_completion_rate(),
    }
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    health = score_health(metrics["dau"], metrics["entries"], metrics["form_rate"], metrics["upgrade_clicks"])
    print(f"Health: {health}")

    issues = build_issues(metrics, webhook_live)
    print(f"Issues found: {len(issues)}")

    print("Generating suggestion via Claude...")
    suggestion = generate_suggestion(metrics, webhook_live)
    print(f"Suggestion: {suggestion}")

    print("Writing to Notion DB...")
    write_to_notion_db(metrics, health, suggestion)

    print("Updating analytics dashboard page...")
    content = build_page_content(metrics, issues, suggestion, webhook_live)
    update_notion_page(content)

    print("Done.")


if __name__ == "__main__":
    main()
