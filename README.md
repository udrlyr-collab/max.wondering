# MovieMax

MovieMax는 CGV 영화의 선택한 상영 포맷별 회차를 주기적으로 조회하고, 새 예매 회차·예매 오픈·추적 중인 회차의 잔여석 증가를 Telegram, 브라우저 Web Push, 열린 웹 콘솔로 알리는 개인용 관리 콘솔입니다. 관리자 화면에서는 날짜, 시작·종료 시각, 상영관, 포맷, 예매 상태, 잔여석/전체 좌석을 확인할 수 있습니다.

기본값에서는 감시 대상을 자동 생성하지 않습니다. 콘솔에서 극장·영화·상영 포맷을 순서대로 골라 2D, 4DX, SCREENX, IMAX 등 CGV가 현재 반환하는 포맷을 별도 대상으로 추가할 수 있습니다. `SEED_DEFAULT_TARGET=true`를 명시한 설치만 환경변수의 기본 대상을 생성합니다.

> 2026-07-26 기준 `max.wondering.kr`의 Lightsail 배포, HTTPS, Caddy 인증, CGV 실조회와 감지 워커 동작을 확인했습니다. Telegram 봇은 관리자가 BotFather에서 발급한 토큰과 Chat ID를 콘솔에 저장해야 활성화됩니다.

## 동작 범위

관리자 콘솔에서 다음 작업을 할 수 있습니다.

- CGV 극장·현재 상영 영화·상영 포맷을 선택해 감시 대상 추가
- 감시 대상 활성화/비활성화
- 감시 대상과 그 대상의 회차·좌석 이력·알림 기록을 함께 삭제
- 새 회차 및 예매 오픈 알림 활성화/비활성화
- 새로 발견한 회차의 좌석 증가 알림 자동 활성화/비활성화
- 특정 회차의 좌석 증가 알림과 회차별 최소 증가 기준 설정
- 날짜·시간·상영관·포맷·예매 상태·잔여석/전체 좌석 확인
- 즉시 조회 요청, 워커 상태와 최근 알림 이벤트 확인
- Telegram 봇 토큰과 알림 채팅 연결, 시험 메시지 전송
- 기기별 브라우저 Web Push 연결·시험·해제

알림 판정은 다음과 같습니다.

- 첫 번째 정상 조회는 기준 상태로 저장하며 알림을 보내지 않습니다.
- 이후 선택한 포맷에서 새로 나타난 예매 가능 회차를 알립니다.
- `예매 준비중`이던 회차가 예매 가능 상태로 바뀌면 알립니다.
- 좌석 증가 알림을 켠 회차에서 잔여석 증가량이 회차별 기준 이상이면 알립니다. 이는 취소표일 수 있지만 CGV의 좌석 재고 조정일 수도 있으므로 확정적인 취소표로 간주하지 않습니다.
- 잔여석 감소는 전체 감지 기록에는 저장하지만 메인 최근 알림과 Telegram에는 보내지 않습니다.
- 이 서비스는 좌석 선점이나 예매를 수행하지 않습니다.

구성은 다음 세 Compose 서비스와 하나의 named volume으로 이루어집니다.

```text
브라우저 -> web 관리자 콘솔 ─────────┐
                                      ├─ moviemax-data (SQLite)
CGV/Telegram/Web Push <- worker ──────┘
              migrate: DB 초기화 후 종료
```

## 보안상 중요한 사항

현재 애플리케이션에는 자체 사용자 계정이나 로그인 기능이 없습니다. `TrustedHost`, Origin 검사, 변경 요청용 헤더 검사는 요청 위조 방어 수단이지 사용자 인증 수단이 아닙니다.

