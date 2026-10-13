# Journal Daily Digest

Nature · Science · Cell RSS에서 새 연구 논문을 모아 근거 텍스트(전문 > 초록)를 확보하고, Codex CLI(또는 Claude Code)로 한국어 요약을 만든 뒤 GitHub Pages 정적 사이트로 보여준다.

- 요약은 **로컬 서버**에서만 돈다(구독 인증, API 키 미사용). GitHub Actions는 `data/*.json`으로 HTML을 빌드·배포만 한다.
- repo에는 **메타데이터 + 요약 + 원문 링크만** 올라간다. 초록·전문·PDF는 `~/.local/share/paper-digest/`에만 저장된다.
- 서버 쪽은 표준 라이브러리 Python(3.11+), `pdftotext`(poppler-utils), AI 개요도 WebP 변환용 Pillow를 쓴다. Actions 빌드는 표준 라이브러리만 쓴다.

```
RSS ─fetch─▶ state.db ─enrich─▶ raw/<doi>/ (초록·전문·PDF, 로컬)
                    ▲                 │
         inbox/*.pdf┘                 ▼
                              summarize (codex / claude, 샌드박스)
                                      │
                              figures (Cell GA 링크 / codex AI 개요도)
                                      │
                         export ─▶ data/papers/YYYY-MM.json, data/figures/*.webp ─push─▶ Actions ─▶ Pages
```

## 파일

| 경로 | 내용 |
|---|---|
| `config/settings.toml` | 엔진·모델·effort, 연락 이메일, 재시도 일수, publish 설정 |
| `config/journals.toml` | 저널 피드와 연구 논문 판별 규칙(저널 추가는 여기에) |
| `config/interests.md` | 관심 분야 프로필(관련도 채점 기준) |
| `digest/` | 수집·근거 확보·요약·내보내기 코드 (`python3 -m digest …`) |
| `site/build.py` | `data/` → `_site/` 정적 HTML (Actions에서도 사용) |
| `data/` | 공개 JSON과 AI 개요도 WebP (자동 생성, 커밋 대상) |
| `deploy/install-timer.sh` | 매일 실행용 systemd user timer 설치 |
| `~/.local/share/paper-digest/` | `state.db`, `raw/`, `inbox/`, `logs/` (비공개) |

## 단계별 확인

모든 명령은 repo 루트에서 실행한다.

**1. 초기화** — 데이터 디렉터리와 DB 생성

```bash
python3 -m digest init
python3 -m digest status
```

**2. RSS 수집** — 두 번째 실행에서 신규 0건이면 중복 방지 정상

```bash
python3 -m digest fetch
python3 -m digest fetch
python3 -m digest list --limit 20
```

연구 논문 아님으로 걸러진 유형은 로그(`연구 논문 아님으로 판별된 유형`)에 저널별로 찍힌다. 빠진 유형이 있으면 `config/journals.toml`의 `include_types`에 추가.

**3. 근거 텍스트 확보** — 논문별로 `fulltext` / `abstract` / `awaiting_text`

```bash
python3 -m digest enrich --limit 10
python3 -m digest status
python3 -m digest show 10.1038/s41586-026-11066-6     # source_level, source_origin, source_path 확인
```

출처 순서: Europe PMC 전문(OA) → nature.com OA 전문 → PubMed 초록 → Crossref 초록 → nature.com 초록 → OpenAlex 초록. science.org·cell.com 페이지는 요청하지 않는다(403).
초록을 못 구한 논문은 매일 재시도하고, `abstract_wait_days`(기본 7일)가 지나면 `needs_pdf`가 된다.

**4. 요약**

```bash
python3 -m digest summarize --limit 2                               # settings.toml 기본(codex gpt-6.1-sol xhigh)
python3 -m digest summarize --limit 1 --engine codex --model gpt-6.1-sol --effort high
python3 -m digest summarize --limit 1 --engine claude --model opus  # claude 로그인 필요
python3 -m digest list --status summarized
python3 -m digest show <DOI>
python3 -m unittest tests.test_summarize tests.test_engines -v     # 한도 도달 시 중단·이월, 도구 사용 차단 검증
```

요약은 이 분야를 잘 모르는 사람이 읽어도 "무엇을 하려 했고 무엇을 주장하는지" 알 수 있게 쓴다. 항목은 다음과 같다.

| 항목 | 내용 |
|---|---|
| 제목 번역 | 전문 용어는 영어 그대로 |
| 한 줄 요약 | 논문의 주장을 한 문장으로 |
| 요약 | 4–6문장: 다루는 문제 → 무엇을 했는지 → 무엇을 발견·주장하는지 |
| 기존 연구와의 차별점 | 근거 텍스트가 직접 밝힌 비교("previously", "unlike", "first" 등)만. 없으면 "비교 없음"으로 표시 |
| 한계 | 저자가 밝힌 한계 + 텍스트에서 확인되는 연구 범위(생쥐 모델만, 세포주만 등). 없으면 "언급 없음" |
| 키워드·관련도 | 관심 분야 프로필 기준 |

