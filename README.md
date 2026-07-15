# 통계연보 검수 보조 PoC

통계연보 원문을 구조화하고 합계·비율·언어·메타정보를 검수하는 담당자용 웹앱입니다.

## 구조

```text
backend/
  app/
    api/          FastAPI 라우터
    db/           SQLite 스키마와 로컬 검수 DB
    ingest/       HWPX·Markdown 가져오기
    models/       Pydantic 응답 스키마
    services/     연보 데이터 조회/요약 서비스
    validation/   검수 프로파일·규칙·언어 검수
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

## 언어 검수

- `오탈자 검수`: 명백한 국문·영문 철자 오류, 문자 깨짐, 숫자 구분기호 오류와 연관 셀 대비 표기 불일치를 찾습니다.
- `용어 제안`: 의미는 통하지만 공식 명칭, 사전 의미 또는 공공 통계 문체에 더 적합한 표현을 제안합니다.
- `번역 검수`: 한국어와 영어의 의미 대응, 번역 누락 및 공식 고유명사 표기를 확인합니다.

제목, 대제목, 하위표 제목, 메타정보, 헤더, 일반 셀을 구분하지 않고 국문 또는 영문이 한 글자라도 있으면 같은 위치에 위 세 후보를 모두 생성합니다. 숫자와 문자가 섞인 셀도 제외하지 않으며, 긴 내용은 버리지 않고 여러 조각으로 나눠 전부 검수합니다.

실행 순서는 `공식 사전 정확 일치 → 동일 문맥의 이전 LLM 결과 캐시 → LLM`입니다. `approved` 또는 `official_verified` 사전과 원문 전체가 정확히 일치하면 API 없이 결과를 확정합니다. 국문명만 확인된 `official_name_only`, 부분 일치, 사전 충돌 및 새로운 표현은 생략하지 않고 LLM으로 보냅니다. 같은 원문이라도 제목·헤더·셀 위치, 표 제목, 행·열 문맥, 사전·프롬프트·모델 버전이 다르면 캐시를 재사용하지 않습니다.

한 위치의 오탈자·용어·번역 후보는 API 입력에서 문맥을 한 번만 전송하고 세 검수 결과를 각각 받습니다. 파란색 구간에 한글과 영어가 함께 있으면 오탈자·용어·번역을 각각 검수하고, 한글만 있으면 문맥에 맞는 영어 번역을 검수값으로 생성합니다. 파란색 표기는 임시 `담당자 확인` 오류를 만들지 않으며 실제 사전 또는 LLM 판단이 끝난 뒤 교정값과 근거를 바로 출력합니다.

`--with-llm`을 지정한 전수 검수는 모든 언어 후보가 `reviewed`가 되어야 성공합니다. API 응답 누락, 비활성 설정, 키 누락 또는 일부 후보가 남은 경우 명령이 실패하므로 부분 결과를 완료로 오인하지 않습니다.

### 고유명사·표준어 사전

사전은 `기관명`, `행정구역명`, `행사·사업명`, `통계 표준어`, `일반 용어`로 나누고 세부 분류와 출처 URL, 확인일, 별칭을 함께 저장합니다.

- 기관명: 중앙부처, 위원회, 공공기관, 산하기관
- 행정구역명: 시도, 시군구, 읍면동
- 행사·사업명: 박람회, 기념일, 정책사업, 제도명
- 통계 표준어: 합계, 소계, 누계, 비율, 증감률, 평균 등
- 일반 용어: 연보에 반복되는 국문·영문 항목명

근거 수준도 분리합니다.

- `approved`: 검수 정책에 따라 수동 확정한 표현
- `official_verified`: 공식 출처에서 국문과 영문을 함께 확인한 표현
- `official_name_only`: ALIO 등 공식 명부에서 국문 고유명사만 확인한 표현. 영문 번역의 정답으로 사용하지 않음
- `llm_reviewed`: LLM 검수 결과에서 생성한 참고 표현. 동일 문맥의 자동 재사용은 별도 검수 캐시가 담당함
- `reference`: 이전 연보 병기나 공식 문맥을 바탕으로 한 참고 표현
- `seed`: 코드에 포함된 낮은 우선순위의 초기 참고 표현

기본 공식 자료는 국가법령정보센터 중앙행정기관 영문명, JOB-ALIO 2026 공공기관 지정현황, 국립국어원 공공용어 번역 정보, 국토지리정보원 시계열행정구역 자료, 정부·지자체 공식 누리집입니다. `backend/app/data/official_translation_glossary.json`과 `backend/app/data/official_entity_names.json`에 출처를 기록합니다.

일반 검수는 API 토큰을 사용하지 않으며 언어 검수 후보를 생성한 뒤 공식 사전과 기존 캐시 결과를 즉시 적용합니다. 남은 후보만 LLM 대기로 유지됩니다.

```bash
PYTHONPATH=backend .venv/bin/python -m app.validation.run_validations
```

LLM 검수는 `.env`에 키를 설정하고 명시적으로 옵션을 켠 경우에만 실행됩니다. 설정 예시는 `.env.example`을 참고합니다.

```bash
PYTHONPATH=backend .venv/bin/python -m app.validation.run_validations --with-llm
```

최초 검수에서 사전과 캐시에 없는 후보는 API 토큰을 사용합니다. 같은 연보의 반복 실행과 다음 연도의 동일 문맥은 저장된 결과를 재사용하므로 이후 호출량이 줄어듭니다. 일부 시험 실행은 아래처럼 별도 LLM 명령에 `--limit`을 주어 실행합니다. 제한 실행은 일부 후보만 `reviewed`로 바꾸며 전수 검수 완료로 표시되지 않습니다.

```bash
PYTHONPATH=backend .venv/bin/python -m app.validation.llm_translation_review --run-id RUN_ID --limit 30
```
