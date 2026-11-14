/*
 * 시나리오 03 JMeter 조율·계측 스크립트.
 *
 * 모든 JSR223 요소가 이 파일 하나를 file script로 사용하고 Parameters로 단계를 받는다.
 * 실행마다 다른 문자열을 소스에 넣지 않는다.
 *
 * - 시작 조율: 워밍업 검증 → 전체 준비 완료 대기 → 대표 0 초기화·확인 → 시작
 * - 호출 계측: 마지막 PreProcessor에서 nanoTime 시작·마감 기록,
 *   첫 PostProcessor에서 반환·판정 기록
 * - 논리적 마감: 준비 단계에서 만든 감시 스레드 하나가 마감 시각에 시간 초과를 한 번만 결정
 * - 늦은 응답: 이미 결정된 시간 초과를 성공으로 바꾸지 않고 보조 관찰만 남긴다.
 * - 전체 실패: 처리 여부 불명확이면 새 배분을 막고 진행 표식을 완료로 바꾸지 않는다.
 */

import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.ScheduledThreadPoolExecutor
import java.util.concurrent.ThreadFactory
import java.util.concurrent.TimeUnit
import org.apache.jorphan.util.JMeterStopThreadException

class M3Coordinator {

    String runId
    String target
    String runDir
    String callsFile
    String completionFile
    int c
    int n
    int warmupTotal
    int warmupPerThread
    long requestTimeoutNs
    long totalLimitNs
    long prepLimitNs
    long finishLimitNs
    long setupStartNs
    long startNs = 0L
    long endNs = 0L
    long lastDecisionNs = 0L
    int inFlight = 0
    int maxInFlight = 0
    int warmupCompleted = 0
    boolean started = false
    boolean stopped = false
    boolean prepFailed = false
    boolean resetVerified = false
    boolean limitHit = false
    boolean measurementComplete = false
    boolean totalFired = false
    boolean finalReadDone = false
    boolean finalReadOk = false
    String stopReason = ''
    String prepFailure = ''
    String finalValue = null
    Object scheduler = null
    Object totalFuture = null
    long testTotalCallbackDelayMs = 0L
    final Properties properties
    def log

    final Map<String, Map> records = new LinkedHashMap<>()
    final Map<String, Object> deadlineFutures = new HashMap<>()
    final Map<String, Integer> warmupCounts = new HashMap<>()
    final Map<String, Integer> measureSequences = new HashMap<>()
    final Map<String, String> connectionIds = new LinkedHashMap<>()
    final Map<String, String> connectionIdsAfter = new LinkedHashMap<>()
    final Set<String> readyThreads = new LinkedHashSet<>()

    M3Coordinator(Properties properties, def logger) {
        this.properties = properties
        this.log = logger
        this.runId = required(properties, 'm3.run_id')
        this.target = required(properties, 'm3.target')
        this.runDir = required(properties, 'm3.run_dir')
        this.callsFile = required(properties, 'm3.calls_file')
        this.completionFile = required(properties, 'm3.completion_file')
        this.c = intValue(properties, 'm3.c')
        this.n = intValue(properties, 'm3.n')
        this.warmupTotal = intValue(properties, 'm3.warmup_total')
        this.warmupPerThread = intValue(properties, 'm3.warmup_per_thread')
        this.requestTimeoutNs = intValue(properties, 'm3.request_timeout_ms') * 1_000_000L
        this.totalLimitNs = intValue(properties, 'm3.total_limit_ms') * 1_000_000L
        this.prepLimitNs = intValue(properties, 'm3.prep_limit_ms') * 1_000_000L
        this.finishLimitNs = intValue(properties, 'm3.finish_limit_ms') * 1_000_000L
        this.setupStartNs = System.nanoTime()
        String testDelay = properties.getProperty('m3.test.total_limit_callback_delay_ms')
        if (testDelay != null && !testDelay.isEmpty()) {
            this.testTotalCallbackDelayMs = Long.parseLong(testDelay)
        }
        validate()
        scheduler = new ScheduledThreadPoolExecutor(1, new ThreadFactory() {
            Thread newThread(Runnable runnable) {
                Thread thread = new Thread(runnable, 'm3-watchdog')
                thread.setDaemon(true)
                return thread
            }
        } as ThreadFactory)
        ((ScheduledThreadPoolExecutor) scheduler).setRemoveOnCancelPolicy(true)
    }

    private static String required(Properties properties, String name) {
        String value = properties.getProperty(name)
        if (value == null || value.isEmpty()) {
            throw new IllegalStateException("필수 실행 속성 없음: " + name)
        }
        return value
    }

    private static int intValue(Properties properties, String name) {
        String value = required(properties, name)
        int parsed = Integer.parseInt(value)
        if (parsed <= 0) {
            throw new IllegalStateException("양수여야 하는 속성: " + name + "=" + value)
        }
        return parsed
    }

    private void validate() {
        if (!(target in ['http', 'jdbc'])) {
            throw new IllegalStateException("대상은 http 또는 jdbc: " + target)
        }
        if (!(c in [1, 10, 50, 100])) {
            throw new IllegalStateException("동시성은 1·10·50·100: " + c)
        }
        if (n <= 0) {
            throw new IllegalStateException("N은 1 이상: " + n)
        }
        if (warmupTotal != 100 || warmupPerThread * c != warmupTotal) {
            throw new IllegalStateException("워밍업은 전체 100건, 주체별 100/C: "
                    + warmupTotal + ", " + warmupPerThread + ", C=" + c)
        }
        if (requestTimeoutNs <= 0 || totalLimitNs <= 0 || prepLimitNs <= 0 || finishLimitNs <= 0) {
            throw new IllegalStateException("시간 한도는 모두 양수여야 한다")
        }
    }

