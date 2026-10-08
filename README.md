# 국장 테마 3단계 알림

국내 주식의 뉴스·공시 이벤트와 실제 장 초반 수급을 분리해 분석하고 텔레그램으로 전송합니다.

분석 결과는 텔레그램 전송과 동시에 `data` 브랜치의 날짜별 JSON으로 보관합니다. 함께 제공되는 서버가 평일 07:30, 08:55, 09:10(한국시간)에 각 GitHub Actions를 호출하고, 브라우저용 대시보드와 조회 API를 제공합니다.

- **07:30 장전 후보:** 최근 3개월간 강하게 상승한 뒤 고점 대비 조정·횡보 중인 테마를 찾고 뉴스·공시를 검증합니다.
- **08:55 장전 중간확정:** 동시호가 예상체결가·예상거래량·매수/매도 잔량으로 후보를 걸러냅니다.
- **09:10 장초 최종확인:** 실제 테마 확산도, 거래대금, 전일 대비 거래량, 가격 강도로 다시 평가합니다.

> 현재 점수 가중치는 운영 초기 휴리스틱입니다. 실제 매매 신호로 사용하려면 결과와 이후 수익률을 저장해 워크포워드 백테스트로 보정해야 합니다.

## 분석 방식

### 뉴스·공시 이벤트

네이버 뉴스와 선택적으로 OpenDART 공시를 합치고 유사 제목을 제거합니다. JEV는 종목별 자료 묶음을 한 번에 평가합니다.

- 해당 국내 종목과 직접 관련 있는지
- 가장 중요한 신규 이벤트가 무엇인지
- 실적, 수주, 정책, 제품, 자금조달, 경영권, 법률 위험 중 어떤 유형인지
- 다음 국내 정규장에 미칠 영향이 `강한 악재 ~ 강한 호재` 중 어디인지
- 영향 기간이 시초가, 당일, 단기, 중장기 중 어디인지
- 공식 공시 또는 확정된 사실인지

일반적인 문장 감성 대신 경제적 효과를 판단합니다. 예를 들어 긍정적인 문체의 유상증자 기사도 주주가치 희석 가능성을 반영해 악재로 분류할 수 있습니다.

### 07:30 3개월 조정 후보 점수

최초 실행에서는 전체 테마의 시가총액 상위 대표 종목 3개로 최근 65거래일 가격·거래대금 시계열을 합성합니다. 이후에는 장 마감 후 네이버 테마 거래대금 스냅샷을 하루 한 번 누적하고 저장된 시계열로 선별합니다. 아래 조건을 모두 통과한 테마만 점수화합니다.

- 3개월 구간 고점 상승률 20% 이상
- 현재가가 구간 고점 대비 5~30% 조정
- 최근 15일 고가·저가 변동폭 20% 이하
- 테마 가격지수가 60일 이동평균선 위
- 과거 거래대금이 직전 20일 중앙값 대비 1.8배 이상 증가
- 거래대금 폭발 후 10~45거래일 경과
- 최근 거래대금이 폭발 당시의 80% 이하로 감소

```text
정량 패턴: 상승 강도 20% + 조정 적정성 20% + 변동성 축소 15%
           + 하락일 거래대금 감소 10% + 60일선 유지 10%
           + 과거 거래대금 폭발 15% + 현재 거래대금 냉각 10%

07:30 테마: 정량 패턴 85% + 뉴스·공시 이벤트 15%

종목: 뉴스·공시 이벤트 55% + 테마 강도 25% + 전일 가격 강도 20%
```

JEV/LLM은 시계열 패턴을 고르는 데 사용하지 않습니다. 정량 스크리너가 고른 후보에 한해 재료의 직접성·지속성과 구조적 악재를 평가하며 테마 점수의 15%만 반영합니다.

### 08:55 장전 중간확정 점수

한국투자증권 Open API의 예상체결가·예상체결수량과 KRX 호가 잔량 조회를 사용합니다. 08:55 값은 09:00 시가가 아니라 동시호가 중간값이므로 `장전 유효`, `장전 주의`, `장전 제외`로만 표시합니다.

