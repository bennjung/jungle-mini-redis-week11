-- 시나리오 03 성능 비교용 MySQL 8.4 시험 전용 테이블.
--
-- 적용 대상: 사용자 데이터와 분리된 시험 전용 스키마(예: mini_redis_benchmark).
-- 이 파일은 테이블을 새로 만들기만 한다. 기존 테이블을 DROP·TRUNCATE·DELETE하거나
-- 구조를 자동 변경하지 않는다. 이미 다른 구조의 benchmark_counter가 있으면
-- 적용을 중단하고 사람이 확인한다.
--
-- 초기 행과 측정 전 0 초기화는 JMeter 준비 단계가 공개 SQL 경로로 수행한다.

CREATE TABLE benchmark_counter (
    counter_key VARBINARY(256) PRIMARY KEY,
    value BIGINT NOT NULL
) ENGINE=InnoDB;
