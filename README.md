# 통계연보 검수 보조

통계연보 파일을 구조화하고 합계·비율·오탈자·번역·메타정보 등을 검수하는
React + FastAPI 웹앱입니다. 업로드한 파일의 구조화 데이터와 검수 결과는 선택된
DB에 저장되며, 검수 작업은 연보 ID에 고정된 단계형 워크플로로 실행됩니다.
로컬은 SQLite를 사용하고, Railway는 같은 저장소 계층을 통해 PostgreSQL을
사용합니다.

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

- 프런트엔드: `http://127.0.0.1:5173`
- 백엔드 API: `http://127.0.0.1:8000`
- API 문서: `http://127.0.0.1:8000/docs`

## 자동 처리

웹 화면의 `연보 업로드` 버튼은 `.xlsx`, `.hwpx`, `.md`를 받습니다. 업로드 후
다음 단계를 순서대로 수행합니다.

1. 파일을 형식별 파서로 구조화합니다.
2. 한 트랜잭션에서 연보, 통계표, 셀을 DB에 저장합니다.
3. 새 표 또는 구조가 바뀐 표의 검수 프로파일을 생성합니다.
4. 프로파일에 저장된 합계·비율·메타정보·이상치 규칙을 실행합니다.
5. 오탈자·번역 및 HWPX 파란색 표기 후보를 구성합니다.
6. 승인 사전과 이전 검수 캐시를 먼저 적용합니다.
7. 명시적으로 활성화한 경우 남은 후보만 LLM으로 검수합니다.
8. 검수 결과와 연산 관계를 UI 하이라이트 좌표로 변환합니다.

작업 상태는 `report_processing_jobs`와 `report_processing_stages`에 저장됩니다.
실패 시 성공한 앞 단계는 재사용하고 실패한 단계부터 재시도할 수 있습니다.
자세한 내부 흐름은 [자동 검수 아키텍처](docs/automated-validation-workflow.md)를
참고하세요.

### 업로드 API

브라우저는 파일을 raw body로 전송하고 원본 파일명은 쿼리에 넣습니다.

```bash
curl -X POST \
  --data-binary @2027_통계연보.xlsx \
  "http://127.0.0.1:8000/api/imports?filename=2027_통계연보.xlsx&year=2027"
```

```bash
curl http://127.0.0.1:8000/api/imports/1
curl -X POST http://127.0.0.1:8000/api/imports/1/retry
```

LLM 전수 검수는 비용이 발생하므로 업로드 요청의 `include_llm=true` 또는
`AUTO_IMPORT_INCLUDE_LLM=1`로 명시적으로 켭니다. 후자는 웹 화면의 모든 신규
업로드에 자동 적용됩니다. 새 표의 프로파일을 LLM으로 보강하려면 환경 변수
`PROFILE_LLM_ENABLED=1`도 설정합니다. 이 옵션들을 끄더라도 계산 규칙, 승인 사전
및 캐시 기반 검수는 수행됩니다.

## 검수 실행

최신 연보를 규칙과 사전만으로 검수합니다.

```bash
PYTHONPATH=backend .venv/bin/python -m app.validation.run_validations
```

특정 연보를 고정해 검수하거나 LLM을 포함할 수 있습니다.

```bash
PYTHONPATH=backend .venv/bin/python -m app.validation.run_validations \
  --report-id 2 --refresh-profiles --with-llm
```

## LLM 설정

키는 저장소의 `.env`에만 두고 커밋하지 않습니다. 예시는 `.env.example`에
있습니다. `LLM_PROVIDER=auto`는 BizRouter 키가 있으면 BizRouter를 우선하고,
없으면 OpenAI를 사용합니다.

```dotenv
LLM_PROVIDER=bizrouter
LLM_REVIEW_ENABLED=1
BIZROUTER_API_KEY=발급받은_키
BIZROUTER_MODEL=openai/gpt-5-mini

# 새 표 또는 구조 변경 표의 계산 프로파일 보강
PROFILE_LLM_ENABLED=1
PROFILE_LLM_MODEL=gpt-5-mini
```

언어 검수는 `공식 사전 정확 일치 → 동일 문맥 캐시 → LLM` 순서입니다. 한 셀의
오탈자와 번역은 하나의 요청 문맥으로 묶습니다. 용어 제안은 현재 검수 범위에서
제외되어 있습니다. 메타정보는 존재 여부만 계산 규칙으로 확인하며, 주석·출처
본문을 언어 검수 대상으로 보내지 않습니다.

## 배포

현재 배포 기준은 `Vercel 프론트엔드 + Railway 백엔드 + Railway Postgres`입니다.
로컬 개발은 계속 SQLite를 사용하고, Railway 환경에 `DATABASE_URL`이 있으면
백엔드는 자동으로 Postgres를 사용합니다.

### Railway 백엔드

Railway에서 GitHub 저장소를 연결한 뒤 백엔드 서비스를 만듭니다. 저장소 루트의
`railway.json`은 루트 `Dockerfile`로 FastAPI 서버를 빌드합니다.

필수 환경 변수:

```dotenv
DATABASE_URL=${{Postgres.DATABASE_URL}}
DATABASE_BACKEND=postgres
UPLOAD_DIR=/data/uploads
ALLOWED_ORIGINS=https://your-vercel-project.vercel.app
AUTO_IMPORT_INCLUDE_LLM=0
PROFILE_LLM_ENABLED=0
```

LLM 검수를 운영에서 자동 실행하려면 아래처럼 명시적으로 켭니다.

```dotenv
LLM_PROVIDER=bizrouter
LLM_REVIEW_ENABLED=1
BIZROUTER_API_KEY=발급받은_키
BIZROUTER_MODEL=openai/gpt-5-mini
AUTO_IMPORT_INCLUDE_LLM=1
```

업로드 원본 파일을 재검수에도 보존하려면 Railway Volume을 `/data`에 연결합니다.
볼륨이 없어도 업로드 직후 DB 저장과 검수는 가능하지만, 재배포 뒤 원본 파일 경로는
유지되지 않을 수 있습니다.

### Vercel 프론트엔드

Vercel에서 같은 GitHub 저장소를 연결하고 Root Directory를 `frontend`로
설정합니다. `frontend/vercel.json`이 Vite 빌드와 SPA 경로 처리를 담당합니다.

필수 환경 변수:

```dotenv
VITE_API_BASE_URL=https://your-railway-backend.up.railway.app
```

Vercel 배포 URL이 확정되면 Railway의 `ALLOWED_ORIGINS` 값을 그 URL로 갱신한 뒤
백엔드 서비스를 재배포합니다.

## 구조

```text
backend/app/
  api/                    FastAPI 엔드포인트
  core/                   설정, 공용 LLM 전송 계층
  db/                     DB 연결 선택, 공통 스키마, PostgreSQL 어댑터
  ingest/                 Excel/HWPX/Markdown 파서와 저장소
  validation/             프로파일, 규칙 엔진, 언어 검수 단계
  workflows/              업로드부터 검수 완료까지의 작업 오케스트레이션
  services/               조회 모델과 하이라이트 표시 변환
frontend/src/
  api/                    업로드·작업 조회·연보 조회 클라이언트
  components/             목록 및 상세 검수 화면
  utils/                  검수 그룹화·정렬·표시 정책
```

## 검증

```bash
cd backend
../.venv/bin/python -m unittest discover -s tests -q
```

```bash
cd frontend
npm run build
```