    long prepDeadlineNs() {
        return setupStartNs + prepLimitNs
    }

    synchronized boolean isPreparationOpen() {
        return !prepFailed && !stopped
    }

    synchronized boolean measurementAllowed() {
        if (!started || stopped || prepFailed) {
            return false
        }
        if (System.nanoTime() >= totalDeadlineNs()) {
            limitHit = true
            stop('total limit reached')
            return false
        }
        return true
    }

    private long totalDeadlineNs() {
        return startNs + totalLimitNs
    }

    synchronized void failPreparation(String reason) {
        if (!prepFailed) {
            prepFailed = true
            prepFailure = String.valueOf(reason)
            log.error('M3 preparation failed: {}', prepFailure)
            notifyAll()
        }
    }

    synchronized void stop(String reason) {
        if (!stopped) {
            stopped = true
            stopReason = String.valueOf(reason)
            log.warn('M3 measurement stopped: {}', stopReason)
            notifyAll()
        }
    }

    synchronized void warmupVerified(String threadName, int index, boolean ok, String detail) {
        if (!ok) {
            failPreparation("warmup " + threadName + "#" + index + ": " + detail)
            return
        }
        String key = threadName + "|" + index
        if (!warmupCounts.containsKey(key)) {
            warmupCounts.put(key, 1)
            warmupCompleted++
        }
    }

    synchronized void recordConnection(String threadName, String connectionId) {
        if (connectionId != null && !connectionId.isEmpty()) {
            connectionIds.put(threadName, connectionId)
        }
    }

    synchronized void recordConnectionAfter(String threadName, String connectionId) {
        if (connectionId == null || connectionId.isEmpty()) {
            failPreparation('connection-id-after: 조회 결과 없음')
            return
        }
        connectionIdsAfter.put(threadName, connectionId)
        String before = connectionIds.get(threadName)
        if (before == null || before != connectionId) {
            failPreparation('connection-id-after: 측정 중 연결이 바뀌었다 ' + threadName)
        }
    }

    synchronized boolean threadReady(String threadName) {
        int verified = warmupCounts.keySet().count { it.startsWith(threadName + "|") }
        if (verified != warmupPerThread) {
            failPreparation("warmup count " + threadName + ": " + verified + "/" + warmupPerThread)
            return false
        }
        readyThreads.add(threadName)
        notifyAll()
        return true
    }

    synchronized boolean waitAllReady(long deadlineNs) {
        while (!prepFailed && readyThreads.size() < c) {
            long remaining = deadlineNs - System.nanoTime()
            if (remaining <= 0) {
                failPreparation("ready wait timeout: " + readyThreads.size() + "/" + c)
                return false
            }
            long waitMs = Math.min(1000L, (long) (remaining / 1_000_000L))
            wait(waitMs < 1L ? 1L : waitMs)
        }
        return !prepFailed && readyThreads.size() == c
    }

    synchronized void markResetVerified() {
        if (!prepFailed) {
            resetVerified = true
            notifyAll()
        }
    }

    synchronized boolean startMeasurement() {
        if (prepFailed) {
            return false
        }
        if (readyThreads.size() != c) {
            failPreparation("start with " + readyThreads.size() + "/" + c + " ready")
            return false
        }
        if (!resetVerified) {
            failPreparation("start without verified reset")
            return false
        }
        if (started) {
            return true
        }
        started = true
        startNs = System.nanoTime()
        if (totalFuture == null) {
            if (testTotalCallbackDelayMs > 0) {
                // 시험 전용: 단일 감시 스케줄러를 막지 않도록 callback만 별도 daemon으로
                // 늦춘다. 실제 실행은 아래 단일 스케줄러 경로만 사용한다.
                Thread delayed = new Thread(new Runnable() {
                    void run() {
                        try {
                            Thread.sleep(testTotalCallbackDelayMs)
                        } catch (InterruptedException ignored) {
                            return
                        }
                        onTotalLimit()
                    }
                }, 'm3-test-total-delay')
                delayed.setDaemon(true)
                delayed.start()
            } else {
                totalFuture = ((ScheduledThreadPoolExecutor) scheduler).schedule(new Runnable() {
                    void run() {
                        onTotalLimit()
                    }
                }, totalLimitNs, TimeUnit.NANOSECONDS)
            }
        }
        notifyAll()
        log.info('M3 measurement start: run={} target={} c={} n={}', runId, target, c, n)
        return true
    }

    synchronized boolean waitStarted(long deadlineNs) {
        while (!started && !prepFailed) {
            long remaining = deadlineNs - System.nanoTime()
            if (remaining <= 0) {
                failPreparation("start wait timeout")
                return false
            }
            long waitMs = Math.min(1000L, (long) (remaining / 1_000_000L))
            wait(waitMs < 1L ? 1L : waitMs)
        }
        return started && !prepFailed
    }

    synchronized Map beginCall(String threadName, int sequence) {
        if (!started || stopped || prepFailed) {
            return null
        }
        long now = System.nanoTime()
        // 총한도 callback이 늦어도 새 요청을 배분하지 않는다.
        if (now >= totalDeadlineNs()) {
            limitHit = true
            stop('total limit reached')
            return null
        }
        String key = threadName + "|" + sequence
        if (records.containsKey(key)) {
            throw new IllegalStateException("duplicate measurement call: " + key)
        }
        long deadline = now + requestTimeoutNs
        Map record = [
                run_id: runId,
                phase: 'measure',
                thread_name: threadName,
                request_sequence: sequence,
                start_ns: now,
                deadline_ns: deadline,
                decision_ns: null,
                observed_decision_ns: null,
                return_ns: null,
                outcome: null,
                processing_unknown: false,
                client_elapsed_ns: null,
                observed_elapsed_ns: null,
                late_return: false,
                http_code: null,
                jdbc_affected: null,
                error: '',
        ]
        records.put(key, record)
        inFlight++
        maxInFlight = Math.max(maxInFlight, inFlight)
        Object future = ((ScheduledThreadPoolExecutor) scheduler).schedule(new Runnable() {
            void run() {
                onDeadline(key)
            }
        }, (long) Math.max(0L, deadline - now), TimeUnit.NANOSECONDS)
        deadlineFutures.put(key, future)
        return record
    }

