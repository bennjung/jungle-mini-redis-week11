# Redis 탄생 배경: UX와 DX에서 시작하는 리서치

조사일: 2026-11-12. antirez의 블로그를 최우선으로 읽고, 본인의 운영 보고·인터뷰와 실제 도입팀 기록을 보조 근거로 사용했다. 이 문서는 역사 조사 노트이며 프로젝트의 요구사항·설계·테스트 계약을 추가하거나 변경하지 않는다.

## 먼저 이해할 요구

조사자의 종합 해석: 사용자는 방문·댓글·작업 결과를 즉시 보고 싶다. 개발자는 계속 바뀌는 목록과 작업 대기열을 여러 서버에서 빠르게 갱신해야 한다. Redis의 배경에는 이런 연산을 자연스럽게 표현하고 비용을 예측하려는 요구가 있다.

antirez는 2009-06-03 글에서 당시 RDBMS에 대한 자신의 문제의식을 데이터 모델과 예측하기 어려운 연산 비용으로 설명했다. 메모리의 리스트·집합을 다루듯 서버의 자료구조를 조작하는 것이 Redis의 방향이었다. 이는 창시자의 당시 관점이며 모든 SQL 구현의 한계를 입증하는 벤치마크는 아니다. [원문](https://oldblog.antirez.com/post/MongoDB-and-Redis.html)

## 최우선 원문: antirez의 블로그

- **2009-02-12, Redis - REmote DIctionary Server**: 영속성을 갖춘 메모리 DB와 리스트·집합 연산을 소개한다. 주기적 저장 사이의 변경은 장애로 잃을 수 있다고 명시한다. 댓글에서 일부 기능이 미완성이라고 설명하므로 모든 기능이 당시 완성됐다고 읽으면 안 된다. [원문](https://zzimma.antirez.com/post/Redis---REmote-DIctionary-Server.html)
- **2009-03-06, Redis, my new open source project**: 단순 GET/SET과 구별되는 특징으로 리스트 push/pop과 집합 연산을 제시한다. 사용자 메시지를 리스트에 추가하고 일부를 조회하는 예가 있다. [원문](https://oldblog.antirez.com/post/Redis-my-new-open-source-project.html)
- **2009-06-03, MongoDB and Redis**: 자료구조를 직접 조작하는 데이터 모델과 연산 비용에 관한 문제의식. [원문](https://oldblog.antirez.com/post/MongoDB-and-Redis.html)
- **2011-03-01, Redis Manifesto**: 자료형의 의미에 지원 연산뿐 아니라 공간·시간 복잡도도 포함한다고 설명한다. [원문](https://oldblog.antirez.com/post/redis-manifesto.html)
- **2011-06-28, How to take advantage of Redis just adding it to your stack**: 느린 최신 목록, 실시간 순위표 등의 문제 유형을 설명한다. 실명 서비스나 정확한 운영 구성은 제시하지 않는다고 저자가 명시한 글이다. [원문](https://oldblog.antirez.com/post/take-advantage-of-redis-adding-it-to-your-stack.html)

## 사례 1: LLOOGG — 실시간 방문 화면이 제품의 핵심이었다

LLOOGG는 2007년에 시작한 실시간 웹 방문 분석 서비스였다. 방문자의 행동을 발생하는 대로 보여주는 것이 제품의 가치였고, antirez는 MySQL을 사용한 확장 문제 때문에 Redis를 만들었다고 기록했다. [본인 저장소 README, 사후 회고](https://github.com/antirez/lloogg)

2011년 직접 인터뷰에서 더 구체적인 동기를 확인할 수 있다. 추적 코드가 설치된 페이지에서 방문마다 DB 쓰기가 발생했다. Ajax 화면에 방문 흐름을 실시간으로 보여주려면 빠른 쓰기와 최근 N개 조회가 필요했다. 그는 삽입 순서를 다루는 연결 리스트라면 단순한 일을 MySQL에서 충분한 성능으로 구현하기 어려웠다고 설명했다. [2011-01-04 인터뷰](https://www.eu-startups.com/2011/01/an-interview-with-salvatore-sanfilippo-creator-of-redis-working-out-of-sicily/)

Top-down으로 읽으면 다음과 같다. 아래 요구의 정리는 조사자의 해석이다.

1. 사용자: 지금 방문자가 무엇을 하는지 보고 싶다.
2. 제품: 방문 이벤트를 계속 받아 최근 기록을 즉시 표시한다.
3. 시스템: 이벤트 쓰기와 최근 N개 읽기를 함께 빠르게 처리한다.
4. 개발자 부담: 간단한 목록 요구를 기존 DB에서 구현·확장하는 비용이 커졌다.

2009-06-27 운영 보고에는 6월 19일 MySQL에서 Redis로 전환했다고 적혀 있다. 주요 데이터는 직렬화한 값을 담은 Redis 리스트였고, 770,018초 동안 49,106,754개 명령을 처리했다. 전환 뒤 DB 부하가 낮아졌다는 보고는 있으나, 동일 조건의 전후 응답 시간과 개선 배율은 확인할 수 없다. [당시 운영 보고](https://groups.google.com/g/redis-db/c/DHBqQ7x4sOU)

## 사례 2: 최신 댓글과 순위표 — 창시자가 제시한 문제 유형

antirez의 2011-06-28 글은 홈의 최신 댓글 목록이 느린 상황과 점수가 계속 바뀌는 순위표를 예로 든다. 최신 댓글은 새 댓글 ID를 리스트에 추가하며 최근 목록을 유지하고, 순위표는 점수에 따라 정렬되는 자료형을 활용한다. [원문](https://oldblog.antirez.com/post/take-advantage-of-redis-adding-it-to-your-stack.html)

조사자의 UX 해석: 새 댓글을 달았는데 최신 목록에 늦게 나타나거나, 점수가 바뀌었는데 내 순위가 뒤늦게 반영되면 제품의 반응성이 떨어진다. 이 문장은 설명용 상황이며 특정 서비스에서 측정된 장애 기록이 아니다. 또한 SQL의 ORDER BY가 언제나 전체 정렬을 수행한다는 뜻은 아니다.

## 사례 3: 초기 GitHub — 작업 두 개를 등록하느라 페이지에 4초가 추가됐다

2009-11-03 GitHub의 글은 Redis 도입 전의 실제 문제를 기록한다. DB 기반 DelayedJob 큐가 약 3만 건까지 밀리면 작업 생성과 잠금 획득이 각각 2초 이상 걸릴 수 있었다. 작업 두 개를 생성하는 페이지는 다른 처리에 앞서 약 4초의 추가 지연을 겪었다. 이는 Redis 공개 이전의 사례가 아니라 GitHub 도입 이전의 사례다. [GitHub 원문](https://github.blog/news-insights/introducing-resque/)

운영자는 밀린 작업·실패한 작업·멈춘 워커를 확인해야 했다. GitHub는 Redis의 원자적 리스트 연산과 조회 기능을 활용하는 Resque를 만들었다. 워커 관리와 관측 기능은 Resque가 담당하므로 전체 개선을 Redis의 메모리 속도 하나로 설명할 수 없다. 같은 글은 출시 시점까지 천만 건 이상의 작업을 처리했다고 보고한다. [같은 원문](https://github.blog/news-insights/introducing-resque/)

## Redis 공개 전의 배경 사례: LiveJournal과 Memcached

Brad Fitzpatrick의 2004년 글은 사용자별 공개 범위와 서로 다른 구성요소의 갱신 주기 때문에 페이지 전체를 미리 생성하기 어려웠다고 설명한다. 웹 프로세스와 서버마다 캐시를 따로 두면 중복도 컸다. 공유 메모리 캐시가 필요했던 실제 배경이며, 여기서 사용한 도구는 Memcached였다. Redis가 메모리 캐시라는 발상 자체를 처음 만든 것은 아니다. [개발자 본인의 글](https://www.linuxjournal.com/article/7451)

## 캐시 미스와 디스크 접근의 구분

과거 MySQL Query Cache는 테이블 변경 시 해당 테이블을 사용하는 쿼리 결과를 무효화했다. 이 기능은 MySQL 8.0에서 제거됐다. [공식 설명](https://docs.oracle.com/cd/E17952_01/mysql-5.7-en/query-cache-operation.html)

하지만 쿼리 결과 캐시 미스가 디스크 읽기를 반드시 뜻하지는 않는다. InnoDB는 테이블과 인덱스 페이지를 별도의 메모리 버퍼 풀에 보관한다. 따라서 결과를 다시 계산해도 필요한 데이터가 메모리에 있을 수 있다. [공식 설명](https://docs.oracle.com/cd/E17952_01/mysql-5.7-en/innodb-buffer-pool.html)

## 확인 범위

- 창시자의 블로그와 직접 발언을 구분하고, 동시대 기록과 사후 회고를 구분했다.
- 특정 LLOOGG 지연 시간, MySQL 대비 개선 배율, 모든 읽기·쓰기의 디스크 접근은 확인되지 않았으므로 주장하지 않는다.
- 블로그의 활용 패턴을 LLOOGG 최초 구현의 정확한 명령 호출 순서로 간주하지 않는다.
- 기존 요구사항은 `resc/phase.md`, 작업 지침은 `AGENTS.md`에서 확인했다. 기존 문서의 개념·함수·테스트 계약을 변경하지 않았으며 이 조사 노트만 추가했다. 코드 변경과 테스트 실행은 없다.
