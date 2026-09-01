"""Real numbers for the ranked lists, pulled from Wikipedia for nothing.

The model cannot be trusted with these. Asked for the largest banks by
assets it returned UBS at $5.0T with no Chinese or American bank in the top
eight; the actual answer is ICBC at $7.6T with four Chinese banks ahead of
JPMorgan. Every figure was invented, and it publishes automatically under
the account's name, so grounding is not optional.

Wikipedia's action API serves the parsed article, and the ranked "List of
countries by X" tables are exactly the data these posts want. It is free and
needs no key, unlike the search tool this pipeline dropped on cost.

The model still writes the tweet -- it picks the interesting rows, shortens
the names and assigns flags -- but it does so over rows it can actually see.
"""

import html
import re

import requests

API = "https://en.wikipedia.org/w/api.php"
UA = {"User-Agent": "404-pipeline/1.0 (https://github.com/sauravpoudel10/404)"}

# subject -> the Wikipedia article carrying that ranking. Every entry here is
# verified by tools/check_reference.py, which fetches each page and reports
# whether a usable table came back; anything that stops resolving should be
# removed rather than left to fail silently at 3am.
SOURCES = {
    # economy
    "largest economies by nominal GDP": "List of countries by GDP (nominal)",
    "largest economies by purchasing power": "List of countries by GDP (PPP)",
    "highest GDP per capita": "List of countries by GDP (nominal) per capita",
    "fastest growing economies": "List of countries by real GDP growth rate",
    # debt and money
    "highest government debt as a share of GDP": "List of countries by government budget",
    "largest foreign currency reserves": "List of countries by foreign-exchange reserves",
    "highest inflation rates": "List of countries by inflation rate",
    "highest central bank interest rates": "List of countries by central bank interest rates",
    # trade
    "largest exporters of goods": "List of countries by exports",
    "largest importers of goods": "List of countries by imports",
    "busiest container ports": "List of busiest container ports",
    # markets and companies
    "most valuable companies in the world": "List of public corporations by market capitalization",
    "companies with the highest revenue": "List of largest companies by revenue",
    "companies with the most employees": "List of largest employers",
    "largest banks by assets": "List of largest banks",
    "largest stock exchanges by market value": "List of stock exchanges",
    # people
    "richest people in the world": "The World's Billionaires",
    "countries with the most billionaires": "List of countries by number of billionaires",
    # energy
    "largest oil producers": "List of countries by oil production",
    "largest natural gas producers": "List of countries by natural gas production",
    "largest electricity producers": "List of countries by electricity production",
    # commodities and industry
    "largest gold reserves held by countries": "Gold reserve",
    "largest steel producers": "List of countries by steel production",
    "largest cement producers": "List of countries by cement production",
    "largest wheat producers": "List of countries by wheat production",
    "largest coffee producers": "List of countries by coffee production",
    "largest wine producers": "List of wine-producing countries",
    # defence
    "highest military spending": "List of countries by military expenditures",
    "largest active armed forces": "List of countries by number of military and paramilitary personnel",
    # population and society
    "most populous countries": "List of countries and dependencies by population",
    "largest cities by population": "List of largest cities",
    "longest life expectancy": "List of countries by life expectancy",
    "countries with the oldest populations": "List of countries by median age",
    "most hospital beds per 1,000 people": "List of countries by hospital beds",
    "highest healthcare spending per person": "List of countries by total health expenditure per capita",
    # work and prices
    "highest average salaries": "List of countries by average wage",
    "highest minimum wages": "List of countries by minimum wage",
    "highest corporate tax rates": "List of countries by tax rates",
    # transport
    "busiest airports by passengers": "List of busiest airports by passenger traffic",
    "largest car producing countries": "List of countries by motor vehicle production",
    # tech and research
    "fastest average internet speeds": "List of countries by Internet connection speeds",
    "most internet users": "List of countries by number of Internet users",
    "highest research spending as a share of GDP": "List of countries by research and development spending",
    # climate and land
    "largest carbon emitters": "List of countries by carbon dioxide emissions",
    "largest forest area": "List of countries by forest area",
    # leisure
    "most visited countries by tourists": "World Tourism rankings",
    "most valuable football clubs": "Forbes' list of the most valuable football clubs",
    "most Olympic medals won": "All-time Olympic Games medal table",
    "most powerful passports by visa free access": "Henley Passport Index",
}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)
TABLE_RE = re.compile(r'<table[^>]*class="[^"]*wikitable[^"]*"[^>]*>(.*?)</table>', re.S)
NUM_RE = re.compile(r"\d")
# Wide enough to reach the metric on tables that put several labelling
# columns first (busiest airports: rank, name, city, country, code,
# then passengers), narrow enough not to bloat the prompt.
COLUMNS = 7
# Footnote markers and citation brackets survive tag-stripping and read as
# part of the number otherwise: "7,645.80[1]".
NOTE_RE = re.compile(r"\[[^\[\]]{0,14}\]")


def _clean(cell: str) -> str:
    text = html.unescape(TAG_RE.sub(" ", cell))
    return WS_RE.sub(" ", NOTE_RE.sub("", text)).strip()


NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