전문 용어는 한국어로 옮기거나 괄호 병기하지 않고 영어 원어로 쓴다. 비전문가를 위해 용어의 일반적인 뜻을 짧게 풀어 주는 것만 허용하고, 논문 내용은 근거 텍스트에 있는 것만 쓴다.

- 사용량 한도 메시지를 받으면 즉시 멈추고 남은 논문은 `ready`로 남는다(실패 횟수에 포함 안 됨).
- 인증 만료 등 엔진 자체 문제도 멈추고 종료 코드 2를 돌려준다.
- 모델 과부하("at capacity")나 5xx 같은 서버 쪽 일시 오류는 1분, 5분 뒤 같은 논문을 다시 시도한다.
  - 그래도 안 되면 이번 실행을 멈추고 남은 논문을 다음 실행으로 넘긴다. 논문 실패 횟수에는 넣지 않는다.
- 그 밖의 실패는 논문별로 3번까지 재시도 후 `failed`.

**5. 공개 JSON과 사이트**

```bash
python3 -m digest export
python3 -m digest build
python3 -m http.server -d _site 8000     # http://localhost:8000
```

사이트는 블로그형이다.
- `index.html`: 최근 7개 날짜의 요약 논문을 포토카드 그리드(그림·관련도·제목·한 줄 요약)로 보여준다.
- `YYYY-MM-DD.html`: 그날 들어온 논문 카드와 요약 대기 목록.
- `paper-<DOI 슬러그>.html`: 카드를 누르면 열리는 상세 글(그림, 요약, 기존 연구와의 차별점, 한계, 원문 링크).
- `archive.html`: 날짜 목록.

카드 그림은 출판사 graphical abstract → AI 개요도 순으로 고르고, 둘 다 없으면 저널 색과 키워드로 만든 표지를 쓴다.

**6. PDF로 재요약** — 전문을 못 본 논문(근거: 초록)이나 `needs_pdf` 논문

```bash
# 원격에서
scp paper.pdf <서버>:~/.local/share/paper-digest/inbox/
# 또는 서버에서 직접
cp paper.pdf ~/.local/share/paper-digest/inbox/
python3 -m digest ingest-pdf             # (run 에도 포함됨)
python3 -m digest summarize --doi <DOI>  # 또는 다음 run 에서 자동 재요약
# DOI를 직접 지정해서 연결(원본 파일은 복사)
python3 -m digest add-pdf <DOI> paper.pdf
```

- 파일 이름은 아무거나 된다. DOI는 PDF 앞 2쪽에서 찾는다.
- 못 찾으면 `inbox/unmatched/`로 옮겨진다. 그때는 파일명을 DOI 슬러그(`10.1016_j.cell.2026.09.017.pdf`, `/`→`_`)로 바꿔 다시 넣는다.
- 스캔 이미지 PDF처럼 텍스트가 안 나오는 파일은 `inbox/failed/`로 옮겨진다.

**7. 개요 그림**

```bash
python3 -m digest figures --limit 0   # GA 확인만 (이미지 생성 없음)
python3 -m digest figures --limit 1   # AI 개요도 1장 생성 (codex, 약 1분)
python3 -m unittest tests.test_figures -v
```

- **Graphical abstract**: Cell처럼 `journals.toml`에 `graphical_abstract = "elsevier"`가 있는 저널은 RSS 링크의 PII로 Elsevier 이미지 서버(`ars.els-cdn.com`)에 GA가 있는지 확인한다.
  - 있으면 이미지 URL만 기록한다. 사이트는 출판사 서버에서 바로 불러와 표시하고, repo에는 복사하지 않는다.
  - GA가 있는 논문은 AI 그림을 만들지 않는다. Nature·Science에는 GA가 없다.
- **AI 개요도**: GA가 없고 관련도가 `figures.ai_min_relevance`(기본 7) 이상인 논문만 대상이다. 엔진이 codex일 때만 만든다.
  - codex 내장 이미지 생성(`image_gen`, gpt-image-2)에 **검증된 요약**(제목·한 줄 요약·요약)만 넘긴다. 레이블은 요약에 나온 용어만 쓰고 수치·그래프는 넣지 않게 지시한다.
  - 결과는 `data/figures/<doi>.webp`(가로 최대 1600px, 장당 100KB 안팎)로 저장된다.
  - 사이트에는 "AI 생성 개요도 — 논문 원본 그림이 아니며 부정확할 수 있음"이 붙는다.