- `compose.yaml`은 관리자 콘솔을 호스트의 `127.0.0.1:8787`에만 바인딩합니다.
- 인터넷에 공개할 때는 기존 Caddy, VPN 또는 별도 접근 제어 계층에서 반드시 인증해야 합니다. 아래 Lightsail 예시는 HTTPS와 Caddy `basic_auth`를 사용합니다.
- Telegram 봇 토큰, Web Push 구독 주소·암호화 키, VAPID 개인 키는 `APP_ENCRYPTION_KEY`로 암호화해 SQLite에 저장하며 화면/API에 다시 노출하지 않습니다. 공개 VAPID 키, Chat ID와 나머지 콘솔 상태도 같은 SQLite DB에 저장됩니다.
- `APP_ENCRYPTION_KEY`를 잃거나 바꾸면 기존 봇 토큰을 복호화할 수 없습니다. DB와 암호화 키를 함께, 서로 안전한 위치에 백업해야 합니다.
- `.env`, `secrets/`, 기본 `data/` 경로의 SQLite DB를 버전 관리에 추가하지 마세요. 현재 `.gitignore`와 `.dockerignore`에서 이 기본 경로들이 제외되어 있습니다.
- 콘솔의 감시 대상 삭제는 현재 활성 SQLite DB의 관련 행을 모두 제거합니다. 삭제 전에 만든 별도 DB 백업 파일은 자동으로 수정하거나 폐기하지 않습니다.

## 로컬 실행: Docker Compose

Docker Engine 또는 Docker Desktop과 Compose 플러그인이 필요합니다.

1. 환경 파일을 만듭니다.

```bash
cp .env.example .env
# Windows PowerShell: Copy-Item .env.example .env
```

2. 유효한 Fernet 키를 생성합니다. 아래 명령의 출력 한 줄을 복사합니다.

```bash
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

로컬 Python이 없다면 Docker로도 생성할 수 있습니다.

```bash
docker run --rm python:3.12-slim python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

3. `.env`의 다음 값을 로컬 주소에 맞게 수정하고 생성한 키를 붙여 넣습니다. `CONSOLE_PUBLIC_ORIGIN`과 브라우저에서 여는 주소가 정확히 같아야 합니다.

```dotenv
CONSOLE_PUBLIC_ORIGIN=http://127.0.0.1:8787
CONSOLE_ALLOWED_HOSTS=127.0.0.1,localhost
APP_ENCRYPTION_KEY=<위에서 생성한 Fernet 키>
```

Linux/macOS에서는 `chmod 600 .env`로 권한을 제한합니다.

4. 구성 확인 후 세 서비스를 시작합니다.

```bash
docker compose config --quiet
docker compose build --pull
docker compose up -d --remove-orphans
docker compose ps
```

브라우저에서 정확히 <http://127.0.0.1:8787>을 엽니다. `http://localhost:8787`을 사용하려면 `CONSOLE_PUBLIC_ORIGIN`도 그 주소로 바꾸고 서비스를 다시 생성해야 합니다.

```bash
docker compose up -d --force-recreate web worker
```

로그와 상태는 다음과 같이 확인합니다.

```bash
docker compose logs -f --tail=100 web worker
curl -fsS http://127.0.0.1:8787/healthz
docker compose exec worker python -m moviemax console-worker-health
```

중지할 때는 named volume을 보존합니다.

```bash
docker compose down
```

`docker compose down -v`는 `moviemax-data`를 삭제하므로 초기화가 목적이 아니라면 사용하지 마세요.

## 로컬 실행: Python 직접 실행

Python 3.11 이상이 필요합니다. `.env.example`은 Compose용 `/data/console.sqlite3` 경로를 사용하므로 직접 실행할 때는 로컬 경로로 바꿉니다.

```bash
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
cp .env.example .env
```

`.env`에서 최소한 다음 값을 설정합니다.

```dotenv
CONSOLE_DB_PATH=./data/console.sqlite3
CONSOLE_WEB_HOST=127.0.0.1
CONSOLE_WEB_PORT=8787
CONSOLE_PUBLIC_ORIGIN=http://127.0.0.1:8787
CONSOLE_ALLOWED_HOSTS=127.0.0.1,localhost
APP_ENCRYPTION_KEY=<생성한 Fernet 키>
```

DB를 초기화한 뒤 웹과 워커를 서로 다른 터미널에서 실행합니다. 두 터미널은 같은 프로젝트 디렉터리와 `.env`를 사용해야 합니다.

```bash
python -m moviemax console-migrate
python -m moviemax console-web
```

```bash
python -m moviemax console-worker
```

개발 검증 명령은 다음과 같습니다.

```bash
ruff check src tests
pytest
python -m moviemax check-cgv
```

## 환경변수

`Settings.from_env()`는 실행 시 프로젝트 루트의 `.env`를 읽되, 이미 설정된 프로세스 환경변수를 덮어쓰지 않습니다.

