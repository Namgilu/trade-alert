const STAGES = [
  { mode: "premarket", time: "07:30", title: "장전 후보", detail: "3개월 조정 패턴 + 뉴스·공시" },
  { mode: "preopen", time: "08:55", title: "장전 중간확정", detail: "예상체결가 + 호가 잔량" },
  { mode: "confirmation", time: "09:10", title: "장초 최종확인", detail: "실제 거래대금 + 확산도" },
];

const DATA_ROOT = "https://raw.githubusercontent.com/Namgilu/trade-alert/data";
const INDEX_PATH = "reports/index.json";
const REPORT_PATH_PATTERN = /^reports\/\d{4}-\d{2}-\d{2}\/(premarket|preopen|confirmation)\.json$/;

const state = {
  index: [],
  reports: [],
  selectedDate: null,
  collapsedStages: new Set(STAGES.map((stage) => stage.mode)),
};
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
  const bodyId = `stage-body-${stage.mode}`;
  const collapsed = state.collapsedStages.has(stage.mode);
  const header = node("button", "stage-header");
  header.type = "button";
  header.setAttribute("aria-expanded", String(!collapsed));
  header.setAttribute("aria-controls", bodyId);
  header.setAttribute("aria-label", `${stage.title} ${collapsed ? "펼치기" : "접기"}`);
  const generated = report ? new Date(report.generated_at).toLocaleTimeString("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Seoul",
  }) : null;
  const title = node("div", "stage-title");
  title.append(
    node("b", "", stage.title),
    node("small", report ? "snapshot-time" : "", report ? `${stage.detail} · ${generated} 기준 스냅샷` : stage.detail),
  );
  const toggleControl = node("span", "stage-toggle-control");
  const toggleLabel = node("span", "stage-toggle-label", collapsed ? "눌러서 내용 보기" : "내용 접기");
  const toggleIcon = node("span", "stage-toggle-icon", collapsed ? "+" : "−");
  toggleControl.append(toggleLabel, toggleIcon);
  header.append(
    node("time", "stage-time", stage.time),
    title,
    node("span", "status-pill", report ? "실시간 아님" : "결과 대기"),
    toggleControl,
  );
  section.append(header);
  const body = node("div", "stage-body");
  body.id = bodyId;
  body.hidden = collapsed;
  section.classList.toggle("is-collapsed", collapsed);
  header.addEventListener("click", () => {
    const shouldCollapse = !body.hidden;
    body.hidden = shouldCollapse;
    section.classList.toggle("is-collapsed", shouldCollapse);
    header.setAttribute("aria-expanded", String(!shouldCollapse));
    header.setAttribute("aria-label", `${stage.title} ${shouldCollapse ? "펼치기" : "접기"}`);
    toggleLabel.textContent = shouldCollapse ? "눌러서 내용 보기" : "내용 접기";
    toggleIcon.textContent = shouldCollapse ? "+" : "−";
    if (shouldCollapse) state.collapsedStages.add(stage.mode);
    else state.collapsedStages.delete(stage.mode);
  });
  if (!report) {
    body.append(node("div", "stage-empty", "아직 저장된 결과가 없습니다. 워크플로 실행이 완료되면 자동으로 표시됩니다."));
    section.append(body);
    return section;
  }
  if (!report.themes || report.themes.length === 0) {
    body.append(node("div", "stage-empty", "조건을 통과한 테마가 없습니다."));
  } else {
    const grid = node("div", "theme-grid");
    report.themes.forEach((theme, index) => grid.append(renderTheme(theme, index + 1, stage.mode)));
    body.append(grid);
  }
  if (report.warning_count) {
    body.append(node("div", "stage-warning", `일부 데이터 처리 실패 ${report.warning_count}건`));
  }
  section.append(body);
  return section;
}

function renderReports() {
  reportsRoot.replaceChildren();
  STAGES.forEach((stage) => {
    reportsRoot.append(renderStage(stage, state.reports.find((report) => report.mode === stage.mode)));
  });
}

function updateDates() {
  const dates = [...new Set(state.index.map((report) => report.market_date))].sort().reverse();
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
  status.lastChild.textContent = " 외부 예약 호출 설정됨";
  root.replaceChildren();
  STAGES.forEach((item) => {
    const row = node("div", "schedule-item");
    row.append(node("time", "", item.time), node("b", "", item.title), node("i"));
    root.append(row);
  });
}

function dataUrl(path, version) {
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  return `${DATA_ROOT}/${encodedPath}?v=${encodeURIComponent(version)}`;
}

async function fetchJson(path, version) {
  const response = await fetch(dataUrl(path, version), { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function validIndexEntry(entry) {
  return entry
    && typeof entry.market_date === "string"
    && typeof entry.mode === "string"
    && typeof entry.generated_at === "string"
    && typeof entry.path === "string"
    && REPORT_PATH_PATTERN.test(entry.path);
}

async function loadSelectedDate(force = false) {
  if (!state.selectedDate) {
    state.reports = [];
    renderReports();
    return;
  }
  const entries = state.index.filter((entry) => entry.market_date === state.selectedDate);
  const settled = await Promise.allSettled(
    entries.map((entry) => fetchJson(entry.path, force ? Date.now() : entry.generated_at)),
  );
  state.reports = settled
    .filter((result) => result.status === "fulfilled")
    .map((result) => result.value)
    .filter((report) => report && report.market_date === state.selectedDate);
  renderReports();
  const failures = settled.filter((result) => result.status === "rejected").length;
  if (failures) {
    notice.textContent = `일부 결과 파일을 불러오지 못했습니다. 잠시 후 새로고침해 주세요. (${failures}건)`;
    notice.hidden = false;
  }
}

async function loadReports(force = false) {
  refreshButton.disabled = true;
  notice.hidden = true;
  try {
    const payload = await fetchJson(INDEX_PATH, Date.now());
    if (!payload || payload.version !== 1 || !Array.isArray(payload.reports)) {
      throw new Error("결과 인덱스 형식이 올바르지 않습니다.");
    }
    state.index = payload.reports.filter(validIndexEntry);
    updateDates();
    if (!state.index.length) {
      notice.textContent = "data 브랜치에 저장된 분석 결과가 아직 없습니다.";
      notice.hidden = false;
    }
    await loadSelectedDate(force);
  } catch (error) {
    state.index = [];
    state.reports = [];
    updateDates();
    reportsRoot.replaceChildren();
    notice.textContent = error.message === "HTTP 404"
      ? "결과 인덱스가 아직 없습니다. 다음 분석 워크플로가 완료되면 자동으로 생성됩니다."
      : "GitHub data 브랜치에서 결과를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
    notice.hidden = false;
  } finally {
    refreshButton.disabled = false;
  }
}

dateSelect.addEventListener("change", async () => {
  state.selectedDate = dateSelect.value;
  notice.hidden = true;
  refreshButton.disabled = true;
  try {
    await loadSelectedDate();
  } finally {
    refreshButton.disabled = false;
  }
});
refreshButton.addEventListener("click", () => loadReports(true));
loadSchedules();
loadReports();
