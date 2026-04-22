from __future__ import annotations

MOCK_MARKETS: list[dict[str, str]] = [
    {
        "market_id": "MKT-TRUMP-001",
        "slug": "trump-mentions-nato-this-week",
        "subject": "trump",
        "prompt": "Will Donald Trump mention NATO by Friday?",
    },
    {
        "market_id": "MKT-TRUMP-002",
        "slug": "trump-mentions-fed-this-week",
        "subject": "trump",
        "prompt": "Will Donald Trump mention the Federal Reserve by Friday?",
    },
    {
        "market_id": "MKT-TRUMP-003",
        "slug": "trump-mentions-border-this-week",
        "subject": "trump",
        "prompt": "Will Donald Trump mention border security by Friday?",
    },
    {
        "market_id": "MKT-TRUMP-004",
        "slug": "trump-mentions-tariffs-this-week",
        "subject": "trump",
        "prompt": "Will Donald Trump mention tariffs by Friday?",
    },
    {
        "market_id": "MKT-LEAVITT-001",
        "slug": "leavitt-mentions-press-briefing-today",
        "subject": "leavitt",
        "prompt": "Will Karoline Leavitt mention press briefing in today's remarks?",
    },
    {
        "market_id": "MKT-LEAVITT-002",
        "slug": "leavitt-mentions-immigration-today",
        "subject": "leavitt",
        "prompt": "Will Karoline Leavitt mention immigration in today's remarks?",
    },
    {
        "market_id": "MKT-LEAVITT-003",
        "slug": "leavitt-mentions-economy-today",
        "subject": "leavitt",
        "prompt": "Will Karoline Leavitt mention the economy in today's remarks?",
    },
    {
        "market_id": "MKT-LEAVITT-004",
        "slug": "leavitt-mentions-energy-today",
        "subject": "leavitt",
        "prompt": "Will Karoline Leavitt mention energy in today's remarks?",
    },
    {
        "market_id": "MKT-MAMDANI-001",
        "slug": "mamdani-mentions-rent-freeze-this-week",
        "subject": "mamdani",
        "prompt": "Will Zohran Mamdani mention rent freeze by Friday?",
    },
    {
        "market_id": "MKT-MAMDANI-002",
        "slug": "mamdani-mentions-transit-this-week",
        "subject": "mamdani",
        "prompt": "Will Zohran Mamdani mention public transit by Friday?",
    },
    {
        "market_id": "MKT-MAMDANI-003",
        "slug": "mamdani-mentions-housing-this-week",
        "subject": "mamdani",
        "prompt": "Will Zohran Mamdani mention housing by Friday?",
    },
    {
        "market_id": "MKT-MAMDANI-004",
        "slug": "mamdani-mentions-union-this-week",
        "subject": "mamdani",
        "prompt": "Will Zohran Mamdani mention unions by Friday?",
    },
]


SUBJECT_PHRASES: dict[str, list[str]] = {
    "trump": [
        "donald trump",
        "trump",
    ],
    "leavitt": [
        "karoline leavitt",
        "leavitt",
    ],
    "mamdani": [
        "zohran mamdani",
        "mamdani",
    ],
}


MARKET_PHRASES: dict[str, list[str]] = {
    "MKT-TRUMP-001": ["nato", "n.a.t.o."],
    "MKT-TRUMP-002": ["federal reserve", "the fed"],
    "MKT-TRUMP-003": ["border security", "border"],
    "MKT-TRUMP-004": ["tariffs", "tariff"],
    "MKT-LEAVITT-001": ["press briefing"],
    "MKT-LEAVITT-002": ["immigration", "immigrant", "immigrants"],
    "MKT-LEAVITT-003": ["economy", "economic"],
    "MKT-LEAVITT-004": ["energy"],
    "MKT-MAMDANI-001": ["rent freeze", "rent control"],
    "MKT-MAMDANI-002": ["public transit", "transit", "subway", "mta"],
    "MKT-MAMDANI-003": ["housing"],
    "MKT-MAMDANI-004": ["union", "unions", "labor union"],
}


def all_market_phrases() -> list[str]:
    """Deduplicated flat list of every resolution phrase across all markets."""
    seen: set[str] = set()
    result: list[str] = []
    for phrases in MARKET_PHRASES.values():
        for p in phrases:
            if p not in seen:
                seen.add(p)
                result.append(p)
    return result

