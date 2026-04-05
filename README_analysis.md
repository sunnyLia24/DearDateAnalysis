# Dear Date — Daily Analysis Setup

Automated daily analysis that pulls PostHog + Supabase metrics, computes health scores, and logs a structured entry to Notion.

## Setup Checklist

### 1. PostHog API Key

1. Go to [PostHog](https://us.posthog.com) → your project → **Settings** → **Project API Key**
2. Copy the **server-side** project API key (starts with `phx_` — **not** the `phc_` client key)
3. This key is used to read event data via the Trends API

### 2. Supabase Anon Key

1. Go to your [Supabase dashboard](https://supabase.com/dashboard) → project → **Settings** → **API**
2. Copy the `anon` / `public` key
3. The script queries the `entries` table (read-only)

### 3. Notion Integration

1. Go to [notion.com/my-integrations](https://www.notion.com/my-integrations)
2. Click **New integration**
   - Name: `Dear Date Analysis Bot`
   - Associated workspace: your workspace
   - Capabilities: **Read content**, **Insert content** (no update needed)
3. Copy the **Internal Integration Secret** (starts with `ntn_`)
4. Open your Notion database → click **⋯** (top right) → **Connections** → **Add connection** → select your integration
5. The database must have these properties (exact names):
   - `Date` (title)
   - `Health` (select) — options: `🟢 Healthy`, `🟡 Needs Attention`, `🔴 Critical`
   - `Daily Active Users` (number)
   - `Entries Logged` (number)
   - `Upgrade Clicks` (number)
   - `Top Suggestion` (rich text)
   - `Action Status` (select) — option: `📋 Pending`
   - `Instagram Notes` (rich text)
   - `Skip Reason` (rich text)
   - `Analysis Date` (date)

### 4. GitHub Secrets

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret Name        | Value                                      |
| ------------------ | ------------------------------------------ |
| `POSTHOG_API_KEY`  | PostHog server-side project API key        |
| `SUPABASE_ANON_KEY`| Supabase anon/public key                   |
| `NOTION_TOKEN`     | Notion internal integration secret         |

### 5. Schedule

The workflow runs automatically at **9:00 AM ET** (1:00 PM UTC) every day.

To run manually: go to **Actions** → **Daily Analysis** → **Run workflow**.

### 6. Stripe Webhook Flag

The script always suggests building the Stripe webhook until a file named `.stripe_webhook_done` exists in the repo root. Once you've built and deployed the webhook, create this file:

```bash
touch .stripe_webhook_done
git add .stripe_webhook_done && git commit -m "Mark Stripe webhook as done" && git push
```

## How It Works

1. **Dedup check** — queries Notion for today's `Analysis Date`; exits early if found
2. **PostHog** — fetches 14 days of event trends (7-day window + prior 7 for comparison)
3. **Supabase** — counts total entries and recent `body_feel` distribution
4. **Analysis** — computes DAU trends, form completion rate, health score
5. **Suggestions** — rule-based, ranked by priority (infrastructure > funnels > engagement > growth)
6. **Notion** — creates a page with properties + detailed body blocks
7. **Stdout** — prints a clean summary for GitHub Actions logs

## Health Scoring

| Status              | Condition                                                    |
| ------------------- | ------------------------------------------------------------ |
| 🟢 Healthy          | DAU ≥ 3 today AND entries logged > 0 this week              |
| 🔴 Critical         | DAU = 0 for 2+ consecutive days OR upgrade modal shown but 0 clicks all week |
| 🟡 Needs Attention  | Everything else                                              |
