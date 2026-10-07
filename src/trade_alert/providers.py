from __future__ import annotations

import html
import io
import math
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from .http import HttpClient
from .models import DailyBar, EventSummary, MarketEvent, PreopenQuote, Stock, Theme


_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_NOISE_RE = re.compile(r"[^0-9a-z가-힣]+", re.IGNORECASE)


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
    """Extract an item array while tolerating small upstream response changes."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "stocks", "themes", "result", "data", "output", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _items(value)
            if nested:
                return nested
    return []


def _query_url(url: str, **updates: Any) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in updates.items()})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def deduplicate_events(events: list[MarketEvent], limit: int) -> list[MarketEvent]:
    """Prefer disclosures and newer articles while removing syndicated headlines."""
    ordered = sorted(
        events,
        key=lambda event: (event.source_kind == "disclosure", event.published_at),
        reverse=True,
    )
    result: list[MarketEvent] = []
    fingerprints: list[str] = []
    for event in ordered:
        fingerprint = _TITLE_NOISE_RE.sub("", event.title.lower())
        if not fingerprint:
            continue
        if any(fingerprint == seen or fingerprint[:28] == seen[:28] for seen in fingerprints):
            continue
        fingerprints.append(fingerprint)
        result.append(event)
        if len(result) >= limit:
            break
    return result


class NaverThemeProvider:
    def __init__(self, http: HttpClient, list_url: str, stocks_url_template: str) -> None:
        self.http = http
        self.list_url = list_url
        self.stocks_url_template = stocks_url_template

    @staticmethod
    def _leading_stocks(value: Any, limit: int) -> tuple[Stock, ...]:
        stocks: list[Stock] = []
        for item in str(value or "").split("|"):
            parts = [part.strip() for part in item.split(",")]
            if len(parts) < 3 or not parts[1] or not parts[2]:
                continue
            stocks.append(Stock(parts[1], ",".join(parts[2:])))
        return tuple(stocks[:limit])

    def _list_themes(self, theme_limit: int, stocks_per_theme: int = 0) -> list[Theme]:
        list_url = _query_url(self.list_url, startIdx=0, pageSize=min(theme_limit, 100))
        raw_themes = _items(self.http.json(list_url))
        themes: list[Theme] = []
        for raw in raw_themes:
            theme_id = str(_first(raw, ("no", "id", "themeCode", "sectorCode", "code"), "")).strip()
            name = str(_first(raw, ("name", "themeName", "sectorName", "korName"), "")).strip()
            if not theme_id or not name:
                continue
            total_count = _float(_first(raw, ("totalCnt", "totalCount")))
            rise_count = _float(_first(raw, ("riseCnt", "riseCount")))
            breadth = rise_count / total_count if total_count and rise_count is not None else None
            themes.append(
                Theme(
                    id=theme_id,
                    name=name,
                    change_rate=_float(_first(raw, ("changeRate", "fluctuationsRatio", "rate"))),
                    breadth=breadth,
                    trading_value=_float(_first(raw, ("totalAccAmount", "tradeAmount", "tradingValue"))),
                    stocks=self._leading_stocks(raw.get("leadingItem"), stocks_per_theme),
                )
            )

        return themes[:theme_limit]

    def hydrate_theme(self, theme: Theme, stocks_per_theme: int, *, order_type: str = "up") -> Theme:
        url = _query_url(
            self.stocks_url_template.format(theme_id=quote(theme.id, safe="")),
            orderType=order_type,
            startIdx=0,
            pageSize=max(stocks_per_theme, 3),
        )
        stocks: list[Stock] = []
        for raw in _items(self.http.json(url)):
            code = str(_first(raw, ("itemCode", "itemcode", "stockCode", "symbol", "code"), "")).strip()
            name = str(_first(raw, ("stockName", "itemName", "itemname", "name", "korName"), "")).strip()
            if not code or not name:
                continue
            stocks.append(
                Stock(
                    code=code,
                    name=name,
                    change_rate=_float(_first(raw, ("changeRate", "prevChangeRate", "fluctuationsRatio", "rate"))),
                    trade_amount=_float(_first(raw, ("tradeAmount", "accumulatedTradingValue"))),
                    trade_volume=_float(_first(raw, ("tradeVolume", "accumulatedTradingVolume"))),
                    previous_volume=_float(_first(raw, ("prevQuant", "previousTradingVolume"))),
                )
            )
        stocks.sort(key=lambda item: item.change_rate if item.change_rate is not None else float("-inf"), reverse=True)
        return Theme(
            id=theme.id,
            name=theme.name,
            change_rate=theme.change_rate,
            breadth=theme.breadth,
            trading_value=theme.trading_value,
            stocks=tuple(stocks[:stocks_per_theme]),
        )

    def screening_themes(self, theme_limit: int, stocks_per_theme: int) -> list[Theme]:
        result: list[Theme] = []
        for theme in self._list_themes(theme_limit):
            try:
                hydrated = self.hydrate_theme(theme, stocks_per_theme, order_type="marketSum")
            except Exception:
                continue
            if hydrated.stocks:
                result.append(hydrated)
        return result

    def top_themes(self, theme_limit: int, stocks_per_theme: int) -> list[Theme]:
        themes = self._list_themes(theme_limit)
        themes.sort(key=lambda item: item.change_rate if item.change_rate is not None else float("-inf"), reverse=True)
        return [self.hydrate_theme(theme, stocks_per_theme) for theme in themes[:theme_limit]]


class NaverNewsProvider:
    endpoint = "https://openapi.naver.com/v1/search/news.json"

    def __init__(self, http: HttpClient, client_id: str, client_secret: str) -> None:
        self.http = http
        self.headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}

    def recent(self, stock: Stock, limit: int, lookback_hours: int, now: datetime) -> list[MarketEvent]:
        params = urlencode({"query": f'"{stock.name}" 주식', "display": limit, "start": 1, "sort": "date"})
        payload = self.http.json(f"{self.endpoint}?{params}", headers=self.headers)
        cutoff = now.astimezone(timezone.utc) - timedelta(hours=lookback_hours)
        result: list[MarketEvent] = []
        for item in payload.get("items", []):
            try:
                published = parsedate_to_datetime(item["pubDate"])
            except (KeyError, TypeError, ValueError):
                continue
            if published.astimezone(timezone.utc) < cutoff:
                continue
            title = _clean_html(str(item.get("title", "")))
            if not title:
                continue
            result.append(
                MarketEvent(
                    title=title,
                    description=_clean_html(str(item.get("description", ""))),
                    url=str(item.get("originallink") or item.get("link") or ""),
                    published_at=published,
                    source_kind="news",
                )
            )
        return result


class OpenDartProvider:
    corp_code_url = "https://opendart.fss.or.kr/api/corpCode.xml"
    disclosure_url = "https://opendart.fss.or.kr/api/list.json"

    def __init__(self, http: HttpClient, api_key: str) -> None:
        self.http = http
        self.api_key = api_key
        self._corp_codes: dict[str, str] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _load_corp_codes(self) -> dict[str, str]:
        if self._corp_codes is not None:
            return self._corp_codes
        if not self.enabled:
            self._corp_codes = {}
            return self._corp_codes
        raw = self.http.bytes(f"{self.corp_code_url}?{urlencode({'crtfc_key': self.api_key})}")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            xml_name = next(name for name in archive.namelist() if name.lower().endswith(".xml"))
            root = ElementTree.fromstring(archive.read(xml_name))
        self._corp_codes = {
            (node.findtext("stock_code") or "").strip(): (node.findtext("corp_code") or "").strip()
            for node in root.findall("list")
            if (node.findtext("stock_code") or "").strip()
        }
        return self._corp_codes

    def recent(self, stock: Stock, lookback_hours: int, now: datetime) -> list[MarketEvent]:
        if not self.enabled:
            return []
        corp_code = self._load_corp_codes().get(stock.code)
        if not corp_code:
            return []
        seoul_now = now.astimezone(ZoneInfo("Asia/Seoul"))
        start = (seoul_now - timedelta(hours=lookback_hours)).strftime("%Y%m%d")
        end = seoul_now.strftime("%Y%m%d")
        params = urlencode(
            {
                "crtfc_key": self.api_key,
                "corp_code": corp_code,
                "bgn_de": start,
                "end_de": end,
                "page_no": 1,
                "page_count": 20,
                "sort": "date",
                "sort_mth": "desc",
            }
        )
        payload = self.http.json(f"{self.disclosure_url}?{params}")
        if payload.get("status") == "013":
            return []
        if payload.get("status") not in (None, "000"):
            raise RuntimeError(f"OpenDART error {payload.get('status')}: {payload.get('message', '')}")
        events: list[MarketEvent] = []
        for item in payload.get("list", []):
            receipt = str(item.get("rcept_no", ""))
            title = str(item.get("report_nm", "")).strip()
            date_text = str(item.get("rcept_dt", ""))
            if not receipt or not title or len(date_text) != 8:
                continue
            published = datetime.strptime(date_text, "%Y%m%d").replace(tzinfo=ZoneInfo("Asia/Seoul"))
            events.append(
                MarketEvent(
                    title=title,
                    description=f"공시 제출인: {item.get('flr_nm', '')}",
                    url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}",
                    published_at=published,
                    source_kind="disclosure",
                )
            )
        return events


class KisPreopenProvider:
    token_path = "/oauth2/tokenP"
    quote_path = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
    quote_tr_id = "FHKST01010200"
    history_path = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    history_tr_id = "FHKST03010100"

    def __init__(
        self,
        http: HttpClient,
        app_key: str,
        app_secret: str,
        base_url: str,
        request_interval_seconds: float = 0.0,
    ) -> None:
        self.http = http
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.request_interval_seconds = max(0.0, request_interval_seconds)
        self._access_token: str | None = None
        self._last_request_at = 0.0
        self._history_cache: dict[tuple[str, date, date], tuple[DailyBar, ...]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.app_key and self.app_secret)

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        if not self.enabled:
            raise RuntimeError("08:55 preopen mode requires KIS_APP_KEY and KIS_APP_SECRET")
        payload = self.http.json(
            f"{self.base_url}{self.token_path}",
            method="POST",
            body={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
        )
        token = str(payload.get("access_token", "")).strip()
        if not token:
            raise RuntimeError(f"KIS access token error: {payload.get('error_description') or payload.get('msg1') or 'empty token'}")
        self._access_token = token
        return token

    def _market_json(self, path: str, tr_id: str, params: dict[str, str]) -> dict[str, Any]:
        wait = self.request_interval_seconds - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        payload = self.http.json(
            f"{self.base_url}{path}?{urlencode(params)}",
            headers={
                "authorization": f"Bearer {self._token()}",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
                "tr_id": tr_id,
                "custtype": "P",
            },
        )
        self._last_request_at = time.monotonic()
        if str(payload.get("rt_cd", "0")) != "0":
            raise RuntimeError(f"KIS market data error {payload.get('msg_cd', '')}: {payload.get('msg1', '')}")
        return payload

    @staticmethod
    def _object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
        return {}

    def quote(self, stock: Stock) -> PreopenQuote:
        payload = self._market_json(
            self.quote_path,
            self.quote_tr_id,
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock.code},
        )
        orderbook = self._object(payload.get("output1"))
        expected = self._object(payload.get("output2"))
        quote = PreopenQuote(
            expected_price=_float(_first(expected, ("antc_cnpr", "stck_prpr"))),
            expected_change_rate=_float(_first(expected, ("antc_cntg_prdy_ctrt", "prdy_ctrt"))),
            expected_volume=_float(_first(expected, ("antc_vol", "antc_cnqn"))),
            total_ask_volume=_float(orderbook.get("total_askp_rsqn")),
            total_bid_volume=_float(orderbook.get("total_bidp_rsqn")),
        )
        if quote.expected_price is None and quote.expected_change_rate is None:
            raise RuntimeError("KIS response did not include pre-open expected execution data")
        return quote

    def history(self, stock: Stock, start: date, end: date) -> tuple[DailyBar, ...]:
        cache_key = (stock.code, start, end)
        if cache_key in self._history_cache:
            return self._history_cache[cache_key]
        bars: dict[date, DailyBar] = {}
        cursor_end = end
        for _ in range(2):
            payload = self._market_json(
                self.history_path,
                self.history_tr_id,
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": stock.code,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": cursor_end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "0",
                },
            )
            rows = payload.get("output2") if isinstance(payload.get("output2"), list) else []
            page_dates: list[date] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    day = datetime.strptime(str(row.get("stck_bsop_date", "")), "%Y%m%d").date()
                except ValueError:
                    continue
                open_price = _float(row.get("stck_oprc"))
                high = _float(row.get("stck_hgpr"))
                low = _float(row.get("stck_lwpr"))
                close = _float(row.get("stck_clpr"))
                volume = _float(row.get("acml_vol"))
                if None in (open_price, high, low, close, volume) or close <= 0 or low <= 0:
                    continue
                bars[day] = DailyBar(day, open_price, high, low, close, volume)
                page_dates.append(day)
            if not page_dates or len(rows) < 100:
                break
            oldest = min(page_dates)
            if oldest <= start:
                break
            cursor_end = oldest - timedelta(days=1)
        result = tuple(bars[day] for day in sorted(bars) if start <= day <= end)
        self._history_cache[cache_key] = result
        return result


class JevEventProvider:
    impact_values = {
        "strong_negative": -2.0,
        "negative": -1.0,
        "neutral": 0.0,
        "positive": 1.0,
        "strong_positive": 2.0,
    }

    def __init__(self, http: HttpClient, api_url: str, api_key: str, model: str) -> None:
        self.http = http
        self.api_url = api_url
        self.api_key = api_key
        self.model = model

    @staticmethod
    def _noul(answer: dict[str, Any], default: float) -> float:
        try:
            return max(0.0, min(1.0, float(answer.get("noul", default))))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _choice(answer: dict[str, Any], default: str) -> str:
        value = answer.get("choice")
        return str(value) if value else default

    def analyze(self, stock: Stock, events: list[MarketEvent], now: datetime) -> EventSummary | None:
        if not events:
            return None
        state = [
            {
                "id": f"event_{index}",
                "company": stock.name,
                "stock_code": stock.code,
                "market": "대한민국 KRX(KOSPI/KOSDAQ)",
                "source": event.source_kind,
                "published_at": event.published_at.isoformat(),
                "title": event.title,
                "description": event.description,
            }
            for index, event in enumerate(events)
        ]
        primary_criteria = {row["id"]: row["title"][:240] for row in state}
        primary_criteria["none"] = "해당 기업 주가에 직접 영향을 줄 만한 이벤트가 없음"
        questions = {
            "relevance": {
                "type": "noul",
                "instructions": (
                    f"이 자료 묶음에 {stock.name}({stock.code})와 직접 관련되고 한국 정규장 가격에 "
                    "영향을 줄 만한 신규 이벤트가 하나 이상 존재한다. 동명이인과 단순 시황 언급은 제외한다."
                ),
            },
            "primary_event": {
                "type": "choice",
                "instructions": "다음 정규장 가격에 가장 큰 영향을 줄 이벤트 하나를 선택한다.",
                "criteria": primary_criteria,
            },
            "event_type": {
                "type": "choice",
                "instructions": "가장 중요한 이벤트의 유형을 선택한다.",
                "criteria": {
                    "earnings": "실적 또는 실적 전망",
                    "contract": "수주, 공급계약, 파트너십",
                    "policy": "정부 정책, 규제, 허가",
                    "product": "제품, 기술, 임상, 연구개발",
                    "financing": "유상증자, 전환사채, 자금조달",
                    "governance": "지분, 최대주주, 경영권",
                    "legal_risk": "소송, 사고, 제재, 회수",
                    "market_commentary": "단순 시황, 전망, 반복 보도",
                },
            },
            "impact": {
                "type": "choice",
                "instructions": (
                    "가장 중요한 이벤트가 한국 시장의 다음 정규장 주가에 미칠 방향과 강도를 판단한다. "
                    "기사 문체가 아니라 경제적 효과를 기준으로 한다."
                ),
                "criteria": {
                    "strong_negative": "직접적이고 큰 악재",
                    "negative": "제한적인 악재",
                    "neutral": "영향 불명확 또는 이미 알려진 정보",
                    "positive": "제한적인 호재",
                    "strong_positive": "직접적이고 큰 호재",
                },
            },
            "horizon": {
                "type": "choice",
                "instructions": "가격 영향이 가장 뚜렷할 것으로 예상되는 기간을 고른다.",
                "criteria": {
                    "open": "다음 시초가와 장 초반",
                    "intraday": "당일 장중",
                    "short_term": "수일에서 수주",
                    "long_term": "수개월 이상",
                },
            },
            "confirmed": {
                "type": "noul",
                "instructions": "핵심 내용이 공식 공시나 확정된 사실이며 추측성 전망이 아니다.",
            },
        }
        payload = self.http.json(
            self.api_url,
            method="POST",
            headers={"Authorization": f"Bearer {self.api_key}"},
            body={"model": self.model, "state": state, "questions": questions},
        )
        answers = payload.get("answers", {})
        relevance = self._noul(answers.get("relevance", {}), 0.5)
        confirmed = self._noul(answers.get("confirmed", {}), 0.5)
        impact_answer = answers.get("impact", {})
        probabilities = impact_answer.get("probabilities") or {}
        if probabilities:
            impact = sum(self.impact_values[label] * float(probabilities.get(label, 0.0)) for label in self.impact_values)
        else:
            impact = self.impact_values.get(self._choice(impact_answer, "neutral"), 0.0)
        try:
            confidence = float(impact_answer.get("confidence", max(probabilities.values(), default=0.7)))
        except (TypeError, ValueError):
            confidence = 0.7
        primary_id = self._choice(answers.get("primary_event", {}), "none")
        primary_event = None
        if primary_id.startswith("event_"):
            try:
                primary_event = events[int(primary_id.removeprefix("event_"))]
            except (ValueError, IndexError):
                primary_event = None
        if primary_event is None and relevance >= 0.5:
            primary_event = events[0]
        if primary_event and primary_event.source_kind == "disclosure":
            confirmed = max(confirmed, 0.9)
        freshness = 0.5
        if primary_event:
            age_hours = max(0.0, (now.astimezone(timezone.utc) - primary_event.published_at.astimezone(timezone.utc)).total_seconds() / 3600)
            freshness = math.exp(-age_hours / 36.0)
        score = (impact / 2.0) * 100.0 * relevance * (0.7 + 0.3 * confirmed) * confidence * (0.8 + 0.2 * freshness)
        return EventSummary(
            score=max(-100.0, min(100.0, score)),
            relevance=relevance,
            confirmed=confirmed,
            event_type=self._choice(answers.get("event_type", {}), "market_commentary"),
            horizon=self._choice(answers.get("horizon", {}), "intraday"),
            confidence=max(0.0, min(1.0, confidence)),
            event_count=len(events),
            primary_event=primary_event,
        )


class TelegramNotifier:
    def __init__(self, http: HttpClient, bot_token: str, chat_id: str) -> None:
        self.http = http
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, message: str) -> None:
        for start in range(0, len(message), 3900):
            self.http.json(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                method="POST",
                body={"chat_id": self.chat_id, "text": message[start : start + 3900], "disable_web_page_preview": True},
            )
