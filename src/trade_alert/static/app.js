const STAGES = [
  { mode: "premarket", time: "07:30", title: "장전 후보", detail: "3개월 조정 패턴 + 뉴스·공시" },
  { mode: "preopen", time: "08:55", title: "장전 중간확정", detail: "예상체결가 + 호가 잔량" },
  { mode: "confirmation", time: "09:10", title: "장초 최종확인", detail: "실제 거래대금 + 확산도" },
];

const state = { reports: [], selectedDate: null };
const reportsRoot = document.querySelector("#reports");
const dateSelect = document.querySelector("#market-date");
const notice = document.querySelector("#notice");
const refreshButton = document.querySelector("#refresh");

function node(tag, className, text) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined && text !== null) value.textContent = text;
  return value;
}

function number(value, digits = 1, suffix = "") {
  return value === null || value === undefined ? "-" : `${Number(value).toFixed(digits)}${suffix}`;
}

function signed(value) {
  if (value === null || value === undefined) return "-";
  const parsed = Number(value);
  return `${parsed >= 0 ? "+" : ""}${parsed.toFixed(2)}%`;
}

function amount(value) {
  if (value === null || value === undefined) return "-";
  return `${Math.round(Number(value) / 100000000).toLocaleString("ko-KR")}억`;
}

function tone(element, value) {
  if (Number(value) > 0) element.classList.add("positive");
  if (Number(value) < 0) element.classList.add("negative");
  return element;
}

function metric(label, value, rawValue) {
  const wrap = node("div", "metric");
  wrap.append(node("small", "", label), tone(node("b", "", value), rawValue));
  return wrap;
}

function validLink(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch (_) {
    return null;
  }
}

function renderEvent(stock) {
  const summary = stock.event_summary;
  if (!summary) return null;
  const wrap = node("div", "event");
  wrap.append(node("div", "event-summary", `${summary.direction} ${number(summary.score, 0)} · ${summary.event_type} · ${summary.horizon}`));
  if (summary.primary_event) {
    const href = validLink(summary.primary_event.url);
    const headline = node(href ? "a" : "div", "", summary.primary_event.title);
    if (href) {
      headline.href = href;
      headline.target = "_blank";
      headline.rel = "noopener noreferrer";
    }
    wrap.append(headline);
  }
  return wrap;
}

function renderStock(stock, mode) {
  const card = node("article", "stock");
  const head = node("div", "stock-head");
  const name = node("div", "stock-name");
  name.append(node("b", "", stock.name), node("small", "", stock.code));
  head.append(name, node("span", "stock-score", `${number(stock.score, 0)}점`));
  card.append(head, node("span", "signal", stock.signal));

  const details = node("div", "stock-details");
  if (mode === "preopen" && stock.preopen_quote) {
    details.append(
      tone(node("span", "", `예상 ${signed(stock.preopen_quote.expected_change_rate)}`), stock.preopen_quote.expected_change_rate),
      node("span", "", `체결가 ${Number(stock.preopen_quote.expected_price || 0).toLocaleString("ko-KR")}원`),
      node("span", "", `잔량비 ${number(stock.preopen_quote.bid_ask_ratio, 2)}배`),
    );
  } else {
    details.append(tone(node("span", "", `등락 ${signed(stock.change_rate)}`), stock.change_rate));
    if (mode === "confirmation") {
      details.append(node("span", "", `거래대금 ${amount(stock.trade_amount)}`), node("span", "", `거래량 ${number(stock.relative_volume, 2)}배`));
    }
  }
  card.append(details);
  const event = renderEvent(stock);
  if (event) card.append(event);
  return card;
}

function renderTheme(theme, rank, mode) {
  const card = node("article", "theme-card");
  card.append(node("div", "theme-rank", `RANK ${String(rank).padStart(2, "0")}`));
  const nameRow = node("div", "theme-name-row");
  nameRow.append(node("div", "theme-name", theme.name), node("div", "theme-score", `${number(theme.score, 0)} / 100`));
  card.append(nameRow);

  const metrics = node("div", "metric-grid");
  metrics.append(metric("테마 등락", signed(theme.change_rate), theme.change_rate));
  metrics.append(metric("확산도", theme.breadth == null ? "-" : `${Math.round(theme.breadth * 100)}%`, theme.breadth));
  if (theme.pattern) {
    metrics.append(metric("3개월 고점상승", signed(theme.pattern.peak_return), theme.pattern.peak_return));
    metrics.append(metric("고점 대비", signed(theme.pattern.drawdown), theme.pattern.drawdown));
    metrics.append(metric("거래대금 폭발", `${number(theme.pattern.turnover_spike_ratio, 1)}배`, theme.pattern.turnover_spike_ratio));
    metrics.append(metric("폭발 후", `${number(theme.pattern.days_since_turnover_spike, 0)}일`, theme.pattern.days_since_turnover_spike));
  }
  card.append(metrics);

  const stocks = node("div", "stocks");
  (theme.stocks || []).forEach((stock) => stocks.append(renderStock(stock, mode)));
  card.append(stocks);
  return card;
}

