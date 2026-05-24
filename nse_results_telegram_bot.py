#!/usr/bin/env python3
"""
Poll NSE result announcements and send new PDFs to Telegram.

Environment:
  TELEGRAM_BOT_TOKEN or NSE_RESULT_TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID or NSE_RESULT_TELEGRAM_CHAT_ID

Optional:
  NSE_RESULT_POLL_SECONDS=60
  NSE_RESULT_STATE_PATH=nse_results_monitor/.state/nse_results_state.json
  NSE_RESULT_SEND_EXISTING_ON_FIRST_RUN=false
  NSE_RESULT_REQUIRE_CALENDAR_MATCH=false
  NSE_RESULT_INCLUDE_OUTSIDE_CALENDAR=true
  NSE_RESULT_ONE_PER_SYMBOL_PER_DAY=true
  NSE_RESULT_ENABLE_AI_ANALYSIS=true
  GEMINI_API_KEY=your_gemini_api_key
  GEMINI_MODEL=gemini-2.0-flash
  GEMINI_FALLBACK_MODELS=gemini-1.5-flash
  NSE_RESULT_MAX_REPORTS_PER_RUN=0
  NSE_RESULT_EXIT_AFTER_MAX_REPORTS=false
  NSE_RESULT_SEND_SOURCE_PDF=false
  NSE_RESULT_AI_COOLDOWN_SECONDS=900
  NSE_RESULT_PROXY_URL=http://user:pass@host:port
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from zoneinfo import ZoneInfo


NSE_BASE = "https://www.nseindia.com"
NSE_ARCHIVE_BASE = "https://nsearchives.nseindia.com"
GEMINI_BASE = "https://generativelanguage.googleapis.com"
IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = BASE_DIR / ".state" / "nse_results_state.json"
TELEGRAM_MESSAGE_LIMIT = 4096

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

FINANCIAL_RESULT_RE = re.compile(
    r"financial\s+results?|audited|unaudited|standalone|consolidated|"
    r"quarter|yearly|year ended|period ended|31 march|31 december|"
    r"30 june|30 september",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Today:
    nse_date: str
    iso_date: str


@dataclass(frozen=True)
class Announcement:
    id: str
    symbol: str
    company_name: str
    published_at: str
    exchange_dissemination_time: str
    pdf_url: str
    sort_date: str

    @property
    def caption(self) -> str:
        lines = [
            f"NSE result update: {self.symbol}",
            self.company_name,
            f"Published: {self.published_at or 'n/a'}",
            f"Exchange dissemination: {self.exchange_dissemination_time or 'n/a'}",
            self.pdf_url,
        ]
        return "\n".join(line for line in lines if line)[:1000]


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    company_name: str
    last_price: float | None
    change: float | None
    percent_change: float | None
    previous_close: float | None
    open_price: float | None
    day_high: float | None
    day_low: float | None
    vwap: float | None
    volume: float | None
    value: float | None
    week_52_high: float | None
    week_52_low: float | None
    market_status: str
    nifty_percent_change: float | None
    nifty_last: float | None
    technical_summary: str

    def as_prompt_context(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "company_name": self.company_name,
            "last_price": self.last_price,
            "change": self.change,
            "percent_change": self.percent_change,
            "previous_close": self.previous_close,
            "open": self.open_price,
            "day_high": self.day_high,
            "day_low": self.day_low,
            "vwap": self.vwap,
            "volume": self.volume,
            "value": self.value,
            "week_52_high": self.week_52_high,
            "week_52_low": self.week_52_low,
            "market_status": self.market_status,
            "nifty_percent_change": self.nifty_percent_change,
            "nifty_last": self.nifty_last,
            "technical_summary": self.technical_summary,
        }


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_today() -> Today:
    now = datetime.now(IST)
    return Today(nse_date=now.strftime("%d-%m-%Y"), iso_date=now.strftime("%Y-%m-%d"))


def first_number(*values: Any) -> float | None:
    for value in values:
        if value in (None, "", "-"):
            continue
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


def compact_json(value: Any, limit: int = 12000) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...truncated..."


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        logging.warning("Ignoring corrupt state file: %s", path)
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp_path.replace(path)


class NSEAccessError(RuntimeError):
    pass


class StopAfterMaxReports(RuntimeError):
    pass


def redact_url(url: str) -> str:
    return re.sub(r"([?&]key=)[^&]+", r"\1<redacted>", url)


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        return max(0.0, parsed.timestamp() - time.time())
    except Exception:
        return None


class GeminiHTTPError(RuntimeError):
    def __init__(self, response: requests.Response, operation: str) -> None:
        self.status_code = response.status_code
        self.operation = operation
        self.body = response.text[:2000]
        super().__init__(
            f"Gemini {operation} failed with HTTP {response.status_code}: {self.body[:500]}"
        )


def parse_csv_env(name: str, default: list[str] | None = None) -> list[str]:
    value = os.getenv(name)
    if value is None:
        return list(default or [])
    return [part.strip() for part in value.split(",") if part.strip()]


class NSEClient:
    def __init__(self, proxy_url: str | None = None) -> None:
        self.session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )
        if proxy_url:
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})

    def warm_up(self) -> None:
        warmup_urls = [
            f"{NSE_BASE}/",
            f"{NSE_BASE}/companies-listing/corporate-filings-announcements",
            f"{NSE_BASE}/companies-listing/corporate-filings-application?id=boardMeetings",
        ]
        for url in warmup_urls:
            try:
                response = self.session.get(
                    url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                    },
                    timeout=20,
                )
                logging.debug("Warm-up %s -> %s cookies=%s", url, response.status_code, len(self.session.cookies))
            except requests.RequestException as exc:
                logging.debug("Warm-up failed for %s: %s", url, exc)

    def get_json(self, url: str, referer: str) -> list[dict[str, Any]]:
        data = self.get_raw_json(url, referer)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        return []

    def get_raw_json(self, url: str, referer: str) -> Any:
        headers = {
            "Accept": "application/json,text/plain,*/*",
            "Referer": referer,
            "Origin": NSE_BASE,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        response = self.session.get(url, headers=headers, timeout=30)
        if response.status_code in {401, 403}:
            self.warm_up()
            response = self.session.get(url, headers=headers, timeout=30)

        if response.status_code in {401, 403} or "Access Denied" in response.text:
            raise NSEAccessError(f"NSE blocked {url} with HTTP {response.status_code}")

        response.raise_for_status()
        return response.json()

    def board_meetings(self, today: Today) -> list[dict[str, Any]]:
        url = (
            f"{NSE_BASE}/api/corporate-board-meetings"
            f"?index=equities&from_date={today.nse_date}&to_date={today.nse_date}"
        )
        return self.get_json(
            url,
            f"{NSE_BASE}/companies-listing/corporate-filings-application?id=boardMeetings",
        )

    def outcome_announcements(self, today: Today) -> list[dict[str, Any]]:
        subject = quote("Outcome of Board Meeting")
        url = (
            f"{NSE_BASE}/api/corporate-announcements"
            f"?index=equities&from_date={today.nse_date}&to_date={today.nse_date}"
            f"&reqXbrl=false&subject={subject}"
        )
        return self.get_json(url, f"{NSE_BASE}/companies-listing/corporate-filings-announcements")

    def quote_equity(self, symbol: str) -> dict[str, Any]:
        url = f"{NSE_BASE}/api/quote-equity?symbol={quote(symbol.upper())}"
        data = self.get_raw_json(url, f"{NSE_BASE}/get-quotes/equity?symbol={quote(symbol.upper())}")
        return data if isinstance(data, dict) else {}

    def all_indices(self) -> list[dict[str, Any]]:
        data = self.get_raw_json(
            f"{NSE_BASE}/api/allIndices",
            f"{NSE_BASE}/market-data/live-market-indices",
        )
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        return data if isinstance(data, list) else []

    def market_snapshot(self, symbol: str) -> MarketSnapshot:
        quote_data = self.quote_equity(symbol)
        price = quote_data.get("priceInfo") or {}
        metadata = quote_data.get("metadata") or {}
        info = quote_data.get("info") or {}
        security_info = quote_data.get("securityInfo") or {}
        industry_info = quote_data.get("industryInfo") or {}
        intraday = price.get("intraDayHighLow") or {}
        week_52 = price.get("weekHighLow") or {}
        pre_open = quote_data.get("preOpenMarket") or {}

        nifty_last = None
        nifty_percent_change = None
        try:
            for row in self.all_indices():
                index_name = str(row.get("index") or row.get("indexSymbol") or "").upper()
                if index_name == "NIFTY 50":
                    nifty_last = first_number(row.get("last"), row.get("lastPrice"))
                    nifty_percent_change = first_number(row.get("percentChange"), row.get("percChange"))
                    break
        except Exception as exc:
            logging.debug("Unable to fetch NIFTY context: %s", exc)

        last_price = first_number(price.get("lastPrice"), metadata.get("lastPrice"))
        open_price = first_number(price.get("open"), intraday.get("open"), pre_open.get("IEP"))
        day_high = first_number(intraday.get("max"), price.get("intraDayHighLow", {}).get("max"))
        day_low = first_number(intraday.get("min"), price.get("intraDayHighLow", {}).get("min"))
        vwap = first_number(price.get("vwap"))
        previous_close = first_number(price.get("previousClose"), metadata.get("previousClose"))
        percent_change = first_number(price.get("pChange"), metadata.get("pChange"))
        change = first_number(price.get("change"))
        week_52_high = first_number(week_52.get("max"))
        week_52_low = first_number(week_52.get("min"))
        volume = first_number(price.get("totalTradedVolume"), metadata.get("totalTradedVolume"))
        value = first_number(price.get("totalTradedValue"), metadata.get("totalTradedValue"))

        technical_bits: list[str] = []
        if last_price is not None and previous_close:
            technical_bits.append(f"price is {((last_price - previous_close) / previous_close) * 100:.2f}% vs previous close")
        if last_price is not None and open_price:
            technical_bits.append(f"{((last_price - open_price) / open_price) * 100:.2f}% vs open")
        if last_price is not None and vwap:
            technical_bits.append(f"{((last_price - vwap) / vwap) * 100:.2f}% vs VWAP")
        if last_price is not None and day_high is not None and day_low is not None and day_high != day_low:
            position = ((last_price - day_low) / (day_high - day_low)) * 100
            technical_bits.append(f"{position:.0f}% position in intraday range")
        if last_price is not None and week_52_high:
            technical_bits.append(f"{((last_price - week_52_high) / week_52_high) * 100:.2f}% below 52-week high")
        if nifty_percent_change is not None:
            technical_bits.append(f"NIFTY 50 is {nifty_percent_change:.2f}%")

        company_name = (
            info.get("companyName")
            or metadata.get("companyName")
            or security_info.get("companyName")
            or ""
        )
        sector = industry_info.get("macro") or industry_info.get("sector") or ""
        if sector and company_name:
            company_name = f"{company_name} ({sector})"

        return MarketSnapshot(
            symbol=symbol.upper(),
            company_name=str(company_name),
            last_price=last_price,
            change=change,
            percent_change=percent_change,
            previous_close=previous_close,
            open_price=open_price,
            day_high=day_high,
            day_low=day_low,
            vwap=vwap,
            volume=volume,
            value=value,
            week_52_high=week_52_high,
            week_52_low=week_52_low,
            market_status=str(metadata.get("status") or quote_data.get("marketStatus") or ""),
            nifty_percent_change=nifty_percent_change,
            nifty_last=nifty_last,
            technical_summary="; ".join(technical_bits),
        )

    def download_file(self, url: str) -> tuple[bytes, str]:
        response = self.session.get(
            url,
            headers={
                "Accept": "application/pdf,application/octet-stream,*/*",
                "Referer": f"{NSE_BASE}/",
            },
            timeout=60,
        )
        if response.status_code in {401, 403} and url.startswith(NSE_ARCHIVE_BASE):
            self.warm_up()
            response = self.session.get(url, headers={"Referer": f"{NSE_BASE}/"}, timeout=60)
        response.raise_for_status()

        filename = url.rsplit("/", 1)[-1] or "nse-result.pdf"
        filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
        return response.content, filename


class TelegramClient:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"

    def send_document(self, filename: str, content: bytes, caption: str) -> None:
        response = requests.post(
            f"{self.base_url}/sendDocument",
            data={"chat_id": self.chat_id, "caption": caption},
            files={"document": (filename, content, "application/pdf")},
            timeout=90,
        )
        if response.status_code == 413:
            raise RuntimeError("Telegram rejected the document because it is too large")
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram sendDocument failed: {result}")

    def send_message(self, text: str, parse_mode: str | None = None) -> None:
        data = {"chat_id": self.chat_id, "text": text[:TELEGRAM_MESSAGE_LIMIT], "disable_web_page_preview": "true"}
        if parse_mode:
            data["parse_mode"] = parse_mode
        response = requests.post(
            f"{self.base_url}/sendMessage",
            data=data,
            timeout=30,
        )
        response.raise_for_status()


class GeminiClient:
    def __init__(self, api_key: str, model: str, fallback_models: list[str] | None = None) -> None:
        self.api_key = api_key
        self.model = model
        self.models = []
        for candidate in [model, *(fallback_models or [])]:
            if candidate and candidate not in self.models:
                self.models.append(candidate)
        self.session = requests.Session()

    def request_with_retry(
        self,
        method: str,
        url: str,
        operation: str,
        *,
        max_attempts: int = 4,
        **kwargs: Any,
    ) -> requests.Response:
        last_response: requests.Response | None = None
        for attempt in range(1, max_attempts + 1):
            logging.debug("Gemini %s attempt %s/%s -> %s", operation, attempt, max_attempts, redact_url(url))
            response = self.session.request(method, url, **kwargs)
            last_response = response
            if response.status_code < 400:
                return response

            retryable = response.status_code in {429, 500, 502, 503, 504}
            body_preview = response.text[:500].replace("\n", " ")
            logging.warning(
                "Gemini %s HTTP %s attempt %s/%s: %s",
                operation,
                response.status_code,
                attempt,
                max_attempts,
                body_preview,
            )
            if not retryable or attempt == max_attempts:
                raise GeminiHTTPError(response, operation)

            delay = retry_after_seconds(response.headers.get("Retry-After"))
            if delay is None:
                delay = min(30.0, 2.0 ** attempt)
            time.sleep(delay)

        raise GeminiHTTPError(last_response, operation)  # type: ignore[arg-type]

    def upload_pdf(self, filename: str, content: bytes) -> tuple[str, str]:
        start_url = f"{GEMINI_BASE}/upload/v1beta/files?key={self.api_key}"
        metadata = {"file": {"display_name": filename}}
        start_response = self.request_with_retry(
            "POST",
            start_url,
            "upload-start",
            headers={
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(len(content)),
                "X-Goog-Upload-Header-Content-Type": "application/pdf",
                "Content-Type": "application/json",
            },
            data=json.dumps(metadata),
            timeout=30,
        )
        upload_url = start_response.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise RuntimeError("Gemini upload did not return an upload URL")

        upload_response = self.request_with_retry(
            "POST",
            upload_url,
            "upload-finalize",
            headers={
                "Content-Length": str(len(content)),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
                "Content-Type": "application/pdf",
            },
            data=content,
            timeout=120,
        )
        file_info = upload_response.json().get("file") or upload_response.json()
        file_name = file_info.get("name")
        file_uri = file_info.get("uri")
        mime_type = file_info.get("mimeType") or file_info.get("mime_type") or "application/pdf"
        if not file_uri:
            raise RuntimeError(f"Gemini upload response did not include a file URI: {file_info}")
        if file_name:
            self.wait_for_file(file_name)
        return file_uri, mime_type

    def wait_for_file(self, file_name: str, timeout_seconds: int = 45) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            response = self.request_with_retry(
                "GET",
                f"{GEMINI_BASE}/v1beta/{file_name}?key={self.api_key}",
                "file-status",
                max_attempts=3,
                timeout=30,
            )
            file_info = response.json()
            state = str(file_info.get("state") or "").upper()
            if state in {"", "ACTIVE"}:
                return
            if state == "FAILED":
                raise RuntimeError(f"Gemini file processing failed for {file_name}")
            time.sleep(2)
        raise RuntimeError(f"Timed out waiting for Gemini file processing: {file_name}")

    def analyze(
        self,
        announcement: Announcement,
        pdf_filename: str,
        pdf_content: bytes,
        market_snapshot: MarketSnapshot | None,
    ) -> dict[str, Any]:
        file_uri, mime_type = self.upload_pdf(pdf_filename, pdf_content)
        market_context = market_snapshot.as_prompt_context() if market_snapshot else {"error": "market data unavailable"}
        prompt = build_gemini_prompt(announcement, market_context)
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {"file_data": {"mime_type": mime_type, "file_uri": file_uri}},
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "response_mime_type": "application/json",
            },
        }
        last_error: Exception | None = None
        for model in self.models:
            try:
                logging.info("Gemini analysis using model %s for %s", model, announcement.symbol)
                response = self.request_with_retry(
                    "POST",
                    f"{GEMINI_BASE}/v1beta/models/{model}:generateContent?key={self.api_key}",
                    f"generate-content:{model}",
                    max_attempts=2,
                    json=payload,
                    timeout=120,
                )
                break
            except GeminiHTTPError as exc:
                last_error = exc
                if exc.status_code in {429, 500, 502, 503, 504}:
                    logging.warning("Gemini model %s unavailable for %s; trying fallback", model, announcement.symbol)
                    continue
                raise
        else:
            raise last_error or RuntimeError("Gemini analysis failed for all configured models")
        data = response.json()
        text = ""
        for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
            text += part.get("text", "")
        return parse_gemini_json(text)


def build_gemini_prompt(announcement: Announcement, market_context: dict[str, Any]) -> str:
    return f"""