```text
테마: 3개월 정량 패턴 30% + 상승 예상 종목 비율 20%
      + 평균 예상 등락 15% + 상대 예상 거래대금 10%
      + 매수/매도 잔량 10% + 뉴스·공시 이벤트 10%
      + 전일 테마 강도 5%

종목: 예상 등락 30% + 상대 예상 거래대금 20% + 매수/매도 잔량 20%
      + 뉴스·공시 이벤트 15% + 테마 강도 15%
```

### 09:10 장초 최종확인 점수

```text
테마: 3개월 정량 패턴 30% + 당일 확산도 20% + 당일 등락 15%
      + 거래대금 15% + 대장주 강도 10% + 뉴스·공시 이벤트 10%

종목: 거래대금 30% + 가격 강도 20% + 전일 대비 거래량 20%
      + 뉴스·공시 이벤트 15% + 테마 강도 15%
```

09:10 결과는 `거래 확인`, `관심 유지`, `관망`, `신호 무효`로 표시합니다. 이는 자동 주문이 아니라 관찰 우선순위입니다.

## 로컬 실행

Python 3.11 이상이 필요합니다.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

`.env`를 채우고 환경변수로 로드합니다.

```bash
set -a
source .env
set +a

trade-alert --mode premarket --dry-run
trade-alert --mode preopen --dry-run
trade-alert --mode confirmation --dry-run
trade-alert --mode collect --history-file .cache/theme-history.json
trade-alert --mode collect --history-repository /path/to/data-branch-checkout
```

`--dry-run`을 제거하면 텔레그램으로 전송합니다.

## 필요한 값

| 이름 | 필수 | 설명 |
|---|---:|---|
| `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` | 예 | 네이버 검색 API 키 |
| `JEV_API_KEY` | 예 | JEV API 키 |
| `JEV_API_URL`, `JEV_MODEL` | 공급자별 | System One URL과 모델 ID |
| `DART_API_KEY` | 아니요 | OpenDART 공시 키. 설정하면 공시를 뉴스보다 우선 반영 |
| `KIS_APP_KEY`, `KIS_APP_SECRET` | 예 | 3개월 수정주가 일봉과 08:55 예상체결·호가 조회용 한국투자 Open API 키 |
| `KIS_BASE_URL` | 아니요 | 기본값은 한국투자 실전 서버 `https://openapi.koreainvestment.com:9443` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 전송 시 | 텔레그램 봇과 수신 대상 |

## 자동 실행

GitHub Actions는 세 번의 알림을 독립된 `workflow_dispatch` 워크플로로 실행하고, 데이터 적재는 별도 워크플로로 관리합니다. 07:30 후보는 한국 날짜별 Actions 캐시에 저장하고 08:55와 09:10이 재사용합니다. 첫날 중복 초기화를 막기 위한 07:30 상태도 16:10까지 당일 캐시로 전달합니다. 장기 테마 이력은 휘발될 수 있는 Actions 캐시 대신 저장소의 `data` 브랜치에 누적합니다.

- `premarket-alert.yml`: 07:30 Asia/Seoul 장전 후보
- `preopen-alert.yml`: 08:55 Asia/Seoul 동시호가 중간확정
- `confirmation-alert.yml`: 09:10 Asia/Seoul 개장 후 최종확인
- `theme-data-collect.yml`: 16:10 Asia/Seoul 당일 테마 거래대금·등락률·확산도 적재

세 알림 워크플로에는 GitHub cron을 두지 않았습니다. 정시성이 필요한 외부 스케줄러가 아래 워크플로의 `workflow_dispatch`를 순서대로 호출해야 합니다. 요청 본문은 모두 `{"ref":"main"}`이며 별도 입력은 없습니다.

