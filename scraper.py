#!/usr/bin/env python3
"""Casino bonus scraper.

Run:  python scraper.py
Needs: GEMINI_API_KEY in the environment and one public comparison page in
sources.json.

The configured page is fetched once after a robots.txt check. An LLM turns its
free-text terms into exactly three structured records. It never logs in, solves
CAPTCHAs or retries around a block.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.robotparser as robotparser
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent
SOURCES_FILE = HERE / "sources.json"
OUT_FILE = HERE / "bonuses.json"

# Identify yourself honestly and give a contact address.
USER_AGENT = "BonusScreeningBot/1.0 (screening task; contact: your-email@example.com)"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT_SECONDS", "120"))
GEMINI_RETRIES = 2
COUNTRY = os.environ.get("CASINOS_COUNTRY", "DE").upper()
COUNTRY_CURRENCIES = {
    "DE": "EUR",
    "FR": "EUR",
    "FI": "EUR",
    "ES": "EUR",
    "IT": "EUR",
    "NL": "EUR",
    "BE": "EUR",
    "AT": "EUR",
    "IE": "EUR",
    "PT": "EUR",
    "GB": "GBP",
    "SE": "SEK",
    "DK": "DKK",
    "NO": "NOK",
    "PL": "PLN",
    "CZ": "CZK",
    "CH": "CHF",
}
MAX_CHARS = 12000  # enough for a promo page, keeps token cost low
DELAY_SECONDS = 3  # pause between requests

BLOCK_MARKERS = ("captcha", "cf-challenge", "access denied", "verify you are human")

PROMPT = """You extract exactly three casino welcome-bonus records from page text.
Return ONLY a JSON array, no prose, no code fences. Return exactly three objects,
each with exactly these keys:
  "casino": string, the casino's name
  "welcome_bonus": string, the headline welcome bonus as written, e.g. "100% up to 500 EUR"
  "wagering_multiplier": number or null, the wagering requirement as a multiplier, e.g. 35
  "min_deposit": string or null, the minimum deposit with currency, e.g. "20 EUR"
Rules: use only what the text says. If a field is not stated, use null. Do not guess.
If the wagering is given for bonus+deposit, report the multiplier as stated.

