# Tokie manual-entry templates

Web-only AI tools emit no local signal Tokie can parse — there is no JSONL log,
no local cache, no usage endpoint. The only honest answer is: the user logs
what they used, by hand, and Tokie surfaces it with `Confidence.INFERRED`
(visually distinct from exact/API-sourced data).

This folder ships starter templates so you don't start from a blank file.

## Quickstart

```bash
# copy the web-tools template into the default manual drop folder
# Linux / macOS
mkdir -p "$(tokie paths data)/manual"
cp "$(python -c 'from tokie_cli.collectors import manual_templates; import importlib.resources; print(importlib.resources.files(manual_templates))')/web_tools.csv" \
   "$(tokie paths data)/manual/my_usage.csv"

# Windows PowerShell
$dataDir = tokie paths data
New-Item -Path "$dataDir\manual" -ItemType Directory -Force | Out-Null
Copy-Item "<path-to-templates>\web_tools.csv" "$dataDir\manual\my_usage.csv"

# edit the file to reflect your actual usage, then:
tokie scan --collector manual
```

(The `tokie paths` helper ships in the Day 3 CLI. Until then, use
`$TOKIE_DATA_HOME/manual/` or set `TOKIE_MANUAL_LOG` to any file/dir.)

## CSV header contract

| Column         | Required | Notes                                                         |
|----------------|----------|---------------------------------------------------------------|
| `occurred_at`  | yes      | ISO-8601 with timezone (e.g. `2026-04-20T10:00:00Z`)           |
| `provider`     | yes      | Vendor slug: `manus`, `wisperflow`, `google`, `openai`, etc.   |
| `product`      | yes      | Product slug: `manus-web`, `gemini-web`, `chatgpt-web`, etc.   |
| `model`        | yes      | Model string the vendor advertises                            |
| `account_id`   | no       | Defaults to `"default"`                                       |
| `input_tokens` | no       | Defaults to `0` — most web tools don't expose this            |
| `output_tokens`| no       | Defaults to `0`; overridden by `messages` if that is present  |
| `cost_usd`     | no       | Dollars spent on this session, if you know                    |
| `notes`        | no       | Free-form; stored in `UsageEvent.source`                      |
| `messages`     | no       | If set, overrides `output_tokens` (for tools that count turns) |

Naive timestamps (no timezone) are rejected — always include `Z` or an offset.

## YAML alternative

Same fields, different shape — either a top-level list or `entries:` block:

```yaml
entries:
  - occurred_at: 2026-04-20T10:00:00Z
    provider: manus
    product: manus-web
    model: manus-v2
    cost_usd: 0.50
    notes: autonomous research task
```

## Tracked tools (covered in `web_tools.csv`)

Manus · WisperFlow · Gemini Advanced · Google AI Studio · v0 by Vercel ·
bolt.new · Lovable · Devin · Mistral Le Chat · DeepSeek web · xAI Grok ·
Perplexity Pro · ChatGPT web · Claude.ai web.

Add your own rows — any provider/product pair is valid. Tokie will render
whatever you log.
