from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import DailyReport, StockAnalysis, ThemeAnalysis
from .providers import JevSentimentProvider, NaverNewsProvider, NaverThemeProvider


class MorningAlertService:
    def __init__(
        self,
        themes: NaverThemeProvider,
        news: NaverNewsProvider,
        sentiment: JevSentimentProvider,
        *,
        theme_limit: int,
        stocks_per_theme: int,
        news_per_stock: int,
        news_lookback_hours: int,
    ) -> None:
        self.themes = themes
        self.news = news
        self.sentiment = sentiment
        self.theme_limit = theme_limit
        self.stocks_per_theme = stocks_per_theme
        self.news_per_stock = news_per_stock
        self.news_lookback_hours = news_lookback_hours

    def run(self, now: datetime | None = None) -> DailyReport:
        now = now or datetime.now(ZoneInfo("Asia/Seoul"))
        themes = self.themes.top_themes(self.theme_limit, self.stocks_per_theme)
        analyses: list[ThemeAnalysis] = []
        warnings: list[str] = []
        for theme in themes:
            stock_analyses: list[StockAnalysis] = []
            for stock in theme.stocks:
                articles = []
                try:
                    articles = self.news.recent(stock, self.news_per_stock, self.news_lookback_hours, now)
                    result = self.sentiment.analyze(stock, articles)
                    stock_analyses.append(StockAnalysis(stock, result, tuple(articles)))
                except Exception as exc:  # isolate one upstream/stock failure from the full report
                    error = f"{type(exc).__name__}: {exc}"
                    warnings.append(f"{stock.name}: {error}")
                    stock_analyses.append(StockAnalysis(stock, None, tuple(articles), error))
            analyses.append(ThemeAnalysis(theme, tuple(stock_analyses)))
        return DailyReport(now, tuple(analyses), tuple(warnings))


def format_report(report: DailyReport) -> str:
    lines = [f"📈 {report.generated_at:%Y-%m-%d} 아침 테마 브리핑", ""]
    if not report.themes:
        lines.append("조회된 테마가 없습니다.")
    for theme_rank, analysis in enumerate(report.themes, 1):
        rate = f" ({analysis.theme.change_rate:+.2f}%)" if analysis.theme.change_rate is not None else ""
        lines.append(f"{theme_rank}. {analysis.theme.name}{rate}")
        if not analysis.stocks:
            lines.append("  · 관련 종목을 찾지 못했습니다.")
        for stock_rank, stock_result in enumerate(analysis.stocks, 1):
            stock_rate = (
                f" {stock_result.stock.change_rate:+.2f}%" if stock_result.stock.change_rate is not None else ""
            )
            if stock_result.sentiment is None:
                status = "뉴스 없음" if not stock_result.error else "분석 실패"
                lines.append(f"  {stock_rank}) {stock_result.stock.name}({stock_result.stock.code}){stock_rate} · {status}")
                continue
            sentiment = stock_result.sentiment
            lines.append(
                f"  {stock_rank}) {stock_result.stock.name}({stock_result.stock.code}){stock_rate} · "
                f"{sentiment.outlook} | 긍정 {sentiment.positive:.0%} / "
                f"중립 {sentiment.neutral:.0%} / 부정 {sentiment.negative:.0%} "
                f"({sentiment.article_count}건)"
            )
        lines.append("")
    if report.warnings:
        lines.append(f"⚠️ 일부 분석 실패: {len(report.warnings)}건")
    lines.extend(["※ 뉴스 기반 참고 지표이며 투자 권유가 아닙니다."])
    return "\n".join(lines).strip()