PAGE TEXT:
"""


def robots_allows(url: str) -> bool:
    parts = urlparse(url)
    rp = robotparser.RobotFileParser()
    rp.set_url(f"{parts.scheme}://{parts.netloc}/robots.txt")
    try:
        rp.read()
    except Exception:
        # Can't read robots.txt: be conservative and skip.
        return False
    return rp.can_fetch(USER_AGENT, url)


def fetch_text(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    if r.status_code in (401, 403, 429):
        raise RuntimeError(f"blocked (HTTP {r.status_code}), switch source")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "svg"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    if any(m in text.lower()[:2000] for m in BLOCK_MARKERS):
        raise RuntimeError("looks like a challenge/CAPTCHA page, switch source")
    if len(text) < 300:
        raise RuntimeError("almost no text (JS-rendered page?), switch source")
    return text[:MAX_CHARS]


def extract(text: str) -> list[dict]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    request = {
        "contents": [{"parts": [{"text": PROMPT + text}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 1200,
            "responseMimeType": "application/json",
        },
    }
    for attempt in range(GEMINI_RETRIES + 1):
        try:
            response = requests.post(
                GEMINI_URL.format(model=MODEL),
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
                json=request,
                timeout=GEMINI_TIMEOUT,
            )
            break
        except requests.exceptions.Timeout as error:
            if attempt == GEMINI_RETRIES:
                raise RuntimeError(
                    f"Gemini timed out after {GEMINI_TIMEOUT}s across "
                    f"{GEMINI_RETRIES + 1} attempts"
                ) from error
            time.sleep(2 ** attempt)
    if response.status_code in (401, 403):
        raise RuntimeError("Gemini rejected the API key; check that it is active")
    if response.status_code == 429:
        raise RuntimeError("Gemini rate limit reached")
    if response.status_code == 404:
        raise RuntimeError(
            f"Gemini model '{MODEL}' was not found; set GEMINI_MODEL to an "
            "available model in your Google AI Studio account"
        )
    response.raise_for_status()
    payload = response.json()
    try:
        raw = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("Gemini returned no text candidate") from error
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    start = raw.find("[")
    if start < 0:
        raise RuntimeError("Gemini response did not contain a JSON array")
    try:
        data, _ = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Gemini returned incomplete or invalid JSON; try again or use "
            "a different Gemini model"
        ) from error
    if not isinstance(data, list) or len(data) != 3:
        raise ValueError("LLM must return exactly three casino records")
    if any(not isinstance(item, dict) for item in data):
        raise ValueError("LLM returned an invalid casino record")
    return data


def validate(data: dict, page_text: str) -> list[str]:
    """Cheap guard against hallucination: numbers must appear in the source text."""
    problems = []
    flat = page_text.replace(",", "")
    for field in ("welcome_bonus", "min_deposit"):
        val = data.get(field)
        if val:
            for num in re.findall(r"\d+(?:\.\d+)?", str(val)):
                if num not in flat:
                    problems.append(f"{field}: '{num}' not found on page")
    w = data.get("wagering_multiplier")
    if w is not None:
        if not isinstance(w, (int, float)) or not (0 < w <= 200):
            problems.append(f"wagering_multiplier implausible: {w}")
        elif str(int(w) if float(w).is_integer() else w) not in flat:
            problems.append(f"wagering_multiplier: {w} not found on page")
    return problems


def parse_min_deposit(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(
        r"(?:min(?:imum)?\s+deposit|deposit)\s*(?:[:=]|\s+of)?\s*"
        r"([€$£]\s?\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s?[€$£])",
        text,
        flags=re.I,
    )
    if match:
        return match.group(1)
    # Some country feeds describe the minimum deposit in a local language.
    match = re.search(
        r"([€$£]\s?\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s?[€$£])",
        text,
    )
    return match.group(1) if match else None


def currency_from_text(*values: str | None) -> str:
    text = " ".join(str(value) for value in values if value).upper()
    symbols = (("€", "EUR"), ("£", "GBP"), ("$", "USD"), ("CHF", "CHF"))
    for marker, currency in symbols:
        if marker in text:
            return currency
    for code in ("EUR", "GBP", "USD", "SEK", "DKK", "NOK", "PLN", "CZK", "CHF"):
        if re.search(rf"\b{code}\b", text):
            return code
    return COUNTRY_CURRENCIES.get(COUNTRY, "EUR")


def scrape_source() -> list[dict]:
    sources = json.loads(SOURCES_FILE.read_text())
    if len(sources) != 1:
        raise RuntimeError("sources.json must contain exactly one public source")
    source = sources[0]
    url = source["url"]
    if not robots_allows(url):
        raise RuntimeError("disallowed by robots.txt (or unreadable); switch source")
    text = fetch_text(url)
    data = extract(text)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    results = []
    for item in data:
        problems = validate(item, text)
        results.append({
            "casino": item.get("casino"),
            "currency": currency_from_text(
                item.get("welcome_bonus"), item.get("min_deposit")
            ),
            "welcome_bonus": item.get("welcome_bonus"),
            "wagering_multiplier": item.get("wagering_multiplier"),
            "min_deposit": item.get("min_deposit"),
            "source_url": url,
            "scraped_at": now,
            "data_source": source["name"],
            "country": COUNTRY,
            "needs_review": bool(problems),
            "review_notes": problems,
        })
    return results


def main() -> int:
    try:
        results = scrape_source()
        print(f"Extracted {len(results)} records from one public source.")
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1

    OUT_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"Wrote {len(results)} records to {OUT_FILE.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