function renderStage(stage, report) {
  const section = node("section", "stage");
  const header = node("header", "stage-header");
  const generated = report ? new Date(report.generated_at).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" }) : null;
  const title = node("div", "stage-title");
  title.append(node("b", "", stage.title), node("small", "", report ? `${stage.detail} · 생성 ${generated}` : stage.detail));
  header.append(node("time", "stage-time", stage.time), title, node("span", "status-pill", report ? "분석 완료" : "결과 대기"));
  section.append(header);
  if (!report) {
    section.append(node("div", "stage-empty", "아직 저장된 결과가 없습니다. 워크플로 실행이 완료되면 자동으로 표시됩니다."));
    return section;
  }
  if (!report.themes || report.themes.length === 0) {
    section.append(node("div", "stage-empty", "조건을 통과한 테마가 없습니다."));
  } else {
    const grid = node("div", "theme-grid");
    report.themes.forEach((theme, index) => grid.append(renderTheme(theme, index + 1, stage.mode)));
    section.append(grid);
  }
  if (report.warning_count) {
    section.append(node("div", "stage-warning", `일부 데이터 처리 실패 ${report.warning_count}건`));
  }
  return section;
}

function renderReports() {
  reportsRoot.replaceChildren();
  const dayReports = state.reports.filter((report) => report.market_date === state.selectedDate);
  STAGES.forEach((stage) => {
    reportsRoot.append(renderStage(stage, dayReports.find((report) => report.mode === stage.mode)));
  });
}

function updateDates() {
  const dates = [...new Set(state.reports.map((report) => report.market_date))].sort().reverse();
  dateSelect.replaceChildren();
  if (!dates.length) {
    dateSelect.append(new Option("저장된 결과 없음", ""));
    dateSelect.disabled = true;
    state.selectedDate = null;
    return;
  }
  dates.forEach((date, index) => dateSelect.append(new Option(`${date}${index === 0 ? " · 최신" : ""}`, date)));
  if (!dates.includes(state.selectedDate)) state.selectedDate = dates[0];
  dateSelect.value = state.selectedDate;
  dateSelect.disabled = false;
}

async function loadSchedules() {
  const root = document.querySelector("#schedule-list");
  const status = document.querySelector("#scheduler-state");
  try {
    const response = await fetch("/api/schedules");
    if (!response.ok) throw new Error();
    const payload = await response.json();
    status.lastChild.textContent = payload.enabled ? " 자동 분석 운영 중" : " 자동 분석 꺼짐";
    root.replaceChildren();
    payload.items.forEach((item) => {
      const row = node("div", "schedule-item");
      row.append(node("time", "", item.time), node("b", "", item.label), node("i"));
      root.append(row);
    });
  } catch (_) {
    status.lastChild.textContent = " 일정 확인 실패";
    root.replaceChildren(node("div", "stage-empty", "일정을 불러오지 못했습니다."));
  }
}

async function loadReports(force = false) {
  refreshButton.disabled = true;
  notice.hidden = true;
  try {
    const response = await fetch(`/api/reports?limit=30${force ? "&refresh=true" : ""}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "결과 조회에 실패했습니다.");
    state.reports = payload.reports || [];
    updateDates();
    if (!state.reports.length) {
      notice.textContent = "data 브랜치에 저장된 분석 결과가 아직 없습니다.";
      notice.hidden = false;
    }
    renderReports();
  } catch (error) {
    reportsRoot.replaceChildren();
    notice.textContent = error.message;
    notice.hidden = false;
  } finally {
    refreshButton.disabled = false;
  }
}

dateSelect.addEventListener("change", () => {
  state.selectedDate = dateSelect.value;
  renderReports();
});
refreshButton.addEventListener("click", () => loadReports(true));
loadSchedules();
loadReports();