    private synchronized void markDeadline(String key, Map record, String outcome,
                                           long governingDeadline, long observed) {
        if (record.decision_ns != null) {
            return
        }
        Object future = deadlineFutures.remove(key)
        if (future != null) {
            ((java.util.concurrent.Future) future).cancel(false)
        }
        long start = ((Number) record.start_ns).longValue()
        // beginCall이 start >= total을 막지만, 음수 구간이 생기지 않도록 한 번 더 막는다.
        long decision = Math.max(governingDeadline, start)
        record.outcome = outcome
        record.decision_ns = decision
        record.observed_decision_ns = Math.max(observed, decision)
        record.processing_unknown = true
        record.client_elapsed_ns = null
        record.late_return = record.return_ns != null
                && ((Number) record.return_ns).longValue() > decision
        record.error = (outcome == 'unfinished') ? 'total limit' : 'request deadline'
        if (outcome == 'unfinished') {
            limitHit = true
        }
        decide(key, record)
        if (outcome == 'unfinished') {
            stop('total limit reached')
        } else {
            stop('request deadline: ' + key)
        }
    }

    private synchronized void onDeadline(String key) {
        try {
            Map record = records.get(key)
            if (record == null || record.decision_ns != null) {
                return
            }
            long requestDeadline = ((Number) record.deadline_ns).longValue()
            long totalDeadline = totalDeadlineNs()
            long effective = Math.min(requestDeadline, totalDeadline)
            if (record.return_ns != null
                    && ((Number) record.return_ns).longValue() <= effective) {
                return
            }
            boolean totalFirst = totalDeadline <= requestDeadline
            markDeadline(key, record, totalFirst ? 'unfinished' : 'timeout',
                    totalFirst ? totalDeadline : requestDeadline,
                    System.nanoTime())
        } catch (Throwable error) {
            log.error('M3 watchdog failure: {}', error.toString())
            stop('watchdog failure: ' + error.toString())
        }
    }

    private synchronized void onTotalLimit() {
        try {
            if (stopped || measurementComplete || totalFired) {
                return
            }
            totalFired = true
            limitHit = true
            long limit = totalDeadlineNs()
            for (Map.Entry<String, Map> entry : records.entrySet()) {
                Map record = entry.value
                if (record.decision_ns != null) {
                    continue
                }
                if (record.return_ns != null) {
                    long returned = ((Number) record.return_ns).longValue()
                    // 총한도 전 반환은 분류가 늦어도 PostProcessor가 판정한다.
                    if (returned <= limit) {
                        continue
                    }
                }
                markDeadline(entry.key, record, 'unfinished', limit,
                        System.nanoTime())
            }
            if (!stopped) {
                stop('total limit reached')
            }
        } catch (Throwable error) {
            log.error('M3 total limit failure: {}', error.toString())
            stop('total limit failure: ' + error.toString())
        }
    }

    private void decide(String key, Map record) {
        // 기록 생성 시 inFlight를 이미 올렸으므로 여기서 한 번만 내린다.
        inFlight = Math.max(0, inFlight - 1)
        long decision = ((Number) record.decision_ns).longValue()
        if (decision > lastDecisionNs) {
            lastDecisionNs = decision
            endNs = decision
        }
        completeMeasurementIfDecided()
    }

    private void completeMeasurementIfDecided() {
        if (measurementComplete || records.size() != n) {
            return
        }
        for (Map record : records.values()) {
            if (record.decision_ns == null) {
                return
            }
        }
        measurementComplete = true
        if (totalFuture != null) {
            ((java.util.concurrent.Future) totalFuture).cancel(false)
            totalFuture = null
        }
        notifyAll()
        log.info('M3 measurement decisions complete: {} calls', records.size())
    }

    synchronized long markReturn(String threadName, int sequence) {
        String key = threadName + "|" + sequence
        Map record = records.get(key)
        if (record == null) {
            throw new IllegalStateException("unknown measurement call: " + key)
        }
        long now = System.nanoTime()
        if (record.return_ns == null) {
            record.return_ns = now
            record.observed_elapsed_ns = now - ((Number) record.start_ns).longValue()
        }
        return ((Number) record.return_ns).longValue()
    }