You are an Indian equity results analyst. Analyze the attached NSE result PDF and the market context below.

Important constraints:
- This is not financial advice. Provide a probabilistic short-term view.
- If analyst consensus estimates are not available in the PDF/context, do not pretend you have them.
- Use the PDF as the primary source for financial performance.
- Use the market context for price action, broad market sentiment, and quote-derived technical view.
- Be concise and specific. Do not output markdown. Output only valid JSON.

Announcement:
{compact_json(announcement.__dict__)}

Market and technical context:
{compact_json(market_context)}

Return JSON with exactly these keys:
{{
  "performance_bucket": "BELOW_EXPECTATION | JUST_MET_EXPECTATION | PERFORMED_WELL | LOT_ABOVE_EXPECTATION | UNCLEAR",
  "directional_bias": "UP | DOWN | SIDEWAYS | UNCLEAR",
  "confidence": 0,
  "time_horizon": "intraday to 3 sessions",
  "one_line_view": "",
  "result_summary": "",
  "key_financials": ["", "", ""],
  "positives": ["", ""],
  "negatives": ["", ""],
  "market_sentiment": "",
  "technical_view": "",
  "guidance": "",
  "risk_notes": ["", ""]
}}
"""


def parse_gemini_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned non-JSON output: {cleaned[:1000]}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Gemini returned JSON but not an object: {value}")
    return value


def html_lines(items: list[Any], limit: int = 3) -> str:
    clean = [str(item).strip() for item in items if str(item).strip()]
    if not clean:
        return "n/a"
    return "\n".join(f"- {html.escape(item)}" for item in clean[:limit])


def bias_label(raw_bias: Any) -> tuple[str, str]:
    bias = str(raw_bias or "UNCLEAR").strip().upper()
    if bias == "UP":
        return "🟢🚀", "UP / BULLISH"
    if bias == "DOWN":
        return "🔴⚠️", "DOWN / BEARISH"
    if bias == "SIDEWAYS":
        return "🟡↔️", "SIDEWAYS"
    return "⚪❔", "UNCLEAR"


def format_directional_bias_alert(announcement: Announcement, analysis: dict[str, Any]) -> str:
    icon, label = bias_label(analysis.get("directional_bias"))
    confidence = analysis.get("confidence", "n/a")
    horizon = str(analysis.get("time_horizon") or "intraday to 3 sessions")
    return "\n".join(
        [
            f"🚨 <b>{html.escape(announcement.symbol)} RESULT ALERT</b>",
            "",
            f"{icon} <b>DIRECTIONAL BIAS: {html.escape(label)}</b>",
            f"Confidence: <b>{html.escape(str(confidence))}%</b>",
            f"Horizon: {html.escape(horizon)}",
        ]
    )[:TELEGRAM_MESSAGE_LIMIT]


def format_ai_detail_report(announcement: Announcement, analysis: dict[str, Any], snapshot: MarketSnapshot | None) -> str:
    bucket = str(analysis.get("performance_bucket") or "UNCLEAR").replace("_", " ")
    bias = str(analysis.get("directional_bias") or "UNCLEAR").upper()
    confidence = analysis.get("confidence", "n/a")
    price_line = "Market data: n/a"
    if snapshot:
        price_bits = []
        if snapshot.last_price is not None:
            price_bits.append(f"LTP {snapshot.last_price:g}")
        if snapshot.percent_change is not None:
            price_bits.append(f"{snapshot.percent_change:+.2f}%")
        if snapshot.nifty_percent_change is not None:
            price_bits.append(f"NIFTY {snapshot.nifty_percent_change:+.2f}%")
        price_line = " | ".join(price_bits) or price_line

    parts = [
        f"<b>{html.escape(announcement.symbol)} AI Analysis Report</b>",
        html.escape(announcement.company_name),
        "",
        f"<b>Performance:</b> {html.escape(bucket)}",
        f"<b>Directional bias:</b> {html.escape(bias)} ({html.escape(str(confidence))}% confidence)",
        f"<b>Horizon:</b> {html.escape(str(analysis.get('time_horizon') or 'intraday to 3 sessions'))}",
        "",
        f"<b>View:</b> {html.escape(str(analysis.get('one_line_view') or 'n/a'))}",
        f"<b>Result:</b> {html.escape(str(analysis.get('result_summary') or 'n/a'))}",
        f"<b>Market:</b> {html.escape(str(analysis.get('market_sentiment') or price_line))}",
        f"<b>Technical:</b> {html.escape(str(analysis.get('technical_view') or (snapshot.technical_summary if snapshot else 'n/a')))}",
        "",
        "<b>Key financials</b>",
        html_lines(analysis.get("key_financials") or []),
        "",
        "<b>Positives</b>",
        html_lines(analysis.get("positives") or [], limit=2),
        "",
        "<b>Concerns</b>",
        html_lines(analysis.get("negatives") or [], limit=2),
        "",
        f"<b>Guidance:</b> {html.escape(str(analysis.get('guidance') or 'n/a'))}",
        "",
        "<b>Risks</b>",
        html_lines(analysis.get("risk_notes") or [], limit=2),
        "",
        f"<b>NSE PDF:</b> {html.escape(announcement.pdf_url)}",
        "",
        "<i>Automated analysis. Not financial advice.</i>",
    ]
    message = "\n".join(parts)
    if len(message) > TELEGRAM_MESSAGE_LIMIT:
        message = message[: TELEGRAM_MESSAGE_LIMIT - 100] + "\n\n<i>Truncated. Read PDF for details.</i>"
    return message


def format_ai_unavailable_detail(announcement: Announcement, reason: str | None = None) -> str:
    reason_line = f"Status: {html.escape(reason[:700])}" if reason else "Status: Analysis unavailable at this time for this filing. Paste below link in Gemini and get response faster"
    return "\n".join(
        [
            f"<b>{html.escape(announcement.symbol)} AI Analysis Report</b>",
            html.escape(announcement.company_name),
            "",
            reason_line,
            "",
            f"<b>NSE PDF:</b> {html.escape(announcement.pdf_url)}",
            "",
            "<i>Automated message. Not financial advice.</i>",
        ]
    )[:TELEGRAM_MESSAGE_LIMIT]


def build_watchlist(rows: list[dict[str, Any]]) -> set[str]:
    symbols: set[str] = set()
    for row in rows:
        symbol = str(row.get("bm_symbol") or "").strip().upper()
        if not symbol:
            continue
        text = f"{row.get('bm_purpose') or ''} {row.get('bm_desc') or ''}"
        if FINANCIAL_RESULT_RE.search(text):
            symbols.add(symbol)
    return symbols


def filter_announcements(
    rows: list[dict[str, Any]],
    today: Today,
    watch_symbols: set[str],
    require_calendar_match: bool,
    include_outside_calendar: bool,
) -> list[Announcement]:
    results: list[Announcement] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue

        sort_date = str(row.get("sort_date") or "")
        an_dt = str(row.get("an_dt") or "")
        is_today = sort_date.startswith(today.iso_date) or today.nse_date in an_dt
        if not is_today:
            continue

        pdf_url = str(row.get("attchmntFile") or "").strip()
        if not pdf_url or pdf_url == "-" or not pdf_url.startswith(("http://", "https://")):
            continue

        text = f"{row.get('desc') or ''} {row.get('attchmntText') or ''}"
        calendar_match = symbol in watch_symbols
        financial_text = bool(FINANCIAL_RESULT_RE.search(text))
        if not financial_text:
            continue
        if require_calendar_match and not calendar_match:
            continue
        if not calendar_match and not include_outside_calendar:
            continue

        unique_id = f"{symbol}|{row.get('seq_id') or row.get('dt') or pdf_url}"
        results.append(
            Announcement(
                id=unique_id,
                symbol=symbol,
                company_name=str(row.get("sm_name") or ""),
                published_at=an_dt or sort_date,
                exchange_dissemination_time=str(row.get("exchdisstime") or ""),
                pdf_url=pdf_url,
                sort_date=sort_date,
            )
        )
    return sorted(results, key=lambda item: item.sort_date)


def send_fallback_message(telegram: TelegramClient, item: Announcement) -> None:
    message_text = (
        f"{item.company_name} RESULTS out\n"
        f"Results link -> {item.pdf_url}\n\n"
        "[Check charts now and scan pdf with AI]"
    )
    telegram.send_message(message_text)


def poll_once(
    args: argparse.Namespace,
    nse: NSEClient,
    telegram: TelegramClient | None,
    gemini: GeminiClient | None = None,
) -> int:
    today = get_today()
    state_path = Path(args.state_path)
    state = load_state(state_path)

    sent_by_date = state.setdefault("sent_by_date", {})
    if sent_by_date.get("date") != today.iso_date:
        state["sent_by_date"] = {"date": today.iso_date, "sent": {}}
        sent_by_date = state["sent_by_date"]

    sent: dict[str, str] = sent_by_date.setdefault("sent", {})
    sent_symbols = set(sent_by_date.setdefault("sent_symbols", []))
    sent_symbols.update(key.split("|", 1)[0] for key in sent if "|" in key)
    sent_by_date["sent_symbols"] = sorted(sent_symbols)
    save_state(state_path, state)

    ai_cooldown_until = parse_iso_datetime(state.get("ai_cooldown_until"))
    ai_cooldown_active = bool(ai_cooldown_until and ai_cooldown_until > datetime.now(IST))
    if ai_cooldown_active:
        logging.info("AI analysis cooldown active until %s", ai_cooldown_until.isoformat())

    nse.warm_up()

    try:
        board_rows = nse.board_meetings(today)
        watch_symbols = build_watchlist(board_rows)
        state["last_board_meeting_error"] = ""
    except (NSEAccessError, requests.RequestException) as exc:
        watch_symbols = set()
        state["last_board_meeting_error"] = str(exc)
        logging.warning("%s; continuing with announcements endpoint", exc)

    announcement_rows = nse.outcome_announcements(today)
    candidates = filter_announcements(
        announcement_rows,
        today,
        watch_symbols,
        require_calendar_match=args.require_calendar_match,
        include_outside_calendar=args.include_outside_calendar,
    )
    new_items: list[Announcement] = []
    suppressed_duplicate_ids: list[str] = []
    cycle_symbols = set(sent_symbols)
    for item in candidates:
        if item.id in sent:
            continue
        if args.max_reports_per_run and len(new_items) >= args.max_reports_per_run:
            break
        if args.one_per_symbol_per_day and item.symbol in cycle_symbols:
            suppressed_duplicate_ids.append(item.id)
            continue
        new_items.append(item)
        if args.one_per_symbol_per_day:
            cycle_symbols.add(item.symbol)

    now_iso = datetime.now(IST).isoformat()
    for item_id in suppressed_duplicate_ids:
        sent[item_id] = now_iso

    if state.get("initialized_date") != today.iso_date:
        state["initialized_date"] = today.iso_date
        if not args.send_existing_on_first_run:
            for item in new_items:
                sent[item.id] = now_iso
                sent_symbols.add(item.symbol)
            sent_by_date["sent_symbols"] = sorted(sent_symbols)
            save_state(state_path, state)
            logging.info(
                "First run for %s: marked %s existing announcements as seen; suppressed %s duplicate symbol uploads",
                today.iso_date,
                len(new_items),
                len(suppressed_duplicate_ids),
            )
            return 0

    delivered = 0
    for item in new_items:
        logging.info("New result: %s %s %s", item.symbol, item.published_at, item.pdf_url)
        if args.dry_run:
            print(item.caption)
            print()
            sent[item.id] = datetime.now(IST).isoformat()
            sent_symbols.add(item.symbol)
            sent_by_date["sent_symbols"] = sorted(sent_symbols)
            save_state(state_path, state)
            delivered += 1
            if args.exit_after_max_reports and args.max_reports_per_run and delivered >= args.max_reports_per_run:
                raise StopAfterMaxReports(f"Processed configured max reports: {delivered}")
            continue

        if telegram is None:
            raise RuntimeError("Telegram credentials are required unless --dry-run is used")

        content: bytes | None = None
        filename = "nse-result.pdf"
        try:
            content, filename = nse.download_file(item.pdf_url)
        except Exception as exc:
            logging.exception("PDF download failed for %s; sending URL fallback", item.symbol)
            telegram.send_message(item.caption + f"\n\nPDF download failed: {exc}")
            sent[item.id] = datetime.now(IST).isoformat()
            sent_symbols.add(item.symbol)
            sent_by_date["sent_symbols"] = sorted(sent_symbols)
            save_state(state_path, state)
            delivered += 1
            if args.exit_after_max_reports and args.max_reports_per_run and delivered >= args.max_reports_per_run:
                raise StopAfterMaxReports(f"Processed configured max reports: {delivered}")
            continue

        if args.enable_ai_analysis and gemini is not None and not ai_cooldown_active:
            if len(content) > args.max_ai_pdf_bytes:
                send_fallback_message(telegram, item)
            else:
                snapshot = None
                try:
                    snapshot = nse.market_snapshot(item.symbol)
                except Exception as exc:
                    logging.warning("Market snapshot failed for %s: %s", item.symbol, exc)
                try:
                    analysis = gemini.analyze(item, filename, content, snapshot)
                    telegram.send_message(format_directional_bias_alert(item, analysis), parse_mode="HTML")
                    telegram.send_message(format_ai_detail_report(item, analysis, snapshot), parse_mode="HTML")
                except Exception as exc:
                    logging.exception("Gemini analysis failed for %s", item.symbol)
                    if isinstance(exc, GeminiHTTPError) and exc.status_code == 429:
                        cooldown_until = datetime.now(IST) + timedelta(seconds=args.ai_cooldown_seconds)
                        state["ai_cooldown_until"] = cooldown_until.isoformat()
                        save_state(state_path, state)
                        ai_cooldown_active = True
                        logging.warning("Gemini 429 received; pausing AI analysis until %s", cooldown_until.isoformat())
                    send_fallback_message(telegram, item)
        else:
            send_fallback_message(telegram, item)

        if args.send_source_pdf:
            try:
                pdf_caption = f"Source PDF: {item.symbol}\nPublished: {item.published_at or 'n/a'}\n{item.pdf_url}"
                telegram.send_document(filename, content, pdf_caption[:1000])
            except Exception as exc:
                logging.exception("PDF upload failed for %s; sending URL fallback", item.symbol)
                telegram.send_message(item.caption + f"\n\nPDF upload failed: {exc}")

        sent[item.id] = datetime.now(IST).isoformat()
        sent_symbols.add(item.symbol)
        sent_by_date["sent_symbols"] = sorted(sent_symbols)
        save_state(state_path, state)
        delivered += 1
        if args.exit_after_max_reports and args.max_reports_per_run and delivered >= args.max_reports_per_run:
            save_state(state_path, state)
            raise StopAfterMaxReports(f"Processed configured max reports: {delivered}")

    sent_by_date["sent_symbols"] = sorted(sent_symbols)
    save_state(state_path, state)
    logging.info(
        "Watchlist=%s candidates=%s new=%s delivered=%s suppressed_duplicates=%s",
        len(watch_symbols),
        len(candidates),
        len(new_items),
        delivered,
        len(suppressed_duplicate_ids),
    )
    return delivered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Poll NSE result announcements and send PDFs to Telegram")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print new announcements instead of sending Telegram")
    parser.add_argument("--poll-seconds", type=int, default=int(os.getenv("NSE_RESULT_POLL_SECONDS", "60")))
    parser.add_argument("--state-path", default=os.getenv("NSE_RESULT_STATE_PATH", str(DEFAULT_STATE_PATH)))
    parser.add_argument(
        "--send-existing-on-first-run",
        action="store_true",
        default=env_bool("NSE_RESULT_SEND_EXISTING_ON_FIRST_RUN", False),
    )
    parser.add_argument(
        "--require-calendar-match",
        action="store_true",
        default=env_bool("NSE_RESULT_REQUIRE_CALENDAR_MATCH", False),
    )
    parser.add_argument(
        "--include-outside-calendar",
        action=argparse.BooleanOptionalAction,
        default=env_bool("NSE_RESULT_INCLUDE_OUTSIDE_CALENDAR", True),
    )
    parser.add_argument(
        "--one-per-symbol-per-day",
        action=argparse.BooleanOptionalAction,
        default=env_bool("NSE_RESULT_ONE_PER_SYMBOL_PER_DAY", True),
        help="Send only the first financial-result announcement per symbol per India date",
    )
    parser.add_argument(
        "--enable-ai-analysis",
        action=argparse.BooleanOptionalAction,
        default=env_bool(
            "NSE_RESULT_ENABLE_AI_ANALYSIS",
            bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
        ),
        help="Analyze each result PDF with Gemini before sending the PDF",
    )
    parser.add_argument("--gemini-model", default=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"))
    parser.add_argument(
        "--gemini-fallback-models",
        default=",".join(parse_csv_env("GEMINI_FALLBACK_MODELS", ["gemini-1.5-flash"])),
        help="Comma-separated Gemini model fallback list",
    )
    parser.add_argument(
        "--max-ai-pdf-bytes",
        type=int,
        default=int(os.getenv("NSE_RESULT_MAX_AI_PDF_BYTES", str(25 * 1024 * 1024))),
        help="Skip AI analysis for PDFs larger than this byte size",
    )
    parser.add_argument(
        "--max-reports-per-run",
        type=int,
        default=int(os.getenv("NSE_RESULT_MAX_REPORTS_PER_RUN", "0")),
        help="Limit number of new reports processed in one poll cycle; 0 means unlimited",
    )
    parser.add_argument(
        "--exit-after-max-reports",
        action=argparse.BooleanOptionalAction,
        default=env_bool("NSE_RESULT_EXIT_AFTER_MAX_REPORTS", False),
        help="Exit the process after max reports are delivered instead of waiting for the next poll",
    )
    parser.add_argument(
        "--send-source-pdf",
        action=argparse.BooleanOptionalAction,
        default=env_bool("NSE_RESULT_SEND_SOURCE_PDF", False),
        help="Also upload the source PDF as a third Telegram message",
    )
    parser.add_argument(
        "--ai-cooldown-seconds",
        type=int,
        default=int(os.getenv("NSE_RESULT_AI_COOLDOWN_SECONDS", "900")),
        help="Pause AI analysis for this many seconds after one Gemini 429 response",
    )
    parser.add_argument("--proxy-url", default=os.getenv("NSE_RESULT_PROXY_URL"))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return parser


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    load_dotenv()
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    token = os.getenv("NSE_RESULT_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("NSE_RESULT_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    telegram = None if args.dry_run else TelegramClient(token or "", chat_id or "")
    if not args.dry_run and (not token or not chat_id):
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment")

    nse = NSEClient(proxy_url=args.proxy_url)
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    gemini = None
    if args.enable_ai_analysis:
        if gemini_key:
            fallback_models = [part.strip() for part in args.gemini_fallback_models.split(",") if part.strip()]
            gemini = GeminiClient(gemini_key, args.gemini_model, fallback_models)
        else:
            logging.warning("AI analysis enabled but GEMINI_API_KEY/GOOGLE_API_KEY is not set")

    while True:
        try:
            poll_once(args, nse, telegram, gemini)
        except StopAfterMaxReports as exc:
            logging.info("%s", exc)
            return 0
        except NSEAccessError as exc:
            logging.error("%s. If this runs on Render, NSE may be blocking Render's IP range.", exc)
        except Exception:
            logging.exception("Poll cycle failed")

        if args.once:
            return 0
        time.sleep(max(10, args.poll_seconds))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