```text
POST /repos/Namgilu/trade-alert/actions/workflows/premarket-alert.yml/dispatches
POST /repos/Namgilu/trade-alert/actions/workflows/preopen-alert.yml/dispatches
POST /repos/Namgilu/trade-alert/actions/workflows/confirmation-alert.yml/dispatches
```

GitHub CLI로 직접 실행할 때는 다음과 같습니다.

```bash
gh workflow run premarket-alert.yml --ref main
gh workflow run preopen-alert.yml --ref main
gh workflow run confirmation-alert.yml --ref main
```

호출 순서는 반드시 07:30 → 08:55 → 09:10으로 유지합니다. 16:10 데이터 적재는 시간 지연에 민감하지 않아 GitHub cron과 수동 `workflow_dispatch`를 함께 유지합니다.

서버 스케줄러를 켜면 위 세 호출은 서버가 담당하므로 별도 cron은 필요하지 않습니다. 서버의 시간대와 무관하게 `Asia/Seoul` 기준 평일에 실행됩니다. 스케줄러 서버를 여러 대 실행하면 중복 호출되므로 `SCHEDULER_ENABLED=true`인 인스턴스는 반드시 한 대만 운영합니다.

첫날 07:30 적재는 네이버 테마 목록·구성 종목 최대 101회와 한국투자 토큰·대표주 일봉 최대 301회로 총 402회가 발생하고, 16:10에는 전달받은 상태에 네이버 테마 목록 1회만 추가하므로 일일 최대 403회입니다. 이후 16:10 적재는 네이버 테마 목록 1회만 사용합니다. 정상 운영 시 한국투자 API는 08:55 예상체결 조회의 최대 16회만 발생합니다. 누적 데이터가 없거나 손상된 경우에는 3개월 초기 적재를 다시 수행해 자동 복구합니다.

`data` 브랜치는 다음 구조로 관리됩니다.

```text
state/theme-history.json            실행 시 바로 읽는 전체 상태
catalog/themes.json                 현재 테마·대표 종목 목록
memberships/YYYY-MM-DD.json         구성 종목이 바뀐 날짜의 스냅샷
daily/YYYY/MM/YYYY-MM-DD.json       거래일별 지수·거래대금 스냅샷
reports/YYYY-MM-DD/premarket.json   07:30 웹·API용 결과
reports/YYYY-MM-DD/preopen.json     08:55 웹·API용 결과
reports/YYYY-MM-DD/confirmation.json 09:10 웹·API용 결과
```

16:10 작업은 테마 이력을 `data` 브랜치에 커밋하며 변경이 없는 휴장일에는 커밋하지 않습니다. 저장소가 공개라면 이 데이터도 공개됩니다. API 키, 텔레그램 토큰, 뉴스 전문과 상세 오류 문구는 이 브랜치에 저장하지 않습니다. 조직 정책이 쓰기를 막는 경우에는 저장소 `Settings → Actions → General → Workflow permissions`에서 쓰기 권한을 허용해야 합니다.

세 알림 작업도 웹 결과 JSON만 `data` 브랜치에 커밋합니다. 네 작업은 같은 동시성 그룹을 사용해 브랜치 쓰기 충돌을 방지합니다. 저장소가 공개라면 종목·점수·뉴스 제목과 링크도 공개되지만 비밀값과 뉴스 본문은 저장하지 않습니다. 따라서 알림 및 데이터 적재 워크플로 모두 `contents: write` 권한이 필요합니다.

## 웹 서버와 스케줄러

서버는 다음 기능을 한 프로세스에서 제공합니다.

- 반응형 웹 대시보드: `/`
- 최신 결과 목록: `GET /api/reports`
- 특정 결과: `GET /api/reports/{YYYY-MM-DD}/{mode}`
- 스케줄 확인: `GET /api/schedules`
- 상태 확인: `GET /api/health`
- 선택적 수동 실행: `POST /api/dispatch/{mode}` (`X-Admin-Token` 필요)

로컬 실행은 다음과 같습니다.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[server]'
cp .env.server.example .env.server
set -a
source .env.server
set +a
trade-alert-web
```

브라우저에서 `http://localhost:8000`을 엽니다. Docker로 상시 실행하려면 `.env.server`를 채운 뒤 다음 명령을 사용합니다.

