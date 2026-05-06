# Spring Boot integration-test harness for omnibank — POC findings 2026-05-06

## Goal

Make it easy for the RCA subagent (or any future test author) to write
an integration test that drives the actual user-reported HTTP flow
through the real Spring + JPA + DB stack, so AppMap recordings capture
the full call tree (Spring proxies, transactional boundaries, JDBC,
HTTP middleware) instead of a mock-based unit test that bypasses all
of that.

User's framing: *"It would be better to allow minor fixups to the
test cases or the submitted patch in the validation phase."*

Then: *"We need to provide and prescribe a way for the agent to run
the code in true integration mode. Otherwise it's going to be forced
to do a bunch of code analysis to figure out what focused test case
to write, obviating the need for -rca in the first place."*

Decision: build a `OmnibankIntegrationTest` base class in
`shared-testing` so any module's integration test can extend it and
get a fully-wired Spring slice with H2 + REST client out of the box.

## What's in the harness (committed to the snapshot)

`shared-testing/src/main/java/com/omnibank/shared/testing/integration/OmnibankIntegrationTest.java`

```java
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@TestPropertySource(properties = {
    "spring.datasource.url=jdbc:h2:mem:omnibank_it;MODE=PostgreSQL;DB_CLOSE_DELAY=-1;DATABASE_TO_LOWER=TRUE",
    "spring.datasource.driver-class-name=org.h2.Driver",
    "spring.datasource.username=sa",
    "spring.datasource.password=",
    "spring.jpa.database-platform=org.hibernate.dialect.H2Dialect",
    "spring.jpa.hibernate.ddl-auto=create-drop",
    "spring.flyway.enabled=false",
    "spring.security.user.name=test",
    "spring.security.user.password=test",
    "spring.main.allow-bean-definition-overriding=true",
    "spring.datasource.hikari.connection-init-sql=CREATE SCHEMA IF NOT EXISTS payments_hub; CREATE SCHEMA IF NOT EXISTS accounts_consumer; CREATE SCHEMA IF NOT EXISTS ledger; CREATE SCHEMA IF NOT EXISTS lending_corporate;"
})
public abstract class OmnibankIntegrationTest {
    @LocalServerPort protected int port;
    @Autowired protected TestRestTemplate http;
    protected String url(String path) { return "http://localhost:" + port + path; }
}
```

`shared-testing/build.gradle.kts` — added `spring-boot-starter-data-jpa`,
`spring-boot-starter-web`, `runtimeOnly("com.h2database:h2")`.

Each consuming module's `build.gradle.kts` adds two test deps:

```kotlin
testImplementation(project(":shared-testing"))
testRuntimeOnly(project(":app-bootstrap"))   // for OmnibankBeanConfig + OmnibankApplication
```

The `testRuntimeOnly(":app-bootstrap")` is the key trick: it makes
`OmnibankApplication` available on the test classpath WITHOUT a
compile-time cycle (app-bootstrap depends on every module).
`@SpringBootTest` auto-detects it via package walk.

## Test-author ergonomics

Once the harness is in place, an integration test is ~15 lines:

```java
class PaymentSubmissionIT extends OmnibankIntegrationTest {
    @BeforeEach void auth() { http = http.withBasicAuth("test", "test"); }

    @Test
    void single_post_returns_payment_id() {
        var req = new PaymentController.SubmitRequest(...);
        ResponseEntity<PaymentController.SubmitResponse> resp =
            http.postForEntity(url("/api/v1/payments"), req,
                               PaymentController.SubmitResponse.class);
        assertThat(resp.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(resp.getBody().paymentId()).isNotNull();
    }
}
```

`bin/record-appmap.sh :customer-portal-api:test --tests PaymentSubmissionIT`
captures HTTP middleware recordings (one per request), JDBC SQL,
exception traces, and labeled-method calls — the full picture.

## What works (verified end-to-end in /tmp/omnibank_h2_poc)

