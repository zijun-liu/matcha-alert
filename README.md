# Matcha Restock Alert

Watches every matcha product on the [Marukyu-Koyamaen international shop](https://www.marukyu-koyamaen.co.jp/english/shop/products/catalog/matcha)
and emails you the moment something goes from **sold out** back to **in stock**.

Marukyu-Koyamaen restocks matcha randomly — mostly during Japanese business
hours, but not always — and won't announce a schedule, so the only way to
catch a restock is to keep checking. This repo does that in the cloud, around
the clock; your computer can be off.

## How it works

```
cron-job.org  ──every 5 min──▶  GitHub Actions workflow  ──runs──▶  matcha_monitor.py
(free pinger)                   (matcha-check.yml)                  checks the shop,
                                                                    emails on restock,
                                                                    commits state.json
```

- `matcha_monitor.py` visits each product page and looks for the shop's own
  "currently out of stock and unavailable" message. No message = buyable.
- It remembers every product's last status in `state.json` (committed back to
  the repo after each run) and only emails when something **newly** restocks —
  no repeat spam. If the email fails to send, the alert is retried on the next run.
- To be polite to the shop, each 5-minute run only re-checks items that are
  currently **sold out**, with a full sweep once an hour to catch new
  sell-outs. Requests are spaced 2 seconds apart with a normal browser
  identity.
- It also scans the catalog for brand-new matcha products, auto-watches them,
  and emails you when they appear.
- Why the external pinger? GitHub's own cron is heavily throttled — in practice
  it fires every few *hours*, not minutes. The `*/30` schedule in the workflow
  is kept only as a backup; cron-job.org provides the real 5-minute cadence via
  the `workflow_dispatch` API.

## Setup

1. **Gmail App Password** — turn on [2-Step Verification](https://myaccount.google.com/security),
   then create an [app password](https://myaccount.google.com/apppasswords).
2. **Repo secrets** — in *Settings → Secrets and variables → Actions*, add:
   - `MATCHA_SMTP_USER` — your Gmail address
   - `MATCHA_SMTP_PASS` — the 16-character app password
   - `MATCHA_MAIL_TO` — where alerts go (can be a comma-separated list)
3. **Pinger** — create a [fine-grained token](https://github.com/settings/personal-access-tokens/new)
   scoped to this repo with **Actions: Read and write**, then on
   [cron-job.org](https://cron-job.org) create a job:
   - URL: `https://api.github.com/repos/<you>/matcha-alert/actions/workflows/matcha-check.yml/dispatches`
   - Schedule: every 5 minutes · Method: `POST` · Body: `{"ref":"main"}`
   - Headers: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`,
     `Content-Type: application/json`
   - A test run returning **204** means it works.
4. **Verify** — the *Actions* tab should show a run every ~5 minutes. Use
   *Run workflow → test-email* to confirm email delivery.

## Watch list

`products.json` holds the ~50 matcha products being monitored. Edit it to add
or remove items, or set `"watch": false` to pause one. Auto-discovered new
products live in `state.json` and can be paused the same way.

## Files

| File | What it is |
|------|-----------|
| `matcha_monitor.py` | The checker/emailer. Also runs locally: `python3 matcha_monitor.py --force` |
| `products.json` | Watch list. Editable. |
| `state.json` | Last known stock status; auto-committed by the workflow. |
| `.github/workflows/matcha-check.yml` | The Actions workflow (dispatch-driven + 30-min backup cron). |

## Notes

- The shop discourages aggressive scraping and its bot protection may block
  requests. The script detects block/challenge pages and never mistakes them
  for "in stock"; blocked checks are simply retried next run.
- Matcha is limited to 5 items per order, and the shop declines orders it
  believes are for resale.
