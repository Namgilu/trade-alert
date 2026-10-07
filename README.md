# trade-alert data

이 브랜치는 `trade-alert` GitHub Actions가 장 마감 후 누적하는 테마 데이터 전용 브랜치입니다.

```text
state/theme-history.json            실행용 전체 상태
catalog/themes.json                 현재 테마·대표 종목 목록
memberships/YYYY-MM-DD.json         구성 종목 변경 이력
daily/YYYY/MM/YYYY-MM-DD.json       거래일별 스냅샷
```

API 키, 토큰, 뉴스 전문은 저장하지 않습니다. 파일은 16:10(Asia/Seoul) 적재 작업이 자동으로 관리합니다.