| Capability | Status |
|---|---|
| H2 in-memory in PostgreSQL mode | ✅ |
| Schema pre-creation via Hikari init-sql | ✅ |
| ddl-auto generates tables from JPA entities | ✅ |
| Spring Boot context loads (~5s) | ✅ |
| 47 JPA entities + their repositories registered | ✅ |
| Component-scan picks up all controllers (verified: 7 controllers, 25+ routes) | ✅ |
| Spring Security basic auth via test props | ✅ |
| TestRestTemplate hits the live port | ✅ |
| Cross-cutting beans (AchCutoffPolicy, CircuitBreaker, etc.) wired via app-bootstrap testRuntime | ✅ |
| Bean-override flag handles dual-app classpath cleanly | ✅ |

## What doesn't yet work — and why

**`PaymentSubmissionIT` returns 400 with `UNSUPPORTED_API_VERSION`
even after my @TestConfiguration override.** The
`ApiVersionRouter` filter in customer-portal-api is intercepting
`/api/v1/payments` and resolving it to v2 (the default
"latestVersion") which has no controllers. My `@Bean @Primary`
override of the router didn't take effect for unclear reasons —
possibly because the OncePerRequestFilter is registered as a
servlet filter via FilterRegistrationBean rather than a regular
bean, so @Primary doesn't reach the filter chain.

This is a customer-portal-api quirk, not a harness issue. Fixing
it likely requires either:
- A test-only profile that disables the filter outright
- Configuring the `latestVersion` via application property
  (`omnibank.api.latest-version=v1`) if the app supports it
- Sending a `Accept-Version: v1` header consistently

## Recommendations

### For the methodology change

The harness IS easy to use. Per-fixture test authors can write
~15-line integration tests. The main agent should:

1. Look for `OmnibankIntegrationTest` (or per-language equivalent)
   in shared-testing as the integration-test entry point.
2. Extend it. Write a `@Test` that drives the user-reported flow
   via `http.postForEntity(url("/api/..."), ...)`.
3. Run it with `bin/record-appmap.sh :module:test --tests
   <ITClassName>`. HTTP middleware does the recording shape work
   automatically.

This replaces the current "agent writes a unit test with mocks"
default path that BUG-0008's RCA took. It's prescribable in the
RCA prompt: *"if the bug describes a user-visible flow and an
HTTP endpoint exists for it, extend `OmnibankIntegrationTest` and
drive the endpoint — do not write a mock-based unit test."*

### For omnibank specifically (out-of-band cleanup)

- **`ApiVersionRouter` defaults are broken**: latestVersion=v2,
  no v2 controllers exist. Either change default to v1 or add
  v2 controllers. This is a real production bug, not a test
  artifact — would surface immediately if a customer hit
  `/api/v1/payments` in any environment that uses the default
  ApiVersionRouter constructor.
- **No `application.yaml` for tests**: Production has it but
  there's no test-specific override. Worth adding
  `application-test.yaml` to set `omnibank.api.latest-version=v1`
  and similar test-friendly defaults.

### For the experiment

Two paths:

- **(a)** Fix the ApiVersionRouter default to v1 (one-line change
  in `ApiVersionRouter.java`), commit it as part of the omnibank
  snapshot maintenance, then re-run BUG-0008 on Sonnet 3-step
  with the harness in place. We'd see whether RCA actually uses
  the integration test and whether the recording is materially
  better than the unit-test recording from the prior run.
- **(b)** Treat omnibank as a flawed reference codebase and shift
  fixture work to a more standard Spring app (or a Python/Django
  one where AppMap-Python's coverage is broader). The harness
  pattern stays the same; the implementation specifics differ
  per project.

## Files committed

- `<snapshot>/shared-testing/src/main/java/com/omnibank/shared/testing/integration/OmnibankIntegrationTest.java`
- `<snapshot>/shared-testing/build.gradle.kts` (added jpa, web, h2 deps)
- `<snapshot>/customer-portal-api/build.gradle.kts` (added testRuntimeOnly app-bootstrap)
