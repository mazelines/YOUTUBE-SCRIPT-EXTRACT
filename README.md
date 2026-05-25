# YouTube 자막 추출기 (YouTube Transcript Extractor)

YouTube 영상 링크를 입력하면 자막(transcript)을 추출해 **마크다운(.md)** 파일로 저장하고, 선택적으로 **MP3 음원**도 추출하는 데스크톱 프로그램입니다. Qt 6(PySide6) 기반 GUI이며, **여러 영상을 동시에** 추출할 수 있습니다.

## 주요 기능

- 🎬 **다중 URL 입력** — 여러 줄로 붙여넣어 한 번에 목록에 추가
- ⚡ **동시 추출** — `QThreadPool` 기반 병렬 처리 (동시 작업 수 1~16 조절)
- 📝 **자막 → 마크다운** — 제목·URL·채널·언어·추출 시각 메타데이터 + 본문
- 🎵 **MP3 음원 추출** — yt-dlp + 동봉 ffmpeg로 음원만 추출 (128 / 192 / 320 kbps)
- 🌐 **언어 우선순위** — 예: `ko, en, ja` 순으로 자막 탐색, 수동 자막 우선/번역 폴백
- 🕒 **타임스탬프 토글** — 타임스탬프 포함 또는 본문만 추출
- 📊 **실시간 진행 표시** — 각 작업의 상태/진행/저장 파일을 표로 확인, 더블클릭으로 파일 열기

자막과 MP3는 **각각 또는 함께** 선택해 추출할 수 있습니다.

지원하는 URL 형식: `watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`, `/live/`, 그리고 11자리 영상 ID 직접 입력.

## 설치

```bash
pip install -r requirements.txt
```

- Python 3.10 이상 권장 (개발 환경: 3.12)

## 실행

```bash
python run.py
```

## 릴리스 빌드 (Windows 실행파일)

PyInstaller로 단일 실행파일(`.exe`)을 만듭니다. ffmpeg 바이너리·배너 이미지·yt-dlp 추출기 모듈이 모두 포함되어 **별도 설치 없이** 실행됩니다.

```bash
pip install pyinstaller
pyinstaller --noconfirm --clean YouTubeTranscriptExtractor.spec
```

- 결과물: `dist/YouTubeTranscriptExtractor.exe` (콘솔 창 없는 GUI 단일 파일)
- 빌드 검증: `dist/YouTubeTranscriptExtractor.exe --selftest` (정상 시 종료 코드 0)

## 사용법

1. 상단 입력란에 YouTube URL을 한 줄에 하나씩 붙여넣고 **목록에 추가**를 누릅니다.
2. **옵션**에서 저장 폴더, 선호 언어, 동시 작업 수, 타임스탬프 포함 여부를 설정합니다.
3. **추출 항목**에서 `자막 (.md)` / `MP3 음원`을 선택합니다(둘 다 가능). MP3는 음질을 고를 수 있습니다.
4. **추출 시작**을 누르면 대기 중인 모든 항목이 병렬로 처리됩니다.
5. 표의 항목을 더블클릭하면 저장된 파일이 열립니다.

> **ffmpeg 안내**: MP3 변환에는 ffmpeg가 필요합니다. `imageio-ffmpeg`가 ffmpeg 바이너리를 함께 제공하므로 별도 설치 없이 동작합니다. 시스템에 ffmpeg가 설치돼 있으면 그것도 사용할 수 있습니다.

기본 저장 위치는 실행 폴더의 `transcripts/` 입니다.

## 출력 예시

```markdown
# 영상 제목

- **URL**: https://www.youtube.com/watch?v=VIDEO_ID
- **영상 ID**: VIDEO_ID
- **채널**: 채널명
- **자막 언어**: Korean (`ko`, 수동 작성)
- **추출 시각**: 2026-05-25 14:30:00

---

## Transcript

`[0:00]` 안녕하세요, 오늘은…
`[0:04]` 다음 주제로 넘어가서…
```

## 프로젝트 구조

```
YOUTUBE-SCRIPT-EXTRACTOR/
├── run.py                  # 실행 진입점
├── requirements.txt
├── yt_extractor/
│   ├── __init__.py
│   ├── core.py             # 추출 로직 (URL 파싱·자막·MP3·마크다운) — GUI 비의존
│   ├── app.py              # PySide6 (Qt 6) GUI
│   └── img/
│       └── mazelinebanner.jpg   # 하단 광고 배너 이미지
└── README.md
```

`core.py`는 GUI에 의존하지 않으므로 CLI 등 다른 프론트엔드에서도 재사용할 수 있습니다. 하단 배너는 [MazeLine](https://mazeline.tech/)으로 연결됩니다.

## 참고

- 자막은 YouTube가 제공하는 경우에만 추출됩니다. 자막이 비활성화된 영상은 추출할 수 없습니다.
- 짧은 시간에 많은 요청을 보내면 YouTube가 일시적으로 IP를 제한할 수 있습니다. 이 경우 동시 작업 수를 줄이세요.