    synchronized Map finishCall(String threadName, int sequence, Map classification) {
        String key = threadName + "|" + sequence
        Map record = records.get(key)
        if (record == null) {
            throw new IllegalStateException("unknown measurement call: " + key)
        }
        long now = record.return_ns == null ? System.nanoTime() : ((Number) record.return_ns).longValue()
        if (record.return_ns == null) {
            record.return_ns = now
            record.observed_elapsed_ns = now - ((Number) record.start_ns).longValue()
        }
        Object future = deadlineFutures.remove(key)
        if (future != null) {
            ((java.util.concurrent.Future) future).cancel(false)
        }
        record.http_code = classification.http_code
        record.jdbc_affected = classification.jdbc_affected
        if (record.decision_ns != null) {
            record.late_return = true
            String detail = classification.error ?: ''
            record.error = (record.error ? record.error + '; ' : '') + 'late return ' + detail
            return record
        }
        long requestDeadline = ((Number) record.deadline_ns).longValue()
        long totalDeadline = totalDeadlineNs()
        long effectiveDeadline = Math.min(requestDeadline, totalDeadline)
        if (now > effectiveDeadline) {
            boolean totalFirst = totalDeadline <= requestDeadline
            markDeadline(key, record, totalFirst ? 'unfinished' : 'timeout',
                    totalFirst ? totalDeadline : requestDeadline, now)
            return record
        }
        record.outcome = classification.outcome
        record.processing_unknown = classification.processing_unknown == true
        record.decision_ns = now
        record.observed_decision_ns = now
        record.client_elapsed_ns = (record.outcome == 'success') ? (now - ((Number) record.start_ns).longValue()) : null
        record.error = classification.error ?: ''
        decide(key, record)
        if (record.processing_unknown) {
            stop('processing unknown: ' + key + ' ' + record.error)
        }
        return record
    }

    synchronized boolean measurementAllSuccess() {
        if (!measurementComplete || records.size() != n) {
            return false
        }
        for (Map record : records.values()) {
            if (record.outcome != 'success' || record.processing_unknown == true) {
                return false
            }
        }
        return true
    }

    synchronized boolean awaitMeasurementComplete() {
        long deadline = totalDeadlineNs()
        while (!measurementComplete && !stopped && !prepFailed) {
            long remaining = deadline - System.nanoTime()
            if (remaining <= 0) {
                limitHit = true
                stop('total limit reached')
                return false
            }
            long waitMs = Math.min(1000L, (long) (remaining / 1_000_000L))
            wait(waitMs < 1L ? 1L : waitMs)
        }
        if (!measurementComplete || stopped || prepFailed) {
            return false
        }
        return measurementAllSuccess()
    }

    synchronized int nextMeasureSequence(String threadName) {
        int next = (measureSequences.get(threadName) ?: 0) + 1
        measureSequences.put(threadName, next)
        return next
    }

    synchronized void setFinalRead(boolean done, boolean ok, Object value) {
        finalReadDone = done
        finalReadOk = ok
        finalValue = (value == null) ? null : String.valueOf(value)
    }

    synchronized boolean allDecided() {
        for (Map record : records.values()) {
            if (record.decision_ns == null) {
                return false
            }
        }
        return true
    }

    synchronized Map counts() {
        Map<String, Integer> result = [success: 0, rejected: 0, timeout: 0,
                                       other_failure: 0, unfinished: 0]
        int unknown = 0
        for (Map record : records.values()) {
            String outcome = record.outcome
            if (result.containsKey(outcome)) {
                result[outcome] = result[outcome] + 1
            }
            if (record.processing_unknown == true) {
                unknown++
            }
        }
        result.processing_unknown = unknown
        result.attempts = records.size()
        return result
    }

    synchronized void shutdownScheduler() {
        if (totalFuture != null) {
            ((java.util.concurrent.Future) totalFuture).cancel(false)
            totalFuture = null
        }
        if (scheduler != null) {
            ((ScheduledThreadPoolExecutor) scheduler).shutdownNow()
        }
    }

    synchronized void writeCallsAndCompletion() {
        new File(runDir).mkdirs()
        List<Map> ordered = new ArrayList<>(records.values())
        ordered.sort { a, b ->
            int byStart = Long.compare(((Number) a.start_ns).longValue(), ((Number) b.start_ns).longValue())
            if (byStart != 0) {
                return byStart
            }
            int byThread = String.valueOf(a.thread_name) <=> String.valueOf(b.thread_name)
            return byThread != 0 ? byThread : Integer.compare((int) a.request_sequence, (int) b.request_sequence)
        }
        List<String> columns = ['run_id', 'phase', 'thread_name', 'request_sequence',
                                'start_ns', 'deadline_ns', 'decision_ns', 'observed_decision_ns',
                                'return_ns', 'outcome', 'processing_unknown', 'client_elapsed_ns',
                                'observed_elapsed_ns', 'late_return', 'http_code',
                                'jdbc_affected', 'error']
        StringBuilder calls = new StringBuilder()
        calls.append(columns.join(',')).append('\n')
        for (Map record : ordered) {
            calls.append(columns.collect { csv(record[it]) }.join(',')).append('\n')
        }
        writeAtomic(new File(callsFile), calls.toString())

        Map counts = counts()
        int decided = 0
        for (Map record : records.values()) {
            if (record.decision_ns != null) {
                decided++
            }
        }
        boolean normal = started && !prepFailed && !stopped && !limitHit
                && counts.attempts == n && counts.success == n
                && counts.rejected == 0 && counts.timeout == 0
                && counts.other_failure == 0 && counts.unfinished == 0
                && counts.processing_unknown == 0
                && decided == counts.attempts
                && warmupCompleted == warmupTotal
                && finalReadDone && finalReadOk
                && finalValue != null && finalValue == String.valueOf(n)
        Map completion = [
                normal: normal,
                run_id: runId,
                target: target,
                concurrency: c,
                planned_n: n,
                attempts: counts.attempts,
                decided: decided,
                success: counts.success,
                rejected: counts.rejected,
                timeout: counts.timeout,
                other_failure: counts.other_failure,
                unfinished: counts.unfinished,
                processing_unknown: counts.processing_unknown,
                max_in_flight: maxInFlight,
                warmup_expected: warmupTotal,
                warmup_completed: warmupCompleted,
                warmup_per_thread: warmupPerThread,
                start_ns: startNs == 0L ? null : startNs,
                end_ns: endNs == 0L ? null : endNs,
                final_read_done: finalReadDone,
                final_read_ok: finalReadOk,
                final_value: finalValue == null ? null : Long.valueOf(finalValue),
                stop_reason: stopReason,
                limit_hit: limitHit,
                prep_failed: prepFailed,
                prep_failure: prepFailure,
                records_written: ordered.size(),
                connection_ids: connectionIds,
                connection_ids_after: connectionIdsAfter,
                connection_identity_stable: connectionIds == connectionIdsAfter,
                distinct_connection_ids: connectionIds.values().toSet().size(),
                measurement_complete: measurementComplete,
                total_fired: totalFired,
                jmeter_version: org.apache.jmeter.util.JMeterUtils.getJMeterVersion(),
        ]
        writeAtomic(new File(completionFile), JsonOutput.prettyPrint(JsonOutput.toJson(completion)))
    }