- **실행 순서와 한도**: 이미지 생성은 구독 한도를 일반 호출보다 3–5배 빨리 쓴다. 그래서 `run`은 요약을 모두 끝낸 뒤 그림을 만든다.
  - 요약이 한도로 멈췄으면 그날은 GA 확인만 한다.
  - 그림 생성 중 한도에 걸리면 멈추고, 남은 그림은 다음 실행으로 넘긴다. 실패는 논문당 2번까지 재시도한다.

**8. 한 번에 실행 + 배포 + 매일 자동 실행**

```bash
python3 -m digest run --no-publish --limit 3   # 수집→PDF→근거→요약(최대 3편)→그림→export, push 없음
python3 -m digest run --no-figures             # AI 개요도 없이 (GA 확인은 함)
python3 -m digest run                          # 전체 + data/ 커밋·push
```

## GitHub 연결 (한 번)

1. GitHub에서 **public** repo 생성.
2. 로컬에서:
   ```bash
   git init -b main      # 아직 안 했다면
   git remote add origin git@github.com:<user>/<repo>.git
   git add . && git commit -m "init" && git push -u origin main
   ```
3. repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
4. Actions 탭에서 `Deploy site` 성공 확인 → `https://<user>.github.io/<repo>/`.

`run`은 `data/`만 커밋한다(`git commit -- data`). 다른 작업 중인 변경은 건드리지 않는다.

## 매일 자동 실행 (07:00 KST)

```bash
deploy/install-timer.sh --print     # 생성될 유닛 확인
deploy/install-timer.sh             # 설치·활성화 (시각 지정: deploy/install-timer.sh 06:30)
sudo loginctl enable-linger "$USER" # 로그아웃 상태에서도 실행
systemctl --user list-timers paper-digest.timer
systemctl --user start paper-digest.service      # 지금 한 번 돌려보기
journalctl --user -u paper-digest.service -n 50  # 결과
```

로그는 `~/.local/share/paper-digest/logs/digest-YYYY-MM-DD.log`에도 남는다. systemd 아래에서는 ssh-agent가 없으므로 push에 쓰는 SSH 키는 패스프레이즈가 없어야 한다.

## 운영 명령

| 명령 | 용도 |
|---|---|
| `status` | 저널·상태·근거별 집계 |
| `list [--status S] [--date D] [--journal J] [--all]` | 목록 |
| `show <DOI>` | 로컬 정보와 요약 전체 |
| `resummarize <DOI>…` | 다시 요약 대기로(모델을 바꾼 뒤 등) |
| `rescore` | `interests.md` 수정 후 관련도만 재채점(요약 텍스트만 보냄, 15편씩 묶어 호출) |
| `figures [--limit N]` | GA 확인 + AI 개요도 생성 |

## 안전 설계

- **논문 텍스트는 신뢰하지 않는 입력**으로 다룬다.
  - codex: `--ignore-user-config`(사용자 MCP·플러그인 미로드), `-s read-only`, `web_search="disabled"`, shell 도구 비활성화, 빈 임시 디렉터리에서 실행한다. 그래도 명령 실행·MCP·웹 검색 이벤트가 보이면 결과를 버린다.
  - 요약 호출은 이미지 생성도 끈다(`--disable image_generation`). 개요도 호출에서만 켜며, 입력은 원문이 아니라 검증을 거친 요약이다.
  - claude: `--tools ""`(내장 도구 전부 제거), `--strict-mcp-config`, `--setting-sources ""`로 실행한다.
  - 두 엔진 모두 `OPENAI_API_KEY`·`ANTHROPIC_API_KEY` 등을 자식 프로세스에 넘기지 않는다.
  - 프롬프트 안에서 논문 텍스트는 `<paper_text>` 구획에 넣는다. 텍스트 안의 닫는 태그는 무력화한다.
- **근거 표시**: 사이트의 각 요약에 `근거: 전문/초록/PDF` 배지가 붙는다.
  - 초록 기반 요약에는 "방법·수치는 원문 확인" 안내가 붙는다.
  - 요약에 나온 수치가 근거 텍스트에 없으면 `⚠ 근거 텍스트에서 확인되지 않은 수치`로 표시한다(10 이하 정수 제외).
- **HTML 이스케이프**: 사이트 생성 시 모든 텍스트를 `html.escape` 처리하고 JavaScript는 쓰지 않는다.
- **공개 범위**: `data/`에는 허용 목록 필드만 내보낸다. RSS 소개문·초록·전문은 내보내지 않는다.
  - 요약·개요도에 쓴 엔진·모델 이름은 로컬 DB에만 남기고 내보내지 않는다.
  - 출판사 GA는 URL만 기록한다(이미지는 복사하지 않음).
  - AI 개요도는 요약에서 생성한 그림이라 `data/figures/`에 올라간다.
