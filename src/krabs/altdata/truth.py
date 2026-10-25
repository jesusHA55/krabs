"""Parse and classify public Truth Social posts from the CNN archive.

Archive: https://ix.cnn.io/data/truth-social/truth_archive.csv

The archive is a public snapshot of Donald Trump's Truth Social posts. This
module only reads it; it does not scrape or bypass access controls.
"""

from __future__ import annotations

import csv
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")

# Topic keywords. Deliberately conservative: a post can match multiple topics,
# but generic words like "war" or "import" are not enough on their own.
TOPICS: dict[str, tuple[str, ...]] = {
    "tariffs_trade": (
        "tariff",
        "tariffs",
        "trade war",
        "trade deal",
        "import tax",
        "duties",
        "section 232",
        "section 301",
        "wto",
        "usmca",
    ),
    "energy": (
        "oil",
        "gas",
        "drilling",
        "pipeline",
        "opec",
        "energy independence",
        "keystone",
    ),
    "defense": (
        "military",
        "defense",
        "nato",
        "missile",
        "troops",
        "pentagon",
        "navy",
        "army",
        "air force",
    ),
    "pharma": (
        "pharma",
        "drug",
        "fda",
        "vaccine",
        "medicare",
        "medicine",
    ),
    "banks_fed": (
        "federal reserve",
        "interest rate",
        "inflation",
        "bank",
        "dollar",
    ),
    "tech_china": (
        "semiconductor",
        "chip",
        "tiktok",
        "huawei",
        "taiwan",
        "technology",
    ),
    "immigration": (
        "border",
        "immigration",
        "deport",
        "migrant",
        "ice ",
    ),
}


@dataclass(frozen=True, slots=True)
class Post:
    id: str
    ts: datetime
    content: str
    url: str = ""

    @property
    def text(self) -> str:
        return strip_html(self.content)


def strip_html(value: str) -> str:
    text = html.unescape(TAG_RE.sub(" ", value or ""))
    return SPACE_RE.sub(" ", text).strip()


def classify(content: str) -> list[str]:
    text = strip_html(content).lower()
    return [topic for topic, keys in TOPICS.items() if any(key in text for key in keys)]


def load_archive(path: str | Path) -> list[Post]:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"Truth Social archive not found: {target}")
    posts: list[Post] = []
    if target.suffix.lower() == ".json":
        payload = json.loads(target.read_text(encoding="utf-8"))
        for row in payload:
            created = row.get("created_at") or ""
            if not created:
                continue
            posts.append(
                Post(
                    id=str(row.get("id") or ""),
                    ts=datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(timezone.utc),
                    content=str(row.get("content") or ""),
                    url=str(row.get("url") or ""),
                )
            )
    else:
        with target.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                created = row.get("created_at") or ""
                if not created:
                    continue
                posts.append(
                    Post(
                        id=str(row.get("id") or ""),
                        ts=datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(timezone.utc),
                        content=str(row.get("content") or ""),
                        url=str(row.get("url") or ""),
                    )
                )
    posts.sort(key=lambda post: post.ts)
    return posts