    private static String csv(Object value) {
        if (value == null) {
            return ''
        }
        String text = String.valueOf(value)
        if (value instanceof Boolean) {
            text = value ? 'true' : 'false'
        }
        if (text.length() > 500) {
            text = text.substring(0, 500) + '...'
        }
        if (text.contains(',') || text.contains('"') || text.contains('\n') || text.contains('\r')) {
            return '"' + text.replace('"', '""') + '"'
        }
        return text
    }

    private static void writeAtomic(File target, String text) {
        File temporary = new File(target.getParentFile(), target.getName() + '.tmp')
        temporary.setText(text, 'UTF-8')
        if (target.exists() && !target.delete()) {
            throw new IllegalStateException("기존 결과 파일을 교체할 수 없음: " + target)
        }
        if (!temporary.renameTo(target)) {
            throw new IllegalStateException("결과 파일을 확정할 수 없음: " + target)
        }
    }
}

// ---------------------------------------------------------------------------
// 응답 판정
// ---------------------------------------------------------------------------

static Map parseJsonObject(String body) {
    Object parsed = new JsonSlurper().parseText(body)
    if (!(parsed instanceof Map)) {
        throw new IllegalArgumentException("JSON 객체가 아님")
    }
    return (Map) parsed
}

static String errorText(def previous) {
    String message = previous == null ? null : previous.getResponseMessage()
    return message == null ? '' : message
}

static Map classifyHttp(def previous) {
    int code = 0
    try {
        code = Integer.parseInt(String.valueOf(previous.getResponseCode()))
    } catch (Exception ignored) {
        code = 0
    }
    String body = previous.getResponseDataAsString()
    if (code == 200) {
        try {
            Map parsed = parseJsonObject(body)
            if (parsed.keySet() == (['ok', 'value'] as Set)
                    && parsed.ok instanceof Boolean && parsed.ok == true
                    && parsed.value instanceof Number && !(parsed.value instanceof Boolean)) {
                return [outcome: 'success', processing_unknown: false,
                        http_code: code, jdbc_affected: null, error: '']
            }
        } catch (Exception ignored) {
            // 아래에서 처리 여부 불명확으로 분류한다.
        }
        return [outcome: 'other_failure', processing_unknown: true,
                http_code: code, jdbc_affected: null,
                error: '200 with invalid success body']
    }
    if (code in [400, 404, 409, 422]) {
        return [outcome: 'rejected', processing_unknown: false,
                http_code: code, jdbc_affected: null,
                error: 'rejected HTTP ' + code]
    }
    return [outcome: 'other_failure', processing_unknown: true,
            http_code: code, jdbc_affected: null,
            error: 'HTTP ' + code + ' ' + errorText(previous)]
}

static Integer affectedRows(def previous) {
    String body = previous.getResponseDataAsString()
    java.util.regex.Matcher matcher = (body =~ /^\s*(\d+)\s+updates/)
    if (matcher.find()) {
        return Integer.parseInt(matcher.group(1))
    }
    String message = errorText(previous)
    matcher = (message =~ /^\s*(\d+)\s+updates/)
    if (matcher.find()) {
        return Integer.parseInt(matcher.group(1))
    }
    return null
}

static Map classifyJdbc(def previous) {
    if (!previous.isSuccessful()) {
        return [outcome: 'other_failure', processing_unknown: true,
                http_code: null, jdbc_affected: null,
                error: 'JDBC error: ' + errorText(previous)]
    }
    Integer affected = affectedRows(previous)
    if (affected == null) {
        return [outcome: 'other_failure', processing_unknown: true,
                http_code: null, jdbc_affected: null,
                error: 'affected rows not observed']
    }
    if (affected == 1) {
        return [outcome: 'success', processing_unknown: false,
                http_code: null, jdbc_affected: affected, error: '']
    }
    // 0·2 같은 값은 완료 응답이므로 처리 여부는 명확하지만 성공은 아니다.
    return [outcome: 'other_failure', processing_unknown: false,
            http_code: null, jdbc_affected: affected,
            error: 'affected rows ' + affected]
}

static boolean httpOk(def previous) {
    if (!previous.isSuccessful()) {
        return false
    }
    try {
        Map parsed = parseJsonObject(previous.getResponseDataAsString())
        return parsed.keySet() == (['ok'] as Set)
                && parsed.ok instanceof Boolean && parsed.ok == true
    } catch (Exception ignored) {
        return false
    }
}

static Long httpZeroValue(def previous, int expected) {
    if (!previous.isSuccessful()) {
        return null
    }
    try {
        Map parsed = parseJsonObject(previous.getResponseDataAsString())
        if (parsed.keySet() != (['found', 'value'] as Set)) {
            return null
        }
        if (!(parsed.found instanceof Boolean) || parsed.found != true) {
            return null
        }
        if (!(parsed.value instanceof Number) || parsed.value instanceof Boolean) {
            return null
        }
        BigDecimal actual
        try {
            actual = new BigDecimal(String.valueOf(parsed.value))
        } catch (Exception ignored) {
            return null
        }
        return actual.compareTo(BigDecimal.valueOf((long) expected)) == 0 ? (long) expected : null
    } catch (Exception ignored) {
        return null
    }
}

