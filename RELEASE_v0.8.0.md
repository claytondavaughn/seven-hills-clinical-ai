# v0.8.0 — Permanent Patient Intelligence (development candidate)

## Changes
- Preserves Cognito authorization-code PKCE, API Gateway JWT authorizer, administrator group check, existing clinical validation and OpenAI narrative generation. Direct events without authenticated API context now receive HTTP 401.
- Uses the existing POST /review endpoint. Legacy review (no action or action=review) remains available to authenticated callers.
- New actions: list_patients, create_patient, set_patient_status, get_chart, save_draft, review_visit, finalize_visit.
- DynamoDB partition key USER#<Cognito sub>. Sort keys PROFILE#<patient UUID> and VISIT#<patient UUID>#<visit UUID>. All access uses server-derived identity, never a client-provided owner or partition key. Each user sees only their own development records.
- Patient profiles store demographics and diagnosis; directory supports name/diagnosis search and active/inactive filters. No deletion.
- Drafts persist structured findings and original form values. Conditional writes enforce draft versions; duplicate IDs and stale edits return 409. Final records are immutable through the application. Created, updated and finalized timestamps/actor IDs are server-derived UTC.
- Review stores the exact server-produced narrative in a versioned draft. Finalization requires the same assessment and reviewed draft version and does not regenerate AI output. Finalized chart history includes dated findings and review.
- Earliest finalized admission is the baseline. Review calculates comparisons against the latest finalized encounter on or before the assessment date and admission baseline, where corresponding findings were measured. PPS, ADL dependence, MAC in cm, weight in lb and percentage, FAST and NYHA are supported. No absent value is imputed. Structured weight is included only when measured on the assessment date.
- Historical context is shown with its date. No real patient data or cloud modifications were used in implementation.

## Build and test locally (no AWS or OpenAI calls)
From the branch root with Python 3.12+ and Node.js installed:
    python3 -m unittest discover -s tests -v
    python3 -m py_compile lambda_function.py patient_store.py build_release.py
    node --check app.js
    python3 build_release.py

The unittest suite supplies fake boto3, DynamoDB, and review responses; it does not contact any cloud service. The build generates dist/seven-hills-lambda-v0.8.0.zip (lambda_function.py and patient_store.py at ZIP root) and dist/seven-hills-web-v0.8.0.zip (index.html and app.js at ZIP root), verifies ZIP contents and prints SHA256 hashes. The pull-request workflow runs only offline tests and packages artifacts; it does not deploy.

## Deployment — owner approval required; synthetic development only
1. Do not merge or deploy until reviewing PR and passing automated checks. Download existing Lambda code and current Amplify deployment ZIP as rollback backups. Record the current environment variables, handler, version, runtime and API route settings without copying secret values.
2. Verify existing DynamoDB table seven-hills-clinical-dev uses string PK/SK and that CLINICAL_TABLE_NAME points to it. Verify Lambda role's least-privilege access to this table (GetItem, PutItem, Query, UpdateItem) and GetSecretValue on the existing AI secret. Do not grant public Lambda invoke rights; confirm API Gateway is the only intended application invocation path. Do not create a new table or new paid service.
3. Verify existing Cognito issuer/client, access-token JWT authorizer on POST /review, Administrators membership, CORS and HTTPS configuration. Leave authentication configuration and OPENAI_SECRET_NAME unchanged. No secret values go in Git or the website.
4. Upload the Lambda ZIP to the existing development function, retaining Python 3.12 and handler lambda_function.lambda_handler. Existing API integration should stay unchanged. Check the package includes patient_store.py and that the environment variable is present. Do not use the old unauthenticated console test event; it now returns 401 by design.
5. Before updating Amplify, verify authenticated legacy review with fictional input through the existing website. Then upload the web ZIP to the existing Amplify development branch, with index.html at its root. Confirm the deployed version displays v0.8.0.
6. Synthetic smoke tests: sign in, confirm fictional-only use, create patient, save unfinished draft, sign out/in and resume; review and finalize admission; finalize later routine visit with different PPS/weight and confirm dated comparison; test stable routine visit, required FAST/NYHA, missing metric, status filters, wrong administrator group and expired token. Use a second authorized development user to confirm isolation. Check for errors and verify logs contain no clinical payloads. These manual reviews require owner approval and may incur normal existing-service usage; none were performed here.
7. Rollback: redeploy the prior Lambda ZIP/version and prior Amplify ZIP together. Preserve DynamoDB records; no destructive migration was introduced. Do not delete data or change IAM/Cognito settings to work around errors. Existing v0.7.3 frontend can still use legacy review with the v0.8.0 backend during staged deployment.

## Known limitations / blockers before production
- Development isolation is per user, not agency/tenant. Shared interdisciplinary charts, tenant roles, user provisioning, consent, retention policies and complete audit event logging are future work.
- Synthetic checkbox is a declaration, not a PHI detector. Do not enter real patient data. Security/privacy validation, applicable BAAs, clinical validation and production readiness remain unapproved.
- Finalization stores comparisons calculated at review time; simultaneous or backdated finalized visits may change the appropriate historical reference. Chart shows the recorded source IDs; no silent rewrite of immutable records. A transactional chart revision or explicit re-review requirement is needed for multi-user/concurrent production use.
- Query pagination is consumed internally; current development API returns the full chart/directory. Large-chart API response pagination and capacity testing are still needed.
- No live AWS configuration, IAM, Cognito login, CORS, DynamoDB semantics or OpenAI calls were verified here. Mocked tests do not replace an integration test. Existing API Gateway timeout may constrain AI calls.
- No automatic migration of old session-only records, which were never durable. The demo patient is no longer preloaded; create a fictional patient using the directory.
- Current workflow supports admission, routine, PRN and recertification; IDG, F2F and physician workflows remain roadmap items. Historical comparisons are documentary, not eligibility determinations.
- Existing public repository contains development configuration identifiers, not secret values; recommend making repository private separately with owner authorization.
