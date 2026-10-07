# 매일 아침 주식 테마 알림

매일 아침 국내 주식의 상승률 상위 테마 3개와 각 테마의 상위 종목 3개를 찾고, 최근 뉴스를 JEV로 분류해 텔레그램으로 전송합니다.

## 처리 흐름

1. 네이버 증권의 공개 읽기 전용 응답에서 상승률 상위 테마를 조회합니다.
2. 각 테마에서 상승률 상위 종목을 최대 3개 선택합니다.
3. 네이버 검색 API로 최근 24시간 뉴스를 수집합니다.
4. JEV `choice` 질문으로 각 뉴스를 `positive / neutral / negative`로 분류하고 확률 평균을 계산합니다.
5. 확률 차이로 `강한 긍정 / 긍정 / 중립 / 부정 / 강한 부정` 전망을 만들고 텔레그램에 보냅니다.

> 이 결과는 뉴스 기반 참고 지표이며 매수·매도 신호가 아닙니다.

## 로컬 실행

Python 3.11 이상이 필요합니다.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

`.env` 값을 채운 뒤 셸에 로드합니다.

```bash
set -a
source .env
set +a
trade-alert --dry-run
trade-alert
```

`--dry-run`은 텔레그램 키 없이 보고서를 표준 출력으로만 보여줍니다. 네이버 검색 API 키와 JEV 키는 필요합니다.

## 필요한 값

| 이름 | 설명 |
|---|---|
| `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` | 네이버 검색 API 애플리케이션 키 |
| `JEV_API_KEY` | JEV API 키 |
| `JEV_API_URL` | 사용하는 JEV 공급자의 System One URL |
| `JEV_MODEL` | JEV 모델 ID |
| `TELEGRAM_BOT_TOKEN` | BotFather가 발급한 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 메시지를 받을 사용자 또는 채널 ID |

JEV 공급자마다 URL과 모델 ID가 다를 수 있으므로 실제 계약 값을 `.env` 또는 GitHub Variables에 지정하십시오.

## 자동 실행

`.github/workflows/morning-alert.yml`은 매일 오전 7시 30분(Asia/Seoul)에 실행됩니다. 저장소의 Actions secrets에 키를 추가하십시오. 기본값과 다른 JEV 공급자를 사용한다면 URL과 모델을 Actions variables에 등록합니다.

## 테스트

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## 운영 참고

- 네이버 검색 API의 뉴스 검색은 공식 API입니다.
- 테마 순위는 네이버 증권 웹 화면의 미문서화된 읽기 전용 엔드포인트를 사용하므로 변경될 수 있습니다. 변경 시 `.env`의 `THEME_LIST_URL`, `THEME_STOCKS_URL_TEMPLATE`을 다른 공급자로 교체할 수 있습니다.
- 한 종목의 뉴스/JEV 분석이 실패해도 나머지 종목은 계속 처리되고 텔레그램 하단에 실패 건수가 표시됩니다.
- GitHub Actions 예약 실행은 혼잡 시 몇 분 지연될 수 있습니다.