static Integer jdbcRowCount(def vars) {
    String count = vars.get('m3_final_value_#')
    if (count == null) {
        return null
    }
    try {
        return Integer.parseInt(count)
    } catch (NumberFormatException ignored) {
        return null
    }
}

static Long jdbcValue(def vars) {
    if (jdbcRowCount(vars) != 1) {
        return null
    }
    String value = vars.get('m3_final_value_1')
    if (value == null) {
        return null
    }
    try {
        return Long.parseLong(value.trim())
    } catch (NumberFormatException ignored) {
        return null
    }
}

static void putRecordVars(def vars, Map record) {
    vars.put('m3_run_id', String.valueOf(record.run_id))
    vars.put('m3_phase', String.valueOf(record.phase))
    vars.put('m3_thread_name', String.valueOf(record.thread_name))
    vars.put('m3_request_sequence', String.valueOf(record.request_sequence))
    vars.put('m3_start_ns', String.valueOf(record.start_ns))
    vars.put('m3_deadline_ns', String.valueOf(record.deadline_ns))
    vars.put('m3_decision_ns', record.decision_ns == null ? '' : String.valueOf(record.decision_ns))
    vars.put('m3_observed_decision_ns', record.observed_decision_ns == null ? '' : String.valueOf(record.observed_decision_ns))
    vars.put('m3_return_ns', record.return_ns == null ? '' : String.valueOf(record.return_ns))
    vars.put('m3_outcome', record.outcome == null ? '' : String.valueOf(record.outcome))
    vars.put('m3_processing_unknown', record.processing_unknown == true ? 'true' : 'false')
    vars.put('m3_client_elapsed_ns', record.client_elapsed_ns == null ? '' : String.valueOf(record.client_elapsed_ns))
    vars.put('m3_observed_elapsed_ns', record.observed_elapsed_ns == null ? '' : String.valueOf(record.observed_elapsed_ns))
    vars.put('m3_late_return', record.late_return == true ? 'true' : 'false')
    vars.put('m3_http_code', record.http_code == null ? '' : String.valueOf(record.http_code))
    vars.put('m3_jdbc_affected', record.jdbc_affected == null ? '' : String.valueOf(record.jdbc_affected))
    vars.put('m3_error', record.error == null ? '' : String.valueOf(record.error))
}

// ---------------------------------------------------------------------------
// 단계 실행
// ---------------------------------------------------------------------------

def coordinator = props.get('m3_coordinator')
def stage = (Parameters ?: '').trim()
String threadName = ctx.getThread().getThreadName()