```bash
cp .env.server.example .env.server
docker compose up -d --build
```

`GITHUB_TOKEN`에는 대상 저장소의 Actions 쓰기 권한이 필요합니다. 비공개 저장소라면 웹 결과를 읽기 위한 Contents 읽기 권한도 추가합니다. 수동 실행 API가 필요 없으면 `WEB_ADMIN_TOKEN`은 비워 두면 됩니다. 방화벽이나 프록시에서는 대시보드용 `8000` 포트만 공개하고, HTTPS는 Caddy·Nginx 또는 배포 플랫폼에서 종료하는 구성을 권장합니다.

서버 자체는 네이버·JEV·한국투자·텔레그램 키를 사용하지 않습니다. 이 키들은 기존처럼 GitHub Actions의 `action-env`에만 두며, 서버는 GitHub 토큰만 보유합니다.

`action-env` 환경의 Actions secrets에 필수 키를 등록하십시오. 저장소 secrets를 사용해도 됩니다. `DART_API_KEY`만 선택 사항입니다. Actions 화면의 수동 실행에서는 세 단계 중 하나를 선택할 수 있습니다.

GitHub 저장소에서 `Settings → Environments → action-env → Environment secrets`로 이동해 아래 값을 등록합니다.

- `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`
- `JEV_API_KEY`
- `KIS_APP_KEY`, `KIS_APP_SECRET`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- 선택: `DART_API_KEY`

한국투자증권 계좌 개설과 Open API 서비스 신청 후 앱 키·앱 시크릿을 발급받아 등록합니다. 실전 키는 기본 주소를 그대로 사용합니다. 모의투자를 사용할 경우 같은 화면의 `Environment variables`에 `KIS_BASE_URL=https://openapivts.koreainvestment.com:29443`을 추가하고 모의투자용 키를 사용해야 합니다. 비밀값은 저장소 파일이나 Actions variable이 아니라 secret에 넣습니다.

## 테스트

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## 운영 참고

- 뉴스 검색은 네이버 공식 Search API를 사용합니다.
- 공시는 금융감독원 OpenDART API를 사용합니다.
- KRX 유료 데이터는 사용하지 않습니다. 최초 3개월 일봉은 한국투자 `FHKST03010100`의 수정주가를 사용하고, 이후 테마 거래대금은 네이버 테마 스냅샷으로 누적합니다.
- 최초 대표주 거래대금 합계는 네이버의 최신 테마 전체 거래대금에 맞춰 비율 보정합니다. 같은 대표 종목이 여러 테마에 겹치면 조회 결과를 재사용합니다.
- 테마 이력은 날짜별로 계속 누적하고 GitHub Actions 캐시로 다음 실행에 전달합니다. 선별 계산에는 가장 최근 65거래일을 사용합니다.
- 07:30 후보 파일은 당일에만 유효합니다. 09:10에는 후보 선정은 재사용하되 네이버 테마 등락률·확산도·거래대금은 다시 읽어 장초 점수에 반영합니다.
- 08:55에는 한국투자 `FHKST01010200`의 예상체결가·수량과 총매수·총매도 잔량을 결합합니다. 예상체결 데이터가 한 종목도 없으면 잘못된 중간확정 알림 대신 실행을 실패시킵니다.
- 테마와 구성 종목은 네이버 증권 웹 화면의 미문서화된 읽기 전용 응답이므로 변경될 수 있습니다.
- 종목별 데이터 오류는 다른 종목 분석을 중단시키지 않고 텔레그램 하단에 실패 건수로 표시합니다.
- GitHub Actions 예약 실행은 혼잡 시 몇 분 지연될 수 있습니다.
- 평일 공휴일에도 작업은 실행되지만, 95% 이상의 테마 값이 직전 스냅샷과 같으면 휴장일로 간주해 중복 적재하지 않습니다.