# Rows that are totals or blocs rather than entrants. They sort above every
# real entry, so "largest economies by nominal GDP" led on "World".
AGGREGATES = {
    "world", "total", "world total", "global", "european union", "eu",
    "eu27", "euro area", "eurozone", "africa", "asia", "europe", "oceania",
    "north america", "south america", "latin america", "americas",
    "middle east", "oecd", "g7", "g20", "arab world", "asean",
    "european union (27)", "all countries", "rest of world", "others",
    "other", "sum", "average", "subtotal",
}


def _is_aggregate(row: list[str]) -> bool:
    """True when the row labels a total or a bloc rather than an entrant.

    Only the NAME cell counts. Several of these tables carry a continent
    column, and testing it as well threw away every row of the reserves
    table -- "Asia" is an aggregate as a row label and a plain attribute as
    a column value.
    """
    if not row:
        return False
    cell = row[0]
    # A leading rank column is not the name; the name is the next cell.
    if NUMBER_RE.fullmatch(cell.strip().rstrip(".")) and len(row) > 1:
        cell = row[1]
    name = re.sub(r"[^a-z0-9 ]", "", cell.lower()).strip()
    return name in AGGREGATES


def _num(cell: str) -> float | None:
    """The leading number in a cell, or None. "8,133.5 t" -> 8133.5"""
    match = NUMBER_RE.search(cell.replace("\u2212", "-"))
    if not match:
        return None
    try:
        return float(match.group().replace(",", ""))
    except ValueError:
        return None


def _is_ranked(rows: list[list[str]]) -> bool:
    """True when column 0 already counts 1, 2, 3 ... down the table."""
    seen = [_num(r[0]) for r in rows[:6] if r]
    seen = [v for v in seen if v is not None]
    return len(seen) >= 4 and seen == sorted(seen) and seen[0] <= 2


def _sort_key_column(rows: list[list[str]]) -> int | None:
    """Index of the column the table should be ranked on.

    The leftmost mostly-numeric column after the name, which on these
    articles is the headline metric -- active personnel before reserves,
    this year's output before last year's.
    """
    width = max(len(r) for r in rows)
    for col in range(1, width):
        values = [_num(r[col]) for r in rows if len(r) > col]
        filled = [v for v in values if v is not None]
        if len(filled) >= 0.8 * len(rows):
            return col
    return None


def _rank(rows: list[list[str]]) -> list[list[str]]:
    """Sort largest-first unless the table is already in rank order."""
    if len(rows) < 4 or _is_ranked(rows):
        return rows
    col = _sort_key_column(rows)
    if col is None:
        return rows
    keyed = [(r, _num(r[col]) if len(r) > col else None) for r in rows]
    if any(v is None for _, v in keyed):
        keyed = [(r, v) for r, v in keyed if v is not None]
    return [r for r, _ in sorted(keyed, key=lambda pair: pair[1], reverse=True)]


def _rows(table_html: str) -> list[list[str]]:
    out = []
    for row in ROW_RE.findall(table_html):
        cells = [_clean(c) for c in CELL_RE.findall(row)]
        cells = [c for c in cells if c]
        if cells:
            out.append(cells)
    return out


def fetch(subject: str, limit: int = 16, timeout: int = 25) -> list[str] | None:
    """Return the ranking for `subject` as plain "a | b | c" lines.

    The largest wikitable on the page is used rather than a hard-coded index:
    articles gain and lose infoboxes and sub-tables, and the ranking is
    reliably the biggest table on a "List of ..." page. Returns None on any
    failure -- a missing source must cost one list, not the whole pool.
    """
    page = SOURCES.get(subject)
    if not page:
        return None
    try:
        resp = requests.get(
            API, headers=UA, timeout=timeout,
            params={"action": "parse", "page": page, "prop": "text",
                    "format": "json", "formatversion": 2, "redirects": 1},
        )
        resp.raise_for_status()
        body = resp.json()["parse"]["text"]
    except Exception as e:
        print(f"    reference '{subject}' unavailable ({type(e).__name__})")
        return None

    tables = [_rows(t) for t in TABLE_RE.findall(body)]
    tables = [t for t in tables if len(t) >= 6]
    if not tables:
        print(f"    reference '{subject}': no usable table on '{page}'")
        return None
    table = max(tables, key=len)

    header, *body_rows = table
    # Rows without a digit are section separators or notes, not rankings.
    data = [r for r in body_rows
            if any(NUM_RE.search(c) for c in r) and not _is_aggregate(r)]
    data = _rank(data)[:limit]
    if len(data) < 6:
        print(f"    reference '{subject}': only {len(data)} rows on '{page}'")
        return None

    lines = [" | ".join(header[:COLUMNS])]
    lines += [" | ".join(r[:COLUMNS]) for r in data]
    return lines


YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")


def detect_year(lines: list[str]) -> str:
    """The year the table is for, from its header, or "" if it names none.

    Wikipedia puts it in the column heading -- "Total assets (April 2026)
    (US$ billion)" -- which is the only place it can be read from honestly.
    Guessing one would date a ranking that might be three years old.
    """
    if not lines:
        return ""
    years = YEAR_RE.findall(lines[0])
    return max(years) if years else ""


def as_context(subject: str, lines: list[str]) -> str:
    return f"### {subject}\n" + "\n".join(lines)
