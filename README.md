# 통계연보 검수 보조 PoC

기존 통계연보 DB 시스템에서 내려받은 엑셀 데이터를 구조화·검수·시각화·재추출하는 담당자용 웹앱 초안입니다.

## 구조

```text
backend/
  app/
    api/          FastAPI 라우터
    data/         이미지 기반 더미 데이터
    models/       Pydantic 응답 스키마
    services/     연보 데이터 조회/요약 서비스
frontend/
  src/
    api/          API 클라이언트
    components/   화면 컴포넌트
    hooks/        데이터 로딩 훅
    styles/       전역 스타일
    utils/        포맷터
```

## 실행

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
.venv/bin/python -m uvicorn app.main:app --app-dir backend --reload
```

```bash
cd frontend
npm install
npm run dev
```

프론트엔드는 기본적으로 `http://127.0.0.1:5173`, 백엔드는 `http://127.0.0.1:8000`에서 실행됩니다.

## 다음 교체 지점

- 실제 엑셀 파싱: `backend/app/data/dummy_report.py`를 파서 출력으로 교체
- 검수 로직: `backend/app/services/report_service.py`에 합계·비율·이상치 규칙 추가
- 저장소 연결: `ReportService`의 데이터 소스를 DB 리포지토리로 교체