| 변수 | 용도와 현재 동작 |
| --- | --- |
| `APP_ENCRYPTION_KEY` | Telegram 봇 토큰 암호화에 필요한 Fernet 키. 콘솔 명령에는 필수입니다. |
| `APP_ENCRYPTION_KEY_FILE` | 키를 파일에서 읽는 대안. `compose.secrets.yaml`은 Docker Compose [secret 파일](https://docs.docker.com/compose/how-tos/use-secrets/)을 `/run/secrets/app_encryption_key`에 마운트합니다. 직접 값과 파일이 모두 있으면 직접 값이 우선합니다. |
| `CONSOLE_DB_PATH` | 콘솔 SQLite 경로. `compose.yaml`에서는 `/data/console.sqlite3`로 고정됩니다. |
| `CONSOLE_PUBLIC_ORIGIN` | 브라우저에서 사용하는 정확한 `http(s)://호스트[:포트]`. 경로, 쿼리, fragment를 포함할 수 없습니다. 변경 요청의 Origin 검사에 사용됩니다. |
| `CONSOLE_ALLOWED_HOSTS` | 허용할 HTTP Host를 쉼표로 구분한 목록. 공개 도메인을 반드시 포함해야 합니다. 인증 설정은 아닙니다. |
| `CONSOLE_WEB_HOST`, `CONSOLE_WEB_PORT` | 직접 실행 시 웹 리슨 주소와 포트. Compose 내부에서는 `0.0.0.0:8000`, 호스트에서는 `127.0.0.1:8787`로 고정됩니다. |
| `CONSOLE_WORKER_TICK_SECONDS` | 워커가 할 일을 다시 확인하는 간격. 허용 범위는 1~30초이며 대상별 CGV 조회 주기와는 다릅니다. |
| `SEED_DEFAULT_TARGET` | 기본값 `false`. `true`이면 기본 대상이 없을 때 환경변수 값으로 다시 생성하므로 운영 콘솔에서는 삭제 영속성을 위해 `false`를 사용합니다. |
| `CGV_COMPANY_CODE` | 기본 대상의 CGV 회사 코드. 현재 예시는 `A420`입니다. |
| `CGV_SITE_NO`, `CGV_SITE_NAME` | 처음 생성할 기본 극장 번호와 표시 이름. 현재 예시는 용산아이파크몰 `0013`입니다. |
| `CGV_MOVIE_NO`, `CGV_MOVIE_NAME` | 처음 생성할 기본 영화 번호와 표시 이름. 현재 예시는 오디세이 `30001323`입니다. |
| `CGV_FORMAT_CODE` | 기본 대상의 CGV 상영 포맷 코드. 값이 있으면 정확히 일치하는 코드만 조회합니다. 콘솔에서 추가한 대상은 CGV 카탈로그의 현재 코드를 자동 저장합니다. |
| `CGV_FORMAT_KEYWORD`, `CGV_SCREEN_GRADE_CODE` | 코드가 없는 기존 기본 대상의 호환 필터. 현재 IMAX 예시는 `IMAX`, `0301`입니다. |
| `POLL_INTERVAL_SECONDS` | 처음 생성하는 기본 대상의 조회 주기. 5초 미만은 거부되며 예시는 60초입니다. 콘솔에서 새로 추가한 대상은 현재 60초로 생성됩니다. |
| `REQUEST_TIMEOUT_SECONDS` | CGV, Telegram, Web Push 요청 제한 시간. |
| `REQUEST_GAP_SECONDS` | 여러 날짜의 CGV 일정을 연속 조회할 때 요청 사이에 두는 간격. |
| `BACKOFF_MAX_SECONDS` | CGV 조회 실패 시 지수 백오프의 상한. `POLL_INTERVAL_SECONDS`보다 작을 수 없습니다. |
| `TELEGRAM_MAX_ATTEMPTS`, `TELEGRAM_RETRY_BASE_SECONDS` | Telegram 발송 재시도 횟수와 기본 지연 시간. 한도를 넘거나 재시도 불가능한 오류는 dead-letter로 기록합니다. |
| `LOG_LEVEL` | Python 로그 레벨. 예시는 `INFO`입니다. |

`STATE_DB_PATH`, `HEARTBEAT_PATH`, `NOTIFY_ON_INITIAL_STATE` 등 `.env.example` 하단의 legacy 항목은 기존 단일 모니터 CLI용 설정입니다. 코드가 지원하는 `TELEGRAM_BOT_TOKEN`과 `TELEGRAM_CHAT_ID` 환경변수도 legacy Telegram CLI용이며 현재 `.env.example`에는 포함되어 있지 않습니다. `console-web`/`console-worker` 흐름에서는 Telegram 토큰과 Chat ID를 관리자 콘솔에서 저장합니다.

## Telegram 봇 연결

Telegram 공식 안내에 따라 [@BotFather](https://t.me/BotFather)에서 봇을 등록하고 인증 토큰을 발급받습니다. 토큰을 가진 사람은 봇을 제어할 수 있으므로 코드, 채팅, 화면 캡처에 노출하지 마세요. 자세한 원칙은 [Telegram Bots 공식 문서](https://core.telegram.org/bots)를 참고하세요.

1. Telegram에서 `@BotFather`를 열고 `/newbot`을 보냅니다.
2. 안내에 따라 봇 이름과 사용자 이름을 정하고 발급된 Bot token을 보관합니다.
3. 생성한 봇과의 대화를 열어 `시작` 또는 `/start`를 보냅니다. Telegram 봇은 먼저 사용자와 대화를 시작할 수 없습니다.
4. MovieMax 콘솔에서 `Telegram`을 열고 Bot token을 입력합니다.
5. `채팅 찾기`를 누릅니다. 목록이 비어 있으면 봇 대화에 `/start` 또는 새 메시지를 보낸 뒤 다시 누릅니다.
6. 연결 확인용 채팅을 선택하고 `Telegram 알림 활성화`를 켠 뒤 `설정 저장`을 누릅니다.
7. `시험 메시지`를 눌러 휴대폰에서 수신 여부를 확인합니다.
8. 감시 대상을 추가할 때 `Telegram 사용`을 켜고 `/start`를 보낸 사용자 한 명을 선택합니다.
9. 해당 사용자에게 보낼 `새 회차·예매 오픈`과 `잔여석 증가`를 각각 선택합니다. 생성 후에도 대상별 감지 설정에서 변경할 수 있습니다.

저장 시 서버가 Telegram `getMe` 요청으로 토큰을 확인합니다. `채팅 찾기`와 대상별 `사용자 새로고침`은 Telegram `getUpdates` 결과를 읽고, 확인된 사용자 목록을 DB에 보존합니다. 대상별 Telegram을 켜지 않은 대상은 브라우저 Push와 웹 감지 기록만 유지하며 Telegram으로 보내지 않습니다. 대상에 지정된 사용자는 다른 대상의 알림을 받지 않습니다. 저장된 토큰을 바꿀 때만 토큰 필드를 다시 입력하면 됩니다.

## 브라우저 Web Push

콘솔의 `브라우저 알림`에서 `이 기기 알림 켜기`를 누르면 현재 기기의 알림 권한을 요청하고 Push 구독을 서버에 암호화해 저장합니다. 서버 워커는 추적 중인 회차에서 회차별 기준 이상으로 잔여석이 증가한 이벤트만 기기별 전달 큐에 추가합니다. Telegram 설정이나 웹 페이지의 열림 여부와 관계없이 각 Push 서비스로 발송합니다.

iPhone·iPad에서는 사이트를 홈 화면에 추가한 뒤 홈 화면의 웹 앱에서 이 버튼을 눌러야 합니다. 권한 요청은 자동 실행하지 않습니다. `시험 알림`으로 현재 기기의 서버 발송 경로를 확인할 수 있고, `이 기기 알림 끄기`는 브라우저 구독과 서버의 해당 기기 구독을 함께 제거합니다.

## AWS Lightsail Ubuntu + 기존 Caddy 배포

아래 절차는 다음 조건을 전제로 합니다.

- Ubuntu Lightsail 인스턴스에 Docker Engine과 Compose 플러그인이 설치되어 있음
- 기존 Caddy 컨테이너 이름이 `market-dominion-caddy-1`이고 Docker host network를 사용함
- Caddyfile의 호스트 bind mount 경로가 `/home/ubuntu/market-dominion/deploy/Caddyfile`임
- `max.wondering.kr` DNS가 해당 인스턴스를 가리킴
- 기존 Caddy 컨테이너가 80/443 포트와 HTTPS 인증서 발급을 처리함

Docker 설치가 필요하면 [Docker Engine Ubuntu 공식 절차](https://docs.docker.com/engine/install/ubuntu/)를 사용하세요.

### 1. Lightsail 방화벽

Lightsail의 IPv4와 IPv6 방화벽은 별도입니다. 사용하는 각 방화벽에서 다음만 허용합니다.

- TCP 22: 가능한 경우 관리자 IP/CIDR로 제한
- TCP 80, 443: 기존 Caddy에 필요한 범위로 허용
- TCP 8787: 규칙을 추가하지 않음

현재 Compose 포트 매핑은 `127.0.0.1:8787:8000`이므로 8787은 호스트 외부 인터페이스에서 수신하지 않습니다. AWS 동작은 [Lightsail 방화벽 공식 문서](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-firewall-and-port-mappings-in-amazon-lightsail.html)를 참고하세요.

### 2. 프로젝트와 환경 준비

예시는 저장소 파일이 `/opt/moviemax`에 전송되어 있다고 가정합니다.

```bash
cd /opt/moviemax
cp .env.example .env
sudo chmod 600 .env
sudoedit .env
```

`.env`에서 다음 값을 확인합니다. 운영에서는 직접 키 값은 비워 두고 secret overlay를 사용합니다.

```dotenv
CONSOLE_PUBLIC_ORIGIN=https://max.wondering.kr
CONSOLE_ALLOWED_HOSTS=max.wondering.kr,127.0.0.1,localhost
APP_ENCRYPTION_KEY=
```

Fernet 키 파일을 한 번만 생성합니다. 이미지의 실행 UID는 `10001`입니다.

```bash
sudo install -d -m 0700 secrets
python3 -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())" \
  | sudo tee secrets/app_encryption_key >/dev/null
sudo chown 10001:10001 secrets/app_encryption_key
sudo chmod 0400 secrets/app_encryption_key
```

키 파일이 이미 있다면 위 생성 명령을 다시 실행하지 마세요. 기존 키를 덮어쓰면 DB에 저장된 Telegram 토큰을 복호화할 수 없습니다.

### 3. Compose 구성 확인과 이미지 빌드

운영 명령에는 항상 두 Compose 파일을 함께 지정합니다.

```bash
sudo docker compose -f compose.yaml -f compose.secrets.yaml config --quiet
sudo docker compose -f compose.yaml -f compose.secrets.yaml build --pull
```

이 단계에서는 아직 MovieMax 컨테이너를 시작하지 않습니다. 먼저 Caddy에 인증·요청 크기 제한·reverse proxy를 함께 적용하고 검증해야 합니다.

### 4. 기존 Caddy 컨테이너에 보호된 route 추가

애플리케이션 자체 로그인 기능이 없으므로 인증 없이 먼저 공개하는 순서는 금지합니다.

> 적용 순서는 `basic_auth` + `request_body` + `reverse_proxy`를 한 번에 편집 → Caddy 설정 검증 → Caddy reload → MovieMax 시작입니다. 인증 없는 `reverse_proxy`만 먼저 reload하거나 임시로 공개하지 마세요.

먼저 현재 컨테이너가 실행 중이고 host network와 Caddyfile bind mount를 사용하는지 확인합니다.

```bash
sudo docker inspect market-dominion-caddy-1 \
  --format 'network={{.HostConfig.NetworkMode}}'
sudo docker inspect market-dominion-caddy-1 \
  --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
sudo docker exec market-dominion-caddy-1 caddy version
```

검사 결과에서 network가 `host`이고 `/home/ubuntu/market-dominion/deploy/Caddyfile`이 컨테이너의 `/etc/caddy/Caddyfile`에 연결되어 있는지 확인합니다. 결과가 다르면 아래 명령을 실행하지 말고 실제 mount destination부터 확인합니다.

아래 설정은 Caddy 2.8 이상에서 사용하는 `basic_auth` 지시어를 전제로 합니다. 컨테이너 버전이 2.8 미만이면 여기서 중단하고 Caddy 이미지를 먼저 업데이트합니다. 인증 지시어를 생략한 채 진행하지 마세요.

평문 비밀번호를 명령 인자로 남기지 않는 대화형 방식으로 관리자 비밀번호 해시를 만듭니다.

```bash
sudo docker exec -it market-dominion-caddy-1 \
  caddy hash-password --algorithm argon2id
```

호스트의 bind mount 원본을 열어 아래 세 지시어를 한 번의 편집으로 추가합니다. 기존 Caddyfile 전체를 덮어쓰지 마세요. `max.wondering.kr` 블록이 이미 있으면 같은 블록에 세 지시어를 넣고, 동일한 사이트 블록을 중복 생성하지 않습니다.

```bash
sudoedit /home/ubuntu/market-dominion/deploy/Caddyfile
```

```caddyfile
max.wondering.kr {
    basic_auth argon2id {
        moviemax <위 명령이 출력한 해시>
    }

    request_body {
        max_size 32768
    }

    reverse_proxy 127.0.0.1:8787 {
        header_up -Authorization
    }
}
```

`32768`은 애플리케이션의 32 KiB 요청 본문 제한과 같은 바이트 수입니다. Caddy 공식 문서는 [basic_auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth), [request_body](https://caddyserver.com/docs/caddyfile/directives/request_body), [reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)를 참고하세요. Basic Authentication은 평문 HTTP에서 안전하지 않으므로 Caddy의 HTTPS를 통해서만 사용합니다.

편집한 파일은 bind mount를 통해 컨테이너의 `/etc/caddy/Caddyfile`에 반영됩니다. 실행 중인 Caddy 컨테이너 안에서 먼저 검증하고, 검증이 성공한 경우에만 reload합니다.

```bash
sudo docker exec market-dominion-caddy-1 \
  caddy validate --config /etc/caddy/Caddyfile
sudo docker exec market-dominion-caddy-1 \
  caddy reload --config /etc/caddy/Caddyfile
sudo docker logs --tail=100 market-dominion-caddy-1
```

검증 또는 reload가 실패하면 MovieMax를 시작하지 말고 Caddyfile을 수정한 뒤 다시 검증합니다. `market-dominion-caddy-1`은 host network를 사용하므로 컨테이너의 `127.0.0.1:8787`에서 호스트의 MovieMax loopback 포트에 접근할 수 있습니다.

### 5. MovieMax 시작과 공개 확인

Caddy의 보호된 route가 정상적으로 reload된 뒤에만 MovieMax를 시작합니다.

```bash
cd /opt/moviemax
sudo docker compose -f compose.yaml -f compose.secrets.yaml up -d --remove-orphans
sudo docker compose -f compose.yaml -f compose.secrets.yaml ps
curl -fsS http://127.0.0.1:8787/healthz
sudo ss -ltnp | grep ':8787'
```

`migrate`는 DB 초기화를 마치고 정상 종료하는 일회성 서비스입니다. `web`과 `worker`는 `restart: unless-stopped`로 실행됩니다. `ss` 출력의 로컬 주소는 `127.0.0.1:8787`이어야 하며 `0.0.0.0:8787` 또는 `[::]:8787`이면 공개 확인 전에 Compose 포트 매핑을 바로잡아야 합니다.

이제 인증 정보 없이 요청했을 때 HTTP 401이 반환되는지 먼저 확인합니다.

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://max.wondering.kr/
# 예상 출력: 401
```

그 다음 브라우저에서 `https://max.wondering.kr`에 접속해 Caddy 인증 후에만 관리자 콘솔이 표시되는지 확인합니다.

### 6. 운영 확인과 업데이트

```bash
cd /opt/moviemax
sudo docker compose -f compose.yaml -f compose.secrets.yaml ps
sudo docker compose -f compose.yaml -f compose.secrets.yaml logs -f --tail=100 web worker
sudo docker compose -f compose.yaml -f compose.secrets.yaml exec worker \
  python -m moviemax console-worker-health
```

코드를 업데이트한 뒤에는 같은 두 Compose 파일로 다시 빌드하고 실행합니다.

```bash
sudo docker compose -f compose.yaml -f compose.secrets.yaml build --pull
sudo docker compose -f compose.yaml -f compose.secrets.yaml up -d --remove-orphans
sudo docker compose -f compose.yaml -f compose.secrets.yaml ps
```

`moviemax-data` volume에는 SQLite DB, 상영 회차 이력, Telegram/Web Push 전달 상태, Push 구독·VAPID 키와 워커 heartbeat가 들어 있습니다. 업데이트 전에 volume과 `secrets/app_encryption_key`를 모두 백업하세요. `down -v` 또는 `docker volume rm moviemax-data`는 데이터를 삭제합니다.

## CGV 비공식 API 의존성과 장애 판단

이 프로젝트는 CGV가 예매 페이지에서 사용하는 다음 내부 JSON 경로를 현재 코드 기준으로 호출합니다.

- `/api/v1/booking/searchRegnList`
- `/api/v1/booking/searchSiteScnscYmdListBySite`
- `/api/v1/booking/searchMovScnInfo`
- `/api/v1/booking/searchSiteScnscYmdListByMov`
- `/api/v1/booking/searchSchByMov`

이 경로들은 이 프로젝트를 위한 공식·계약형 API가 아닙니다. CGV가 URL, 요청 파라미터, 응답 필드, 상영 포맷 코드, 예매 상태 의미 또는 접근 정책을 바꾸면 감시가 중단되거나 코드 수정이 필요할 수 있습니다. Cloudflare 또는 CGV가 Lightsail 출발 IP를 차단하거나 제한하면 HTTP 403/429가 발생할 수 있으며, 로컬 PC에서 성공했다는 사실만으로 Lightsail에서도 성공한다고 보장할 수 없습니다.

코드는 HTTP 오류, JSON이 아닌 응답, 예상 필드 누락과 잘못된 좌석 값 등을 성공 결과로 저장하지 않고 대상 오류로 기록합니다. 연속 실패 시 조회 간격을 늘리며, 활성 대상이 3회 이상 연속 실패하면 worker healthcheck가 실패합니다. 이 상태에서는 다음을 확인합니다.

```bash
sudo docker compose -f compose.yaml -f compose.secrets.yaml logs --tail=200 worker

# .env에 정의된 기본 대상만 상태 변경 없이 한 번 조회
sudo docker compose -f compose.yaml -f compose.secrets.yaml run --rm worker check-cgv
```

- 403/429: 해당 서버 IP의 CGV 접근 제한 가능성을 확인합니다. 우회나 과도한 재시도를 전제로 운영하지 않습니다.
- invalid JSON 또는 필드 검증 오류: CGV 응답 형식 변경 여부를 확인하고 파서와 테스트를 함께 수정해야 합니다.
- 회차 0건: 실제 편성 없음, 영화 번호 변경, 극장/영화 선택 오류를 구분해 CGV 웹 페이지와 콘솔의 현재 카탈로그를 함께 확인합니다.
- 기존 감시 대상의 영화 번호가 더 이상 유효하지 않으면 기존 대상을 비활성화하고 콘솔에서 현재 카탈로그에 나타나는 새 대상을 추가합니다.

한 번의 대상 조회는 먼저 `searchSiteScnscYmdListByMov`를 호출해 상영 날짜 목록을 받고, 각 날짜마다 `searchSchByMov`를 한 번씩 호출합니다. 날짜별 시간표 응답에는 해당 영화의 여러 회차와 각 회차의 잔여석이 함께 들어오므로, 알림 회차 선택 여부는 CGV 요청 수를 바꾸지 않습니다. 날짜 수가 `D`개이면 정상 조회 한 주기의 요청 수는 `1 + D`회입니다. 선택하지 않은 회차도 마지막 조회 잔여석은 갱신되며, 선택은 좌석 증가 알림 여부와 기준값만 제어합니다.

`POLL_INTERVAL_SECONDS`는 5초 미만으로 설정할 수 없습니다. 다음 조회 시각은 한 주기의 응답 처리가 끝난 뒤 기본 간격과 무작위 오프셋을 더해 정하므로 실제 조회 시작 간격에는 CGV 응답 시간과 최대 `CONSOLE_WORKER_TICK_SECONDS`의 워커 확인 지연도 포함됩니다. 5초 설정은 CGV 요청 빈도를 크게 늘리므로 필요한 대상에만 사용하세요. 표시되는 좌석은 마지막 정상 조회 시점의 CGV 응답이며 실시간 좌석 보장을 의미하지 않습니다.