switch (stage) {
    case 'setup':
        if (coordinator != null) {
            throw new IllegalStateException('이미 준비된 실행이 있다')
        }
        def created = new M3Coordinator(props, log)
        props.put('m3_coordinator', created)
        vars.put('m3_run_id', created.runId)
        vars.put('m3_phase', 'prepare')
        log.info('M3 setup: run={} target={}', created.runId, created.target)
        break

    case 'thread-prepare':
        if (coordinator == null) {
            throw new IllegalStateException('공유 조율 상태가 없다')
        }
        vars.put('m3_phase', 'prepare')
        vars.put('m3_thread_name', threadName)
        // 이 JMeter 5.6.3에서 getThreadNum()은 0부터 시작한다. 이름의 마지막
        // 실행 주체 번호를 기준으로 대표 하나를 고정해 버전 차이에 덜 의존한다.
        boolean representative = threadName.endsWith('-1')
        vars.put('m3_is_representative', representative ? 'true' : 'false')
        log.debug('M3 thread prepare: {} num={} representative={}',
                threadName, ctx.getThread().getThreadNum(), representative)
        vars.put('m3_warmup_sequence', '0')
        vars.put('m3_measure_seq', '0')
        // 완료 장벽을 통과하지 못하면 after 연결 조회를 절대 실행하지 않는다.
        vars.put('m3_after_allowed', 'false')
        break

    case 'warmup-pre':
        if (coordinator == null) {
            throw new IllegalStateException('공유 조율 상태가 없다')
        }
        if (!coordinator.isPreparationOpen()) {
            throw new JMeterStopThreadException('워밍업 중단: ' + coordinator.prepFailure)
        }
        int warmupSequence = (vars.get('m3_warmup_sequence') as int) + 1
        vars.put('m3_warmup_sequence', String.valueOf(warmupSequence))
        vars.put('m3_phase', 'warmup')
        vars.put('m3_thread_name', threadName)
        vars.put('m3_request_sequence', String.valueOf(warmupSequence))
        vars.put('m3_warmup_start_ns', String.valueOf(System.nanoTime()))
        break

    case 'warmup-post-http':
    case 'warmup-post-jdbc':
        Map warmupClassification = stage.endsWith('http')
                ? classifyHttp(prev) : classifyJdbc(prev)
        boolean warmupOk = warmupClassification.outcome == 'success'
                && warmupClassification.processing_unknown == false
        vars.put('m3_phase', 'warmup')
        vars.put('m3_outcome', warmupClassification.outcome)
        vars.put('m3_processing_unknown', warmupClassification.processing_unknown ? 'true' : 'false')
        vars.put('m3_error', warmupClassification.error ?: '')
        if (!warmupOk) {
            prev.setSuccessful(false)
            prev.setResponseMessage('warmup failed: ' + warmupClassification.error)
        }
        coordinator.warmupVerified(threadName,
                vars.get('m3_request_sequence') as int, warmupOk,
                warmupClassification.error)
        break

    case 'connection-id':
        if (coordinator == null) {
            throw new IllegalStateException('공유 조율 상태가 없다')
        }
        String connectionCount = vars.get('m3_connection_id_#')
        String connectionValue = vars.get('m3_connection_id_1')
        if (!prev.isSuccessful() || connectionCount != '1' || connectionValue == null) {
            coordinator.failPreparation('connection-id: 조회 결과 없음')
            prev.setSuccessful(false)
            prev.setResponseMessage('connection id not observed')
            break
        }
        try {
            Long.parseLong(connectionValue.trim())
        } catch (NumberFormatException ignored) {
            coordinator.failPreparation('connection-id: 수치 아님')
            prev.setSuccessful(false)
            prev.setResponseMessage('connection id is not numeric')
            break
        }
        coordinator.recordConnection(threadName, connectionValue.trim())
        vars.put('m3_connection_id', connectionValue.trim())
        vars.put('m3_phase', 'prepare')
        break

    case 'connection-id-after':
        if (coordinator == null) {
            throw new IllegalStateException('공유 조율 상태가 없다')
        }
        String afterCount = vars.get('m3_connection_id_after_#')
        String afterValue = vars.get('m3_connection_id_after_1')
        if (!prev.isSuccessful() || afterCount != '1' || afterValue == null) {
            coordinator.failPreparation('connection-id-after: 조회 결과 없음')
            prev.setSuccessful(false)
            prev.setResponseMessage('connection id not observed after measurement')
            break
        }
        try {
            Long.parseLong(afterValue.trim())
        } catch (NumberFormatException ignored) {
            coordinator.failPreparation('connection-id-after: 수치 아님')
            prev.setSuccessful(false)
            prev.setResponseMessage('connection id after is not numeric')
            break
        }
        coordinator.recordConnectionAfter(threadName, afterValue.trim())
        vars.put('m3_connection_id_after', afterValue.trim())
        vars.put('m3_phase', 'verify')
        break

    case 'wait-complete':
        if (coordinator == null) {
            throw new IllegalStateException('공유 조율 상태가 없다')
        }
        boolean afterAllowed = coordinator.awaitMeasurementComplete()
        vars.put('m3_after_allowed', afterAllowed ? 'true' : 'false')
        vars.put('m3_phase', 'verify')
        break

    case 'after-probe':
        String probeUrl = props.getProperty('m3.test.after_probe_url')
        if (probeUrl == null || probeUrl.isEmpty()) {
            throw new IllegalStateException('after-probe URL이 없다')
        }
        HttpURLConnection probe = (HttpURLConnection) new URL(probeUrl).openConnection()
        probe.setRequestMethod('GET')
        probe.setConnectTimeout(2000)
        probe.setReadTimeout(2000)
        try {
            int probeCode = probe.getResponseCode()
            if (probeCode != 200) {
                throw new IllegalStateException('after-probe HTTP ' + probeCode)
            }
        } finally {
            probe.disconnect()
        }
        vars.put('m3_phase', 'verify')
        break

    case 'warmup-complete':
        if (coordinator == null) {
            throw new IllegalStateException('공유 조율 상태가 없다')
        }
        if (!coordinator.threadReady(threadName)) {
            throw new IllegalStateException('워밍업 완료 실패: ' + coordinator.prepFailure)
        }
        if (vars.get('m3_is_representative') == 'true') {
            if (!coordinator.waitAllReady(coordinator.prepDeadlineNs())) {
                throw new IllegalStateException('전체 준비 완료 대기 실패: ' + coordinator.prepFailure)
            }
        }
        break

    case 'setup-http-put':
    case 'reset-http-put':
        boolean resetPutOk = httpOk(prev)
        if (!resetPutOk) {
            coordinator.failPreparation(stage + ': 초기화 응답 불일치')
            prev.setSuccessful(false)
            prev.setResponseMessage('reset response mismatch')
        }
        break

    case 'setup-http-get':
        Long setupValue = httpZeroValue(prev, 0)
        if (setupValue == null) {
            coordinator.failPreparation('setup-http-get: 0 확인 실패')
            prev.setSuccessful(false)
            prev.setResponseMessage('setup read mismatch')
        }
        break

    case 'reset-http-get':
        Long resetValue = httpZeroValue(prev, 0)
        if (resetValue == null) {
            coordinator.failPreparation('reset-http-get: 0 확인 실패')
            prev.setSuccessful(false)
            prev.setResponseMessage('reset read mismatch')
        } else {
            coordinator.markResetVerified()
        }
        break

    case 'setup-jdbc-insert':
        if (!prev.isSuccessful()) {
            coordinator.failPreparation('setup-jdbc-insert: ' + errorText(prev))
            prev.setSuccessful(false)
        }
        break

    case 'setup-jdbc-update':
        if (!prev.isSuccessful()) {
            coordinator.failPreparation('setup-jdbc-update: ' + errorText(prev))
            prev.setSuccessful(false)
        }
        break

    case 'setup-jdbc-get':
        Long jdbcSetupValue = jdbcValue(vars)
        if (jdbcSetupValue == null || jdbcSetupValue != 0L) {
            coordinator.failPreparation('setup-jdbc-get: 0 확인 실패')
            prev.setSuccessful(false)
            prev.setResponseMessage('setup select mismatch')
        }
        break

    case 'reset-jdbc-update':
        Integer resetAffected = affectedRows(prev)
        if (!prev.isSuccessful() || resetAffected == null || !(resetAffected in [0, 1])) {
            coordinator.failPreparation('reset-jdbc-update: ' + errorText(prev)
                    + ' affected=' + resetAffected)
            prev.setSuccessful(false)
            prev.setResponseMessage('reset update mismatch')
        }
        break

    case 'reset-jdbc-get':
        Long jdbcResetValue = jdbcValue(vars)
        if (jdbcResetValue == null || jdbcResetValue != 0L) {
            coordinator.failPreparation('reset-jdbc-get: 0 확인 실패')
            prev.setSuccessful(false)
            prev.setResponseMessage('reset select mismatch')
        } else {
            coordinator.markResetVerified()
        }
        break

    case 'start-gate':
        if (coordinator == null) {
            throw new IllegalStateException('공유 조율 상태가 없다')
        }
        log.debug('M3 start gate: {} representative={}', threadName,
                vars.get('m3_is_representative'))
        if (vars.get('m3_is_representative') == 'true') {
            if (!coordinator.startMeasurement()) {
                throw new IllegalStateException('측정 시작 거절: ' + coordinator.prepFailure)
            }
        } else if (!coordinator.waitStarted(coordinator.prepDeadlineNs())) {
            throw new IllegalStateException('측정 시작 대기 실패: ' + coordinator.prepFailure)
        }
        vars.put('m3_phase', 'measure')
        vars.put('m3_measure_seq', '0')
        break

    case 'measure-pre':
        if (coordinator == null) {
            throw new IllegalStateException('공유 조율 상태가 없다')
        }
        int sequence = coordinator.nextMeasureSequence(threadName)
        Map record = coordinator.beginCall(threadName, sequence)
        if (record == null) {
            throw new JMeterStopThreadException('측정 중단: ' + coordinator.stopReason)
        }
        vars.put('m3_phase', 'measure')
        vars.put('m3_thread_name', threadName)
        vars.put('m3_request_sequence', String.valueOf(sequence))
        vars.put('m3_start_ns', String.valueOf(record.start_ns))
        vars.put('m3_deadline_ns', String.valueOf(record.deadline_ns))
        break

    case 'measure-post-http':
    case 'measure-post-jdbc':
        // 결과 판정(JSON 해석·오류 문자열) 전에 반환 시각을 먼저 고정한다.
        long returnedAt = coordinator.markReturn(threadName,
                vars.get('m3_request_sequence') as int)
        // 시험 전용 분류 지연. 기본값은 없으며 결과 판정 경계를 격리 검증할 때만 쓴다.
        String classifyDelay = props.getProperty('m3.test.classification_delay_ms')
        if (classifyDelay != null && !classifyDelay.isEmpty()) {
            long delayMs = Long.parseLong(classifyDelay)
            if (delayMs > 0) {
                Thread.sleep(delayMs)
            }
        }
        Map classification = stage.endsWith('http')
                ? classifyHttp(prev) : classifyJdbc(prev)
        Map finished = coordinator.finishCall(threadName,
                vars.get('m3_request_sequence') as int, classification)
        putRecordVars(vars, finished)
        boolean success = finished.outcome == 'success'
        prev.setSuccessful(success)
        if (!success) {
            prev.setResponseMessage('M3 outcome=' + finished.outcome
                    + ' unknown=' + finished.processing_unknown
                    + ' error=' + finished.error)
        }
        break

    case 'final-http':
        Long finalHttpValue = httpZeroValue(prev, vars.get('m3_expected_final') as int)
        boolean finalHttpOk = prev.isSuccessful() && finalHttpValue != null
        vars.put('m3_final_value', finalHttpValue == null ? '' : String.valueOf(finalHttpValue))
        vars.put('m3_final_read_ok', finalHttpOk ? 'true' : 'false')
        if (!finalHttpOk) {
            prev.setSuccessful(false)
            prev.setResponseMessage('최종 조회 응답 불일치')
        }
        break

    case 'final-jdbc':
        Long finalJdbcValue = jdbcValue(vars)
        boolean finalJdbcOk = prev.isSuccessful() && finalJdbcValue != null
        vars.put('m3_final_value', finalJdbcValue == null ? '' : String.valueOf(finalJdbcValue))
        vars.put('m3_final_read_ok', finalJdbcOk ? 'true' : 'false')
        if (!finalJdbcOk) {
            prev.setSuccessful(false)
            prev.setResponseMessage('최종 SELECT 결과 불일치')
        }
        break

    case 'teardown-prepare':
        boolean doRead = coordinator != null && coordinator.started
                && !coordinator.prepFailed && coordinator.allDecided()
        vars.put('m3_do_final_read', doRead ? 'true' : 'false')
        vars.put('m3_expected_final', String.valueOf(coordinator == null ? 0 : coordinator.n))
        vars.put('m3_phase', 'verify')
        break

    case 'teardown-write':
        if (coordinator == null) {
            throw new IllegalStateException('공유 조율 상태가 없다')
        }
        boolean doRead = vars.get('m3_do_final_read') == 'true'
        String rawValue = vars.get('m3_final_value')
        Long observed = (rawValue == null || rawValue.isEmpty()) ? null : Long.valueOf(rawValue)
        boolean readOk = doRead && vars.get('m3_final_read_ok') == 'true'
        coordinator.setFinalRead(doRead, readOk, observed)
        coordinator.writeCallsAndCompletion()
        coordinator.shutdownScheduler()
        vars.put('m3_phase', 'verify')
        vars.put('m3_outcome', readOk ? 'success' : 'other_failure')
        vars.put('m3_processing_unknown', 'false')
        break

    default:
        throw new IllegalStateException('알 수 없는 M3 단계: ' + stage)
}
