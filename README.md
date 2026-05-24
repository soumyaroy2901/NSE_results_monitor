# NSE Results Monitor

This folder contains the NSE result-announcement monitor.

- `nse_results_telegram_bot.py`: Python poller that sends new result PDFs to Telegram.
- `n8n/`: importable n8n workflow artifacts kept for reference.

## Run locally

Install dependencies:

```bash
pip install -r ../requirements.txt
```

Set environment variables in `.env`:

```bash
TELEGRAM_BOT_TOKEN=your_full_bot_token
TELEGRAM_CHAT_ID=1578138331
NSE_RESULT_POLL_SECONDS=60
NSE_RESULT_STATE_PATH=.state/nse_results_state.json
NSE_RESULT_ONE_PER_SYMBOL_PER_DAY=true
NSE_RESULT_ENABLE_AI_ANALYSIS=true
NSE_RESULT_MAX_REPORTS_PER_RUN=0
NSE_RESULT_EXIT_AFTER_MAX_REPORTS=false
NSE_RESULT_SEND_SOURCE_PDF=false
NSE_RESULT_AI_COOLDOWN_SECONDS=900
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.0-flash
GEMINI_FALLBACK_MODELS=gemini-1.5-flash
```

Run once without Telegram:

```bash
python3 nse_results_telegram_bot.py --once --dry-run
```

Run continuously:

```bash
python3 nse_results_telegram_bot.py
```

## Render

Use a Background Worker, not a Web Service.

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
python3 nse_results_monitor/nse_results_telegram_bot.py
```

Environment variables:

```bash
TELEGRAM_BOT_TOKEN=your_full_bot_token
TELEGRAM_CHAT_ID=1578138331
NSE_RESULT_POLL_SECONDS=60
NSE_RESULT_STATE_PATH=nse_results_monitor/.state/nse_results_state.json
NSE_RESULT_ONE_PER_SYMBOL_PER_DAY=true
NSE_RESULT_ENABLE_AI_ANALYSIS=true
NSE_RESULT_MAX_REPORTS_PER_RUN=0
NSE_RESULT_EXIT_AFTER_MAX_REPORTS=false
NSE_RESULT_SEND_SOURCE_PDF=false
NSE_RESULT_AI_COOLDOWN_SECONDS=900
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.0-flash
GEMINI_FALLBACK_MODELS=gemini-1.5-flash
```

If Render is blocked by NSE, Python will show an `NSE blocked ... HTTP 403` error. In that case the issue is the Render IP/routing, not the script. Move the bot to a VPS/IP that NSE allows, or set:

```bash
NSE_RESULT_PROXY_URL=http://user:pass@host:port
```

## Behavior

- Polls once per minute by default.
- Warms up an NSE session before calling API endpoints.
- Uses the board-meeting calendar when available.
- Falls back to the global `Outcome of Board Meeting` announcements endpoint if the board calendar is blocked.
- Requires the announcement text to look like financial results, even when the symbol is in the board-meeting calendar.
- Sends only the first matching result announcement per symbol per India date by default. Set `NSE_RESULT_ONE_PER_SYMBOL_PER_DAY=false` if you want corrected/supplementary filings too.
- If `NSE_RESULT_ENABLE_AI_ANALYSIS=true` and `GEMINI_API_KEY` is set, uploads the result PDF to Gemini, fetches NSE quote/index context, and sends an AI report to Telegram before sending the source PDF.
- The AI report includes performance bucket, directional bias, confidence, key financials, positives, concerns, market sentiment, and quote-derived technical view.
- `NSE_RESULT_MAX_REPORTS_PER_RUN=5` is useful for testing; `0` means no per-cycle limit. Set `NSE_RESULT_EXIT_AFTER_MAX_REPORTS=true` to stop the process after that test batch.
- Telegram output is two text messages per stock by default: a highlighted directional-bias alert, then the detailed AI report with the NSE PDF link. Set `NSE_RESULT_SEND_SOURCE_PDF=true` only if you also want the PDF uploaded as a third message.
- On the first Gemini `429`, AI analysis is paused for `NSE_RESULT_AI_COOLDOWN_SECONDS` seconds. Telegram messages continue with an `UNCLEAR` bias and the NSE PDF link.
- Dedupes by announcement ID in `NSE_RESULT_STATE_PATH`.
- On first run, it marks already-published announcements as seen to avoid flooding Telegram. Set `NSE_RESULT_SEND_EXISTING_ON_FIRST_RUN=true` to backfill today.

## AI analysis limits

The report is an automated research summary, not financial advice.

The bot does not currently fetch paid analyst consensus. If consensus expectations are not present in the PDF or market context, Gemini is instructed not to invent them. The expectation bucket is therefore based on the reported numbers, commentary, price reaction, broad market context, and technical snapshot.

If Gemini returns `503 UNAVAILABLE`, the configured model is overloaded. The bot now tries `GEMINI_MODEL` first and then `GEMINI_FALLBACK_MODELS`. Keep the primary model lightweight for live monitoring:

```bash
GEMINI_MODEL=gemini-2.0-flash
GEMINI_FALLBACK_MODELS=gemini-1.5-flash
```
