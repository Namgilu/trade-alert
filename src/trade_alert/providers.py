from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode

from .http import HttpClient
from .models import NewsArticle, Sentiment, Stock, Theme


_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(value: str) -> str:
    return html.unescape(_TAG_RE.sub("", value)).strip()


def _first(mapping: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return default


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _items(payload: Any) -> list[dict[str, Any]]:
    """Extract the item array while tolerating small upstream response changes."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "stocks", "themes", "result", "data", "output"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _items(value)
            if nested:
                return nested
    return []


class NaverThemeProvider:
    def __init__(
        self,
        http: HttpClient,
        list_url: str,
        stocks_url_template: str,
    ) -> None:
        self.http = http
        self.list_url = list_url
        self.stocks_url_template = stocks_url_template

    def top_themes(self, theme_limit: int, stocks_per_theme: int) -> list[Theme]:
        raw_themes = _items(self.http.json(self.list_url))
        themes: list[Theme] = []
        for raw in raw_themes:
            theme_id = str(_first(raw, ("no", "id", "themeCode", "sectorCode", "code"), "")).strip()
            name = str(_first(raw, ("name", "themeName", "sectorName", "korName"), "")).strip()
            if not theme_id or not name:
                continue
            change_rate = _float(_first(raw, ("changeRate", "fluctuationsRatio", "rate")))
            themes.append(Theme(theme_id, name, change_rate))

        themes.sort(key=lambda item: item.change_rate if item.change_rate is not None else float("-inf"), reverse=True)
        result: list[Theme] = []
        for theme in themes[:theme_limit]:
            url = self.stocks_url_template.format(theme_id=quote(theme.id, safe=""))
            raw_stocks = _items(self.http.json(url))
            stocks: list[Stock] = []
            for raw in raw_stocks:
                code = str(_first(raw, ("itemCode", "itemcode", "stockCode", "symbol", "code"), "")).strip()
                name = str(_first(raw, ("stockName", "itemName", "itemname", "name", "korName"), "")).strip()
                if not code or not name:
                    continue
                rate = _float(
                    _first(raw, ("changeRate", "prevChangeRate", "fluctuationsRatio", "fluctuationRate", "rate"))
                )
                stocks.append(Stock(code, name, rate))
            stocks.sort(key=lambda item: item.change_rate if item.change_rate is not None else float("-inf"), reverse=True)
            result.append(Theme(theme.id, theme.name, theme.change_rate, tuple(stocks[:stocks_per_theme])))
        return result


class NaverNewsProvider:
    endpoint = "https://openapi.naver.com/v1/search/news.json"

    def __init__(self, http: HttpClient, client_id: str, client_secret: str) -> None:
        self.http = http
        self.headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}

    def recent(self, stock: Stock, limit: int, lookback_hours: int, now: datetime) -> list[NewsArticle]:
        params = urlencode({"query": f'"{stock.name}" 주식', "display": limit, "start": 1, "sort": "date"})
        payload = self.http.json(f"{self.endpoint}?{params}", headers=self.headers)
        cutoff = now.astimezone(timezone.utc) - timedelta(hours=lookback_hours)
        result: list[NewsArticle] = []
        seen: set[str] = set()
        for item in payload.get("items", []):
            try:
                published = parsedate_to_datetime(item["pubDate"])
            except (KeyError, TypeError, ValueError):
                continue
            if published.astimezone(timezone.utc) < cutoff:
                continue
            title = _clean_html(str(item.get("title", "")))
            url = str(item.get("originallink") or item.get("link") or "")
            identity = url or title
            if not title or identity in seen:
                continue
            seen.add(identity)
            result.append(
                NewsArticle(
                    title=title,
                    description=_clean_html(str(item.get("description", ""))),
                    url=url,
                    published_at=published,
                )
            )
        return result


class JevSentimentProvider:
    def __init__(self, http: HttpClient, api_url: str, api_key: str, model: str) -> None:
        self.http = http
        self.api_url = api_url
        self.api_key = api_key
        self.model = model

    def analyze(self, stock: Stock, articles: list[NewsArticle]) -> Sentiment | None:
        if not articles:
            return None
        totals = {"positive": 0.0, "neutral": 0.0, "negative": 0.0}
        analyzed = 0
        for offset in range(0, len(articles), 6):
            chunk = articles[offset : offset + 6]
            state = [
                {
                    "id": f"news_{offset + index}",
                    "company": stock.name,
                    "title": article.title,
                    "description": article.description,
                }
                for index, article in enumerate(chunk)
            ]
            questions = {
                row["id"]: {
                    "type": "choice",
                    "instructions": (
                        f"state 배열에서 id가 {row['id']}인 뉴스가 {stock.name}의 향후 사업 또는 주가에 "
                        "미치는 방향을 판단하세요. 단순 시장 설명은 중립으로 분류하세요."
                    ),
                    "criteria": {
                        "positive": "실적, 수주, 제품, 규제 또는 수급 측면에서 기업에 유리함",
                        "neutral": "영향이 불명확하거나 사실 전달 중심임",
                        "negative": "실적, 사업, 규제 또는 수급 측면에서 기업에 불리함",
                    },
                }
                for row in state
            }
            payload = self.http.json(
                self.api_url,
                method="POST",
                headers={"Authorization": f"Bearer {self.api_key}"},
                body={"model": self.model, "state": state, "questions": questions},
            )
            answers = payload.get("answers", {})
            for row in state:
                answer = answers.get(row["id"], {})
                probabilities = answer.get("probabilities") or {}
                choice = answer.get("choice")
                if not probabilities and choice in totals:
                    probabilities = {choice: 1.0}
                if not probabilities:
                    continue
                for label in totals:
                    totals[label] += float(probabilities.get(label, 0.0))
                analyzed += 1
        if analyzed == 0:
            raise RuntimeError("JEV returned no usable sentiment answers")
        return Sentiment(
            positive=totals["positive"] / analyzed,
            neutral=totals["neutral"] / analyzed,
            negative=totals["negative"] / analyzed,
            article_count=analyzed,
        )


class TelegramNotifier:
    def __init__(self, http: HttpClient, bot_token: str, chat_id: str) -> None:
        self.http = http
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, message: str) -> None:
        # Telegram limits a text message to 4096 characters. Keep chunks below it.
        for start in range(0, len(message), 3900):
            self.http.json(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                method="POST",
                body={"chat_id": self.chat_id, "text": message[start : start + 3900], "disable_web_page_preview": True},
            )
