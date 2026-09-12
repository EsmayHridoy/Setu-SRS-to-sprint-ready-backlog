-- =====================================================================
-- Setu - PostgreSQL schema and seed data
--
-- Creates every table the application uses and fills it with the same
-- sample data the SQLite build seeds itself with.
--
--   createdb setu
--   psql -U setu -d setu -f db/migrations/V1__initial_schema.sql
--
-- Running this twice is safe: it drops the Setu tables first. It touches
-- nothing else in the database.
--
-- Generated from backend/app/seed.py - do not edit by hand.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- Clean slate. Order matters only for readability; CASCADE handles the
-- dependencies.
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS business_items  CASCADE;
DROP TABLE IF EXISTS business_plans  CASCADE;
DROP TABLE IF EXISTS citations       CASCADE;
DROP TABLE IF EXISTS messages        CASCADE;
DROP TABLE IF EXISTS conversations   CASCADE;
DROP TABLE IF EXISTS artifacts       CASCADE;
DROP TABLE IF EXISTS role_projects   CASCADE;
DROP TABLE IF EXISTS user_roles      CASCADE;
DROP TABLE IF EXISTS projects        CASCADE;
DROP TABLE IF EXISTS roles           CASCADE;
DROP TABLE IF EXISTS users           CASCADE;
DROP TABLE IF EXISTS audit_events    CASCADE;
DROP TABLE IF EXISTS jobs            CASCADE;


-- ---------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------
CREATE TABLE users (
    id          VARCHAR(36)  PRIMARY KEY,
    name        VARCHAR(120) NOT NULL,
    email       VARCHAR(200) NOT NULL UNIQUE,
    job_title   VARCHAR(120) NOT NULL DEFAULT '',
    is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMP    NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc')
);

CREATE TABLE roles (
    id          VARCHAR(36)  PRIMARY KEY,
    name        VARCHAR(120) NOT NULL UNIQUE,
    description TEXT         NOT NULL DEFAULT '',
    -- A flag rather than a magic role name, so renaming a role can never
    -- silently remove admin access.
    is_admin    BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMP    NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc')
);

CREATE TABLE user_roles (
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id VARCHAR(36) NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);


-- ---------------------------------------------------------------------
-- Projects
--
-- token_encrypted holds a Fernet ciphertext written by the application.
-- It is never seeded here and never returned by the API; only the last
-- four characters are exposed.
-- ---------------------------------------------------------------------
CREATE TABLE projects (
    id              VARCHAR(36)  PRIMARY KEY,
    name            VARCHAR(160) NOT NULL UNIQUE,
    description     TEXT         NOT NULL DEFAULT '',
    provider        VARCHAR(20)  NOT NULL DEFAULT 'GITHUB',
    repo_url        VARCHAR(500) NOT NULL DEFAULT '',
    default_branch  VARCHAR(80)  NOT NULL DEFAULT 'main',
    token_encrypted TEXT,
    token_last4     VARCHAR(8),
    token_set_at    TIMESTAMP,
    status          VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
    indexed_at      TIMESTAMP,
    created_at      TIMESTAMP    NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    CONSTRAINT projects_provider_check
        CHECK (provider IN ('GITHUB', 'GITLAB', 'MANUAL')),
    CONSTRAINT projects_status_check
        CHECK (status IN ('ACTIVE', 'DISABLED'))
);

CREATE TABLE role_projects (
    role_id    VARCHAR(36) NOT NULL REFERENCES roles(id)    ON DELETE CASCADE,
    project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, project_id)
);


-- ---------------------------------------------------------------------
-- Indexed knowledge
--
-- project_id is the column every future retrieval query must filter on.
-- Without it, anyone who can search the index can read code from a
-- project they were never granted.
-- ---------------------------------------------------------------------
CREATE TABLE artifacts (
    id         VARCHAR(36)  PRIMARY KEY,
    project_id VARCHAR(36)  NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind       VARCHAR(20)  NOT NULL,
    source_ref VARCHAR(400) NOT NULL,
    content    TEXT         NOT NULL DEFAULT '',
    keywords   TEXT         NOT NULL DEFAULT '',
    CONSTRAINT artifacts_kind_check
        CHECK (kind IN ('CODE', 'SCHEMA', 'RCA'))
);

CREATE INDEX ix_artifacts_project_id ON artifacts (project_id);


-- ---------------------------------------------------------------------
-- Conversations
-- ---------------------------------------------------------------------
CREATE TABLE conversations (
    id         VARCHAR(36)  PRIMARY KEY,
    user_id    VARCHAR(36)  NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    project_id VARCHAR(36)  NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title      VARCHAR(300) NOT NULL DEFAULT 'New conversation',
    created_at TIMESTAMP    NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP    NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc')
);

CREATE INDEX ix_conversations_user_id    ON conversations (user_id);
CREATE INDEX ix_conversations_project_id ON conversations (project_id);

CREATE TABLE messages (
    id              VARCHAR(36) PRIMARY KEY,
    conversation_id VARCHAR(36) NOT NULL
                    REFERENCES conversations(id) ON DELETE CASCADE,
    role            VARCHAR(16) NOT NULL,
    content         TEXT        NOT NULL,
    is_placeholder  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP   NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    CONSTRAINT messages_role_check
        CHECK (role IN ('USER', 'ASSISTANT', 'SYSTEM'))
);

CREATE INDEX ix_messages_conversation_id ON messages (conversation_id);


-- ---------------------------------------------------------------------
-- Evidence
--
-- verified stays FALSE until a real verifier confirms quoted_span really
-- appears in the cited artifact. A model can attach a real file path to
-- an invented claim, so the pointer alone proves nothing.
-- ---------------------------------------------------------------------
CREATE TABLE citations (
    id              VARCHAR(36)  PRIMARY KEY,
    message_id      VARCHAR(36)  NOT NULL
                    REFERENCES messages(id) ON DELETE CASCADE,
    artifact_id     VARCHAR(36),
    kind            VARCHAR(20)  NOT NULL,
    source_ref      VARCHAR(400) NOT NULL,
    quoted_span     TEXT         NOT NULL DEFAULT '',
    char_start      INTEGER,
    char_end        INTEGER,
    verified        BOOLEAN      NOT NULL DEFAULT FALSE,
    retrieval_rank  INTEGER,
    retrieval_score DOUBLE PRECISION
);

CREATE INDEX ix_citations_message_id ON citations (message_id);


-- ---------------------------------------------------------------------
-- Business plans
--
-- A plan is the numbered list of business requirements extracted from one
-- uploaded document. Items are vetted one at a time against the live GitHub
-- repo once the plan is confirmed; vetting_status is what makes that
-- resumable -- a dropped connection just means re-opening the stream picks
-- up at the first PENDING row instead of starting over.
-- ---------------------------------------------------------------------
CREATE TABLE business_plans (
    id              VARCHAR(36)  PRIMARY KEY,
    project_id      VARCHAR(36)  NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id         VARCHAR(36)  NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    source_filename VARCHAR(300) NOT NULL DEFAULT '',
    status          VARCHAR(20)  NOT NULL DEFAULT 'DRAFT',
    created_at      TIMESTAMP    NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    updated_at      TIMESTAMP    NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    CONSTRAINT business_plans_status_check
        CHECK (status IN ('DRAFT', 'CONFIRMED', 'DONE', 'DISCARDED'))
);

CREATE INDEX ix_business_plans_project_id ON business_plans (project_id);

CREATE TABLE business_items (
    id                           VARCHAR(36)  PRIMARY KEY,
    plan_id                      VARCHAR(36)  NOT NULL
                                 REFERENCES business_plans(id) ON DELETE CASCADE,
    seq_no                       INTEGER      NOT NULL,
    description                  TEXT         NOT NULL,
    -- Where in the source document this came from, e.g. "Page 3" or
    -- "Section: Refund Policy". Blank for an item the human adds by hand.
    location                     VARCHAR(200) NOT NULL DEFAULT '',
    vetting_status               VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
    -- Whether the requirement itself is clear enough to vet, or vague/
    -- ambiguous enough that the client should be asked to clarify it first.
    is_requirement_clear         BOOLEAN,
    is_feasible                  BOOLEAN,
    already_supported            BOOLEAN,
    -- Response fields, in BRAC IT's own Change Request / Story template
    -- vocabulary -- verified and gap-filled against the live repository.
    user_story                   TEXT         NOT NULL DEFAULT '',
    actors                       TEXT         NOT NULL DEFAULT '',
    pre_condition                TEXT         NOT NULL DEFAULT '',
    impacted_areas               TEXT         NOT NULL DEFAULT '',
    requirements                 TEXT         NOT NULL DEFAULT '',
    acceptance_criteria          TEXT         NOT NULL DEFAULT '',
    exceptions                   TEXT         NOT NULL DEFAULT '',
    -- Superseded by the template-shaped fields above. Kept (unpopulated by
    -- new vettings) for historical rows and anything still reading them.
    current_business             TEXT         NOT NULL DEFAULT '',
    feasibility_notes            TEXT         NOT NULL DEFAULT '',
    integration_approach         VARCHAR(30),
    related_existing_feature     TEXT         NOT NULL DEFAULT '',
    integration_notes            TEXT         NOT NULL DEFAULT '',
    impacts_other_features       BOOLEAN,
    impact_notes                 TEXT         NOT NULL DEFAULT '',
    is_existing_business_change  BOOLEAN,
    change_feasible              BOOLEAN,
    verdict                      TEXT         NOT NULL DEFAULT '',
    error_message                TEXT         NOT NULL DEFAULT '',
    vetted_at                    TIMESTAMP,
    CONSTRAINT business_items_vetting_status_check
        CHECK (vetting_status IN ('PENDING', 'DONE', 'ERROR')),
    CONSTRAINT business_items_integration_approach_check
        CHECK (integration_approach IS NULL OR integration_approach IN (
            'ALREADY_SUPPORTED', 'EXTEND_EXISTING_FEATURE', 'NEW_FEATURE_OR_ENDPOINT'
        )),
    CONSTRAINT uq_business_items_plan_seq UNIQUE (plan_id, seq_no)
);

CREATE INDEX ix_business_items_plan_id ON business_items (plan_id);

-- A document uploaded in chat produces a plan; the assistant message that
-- presents it points back here so the chat can render the plan's review and
-- vetting UI in place, and so later turns can recall its results. Added
-- after business_plans exists because messages is created earlier.
ALTER TABLE messages
    ADD COLUMN business_plan_id VARCHAR(36)
        REFERENCES business_plans(id) ON DELETE SET NULL;

CREATE INDEX ix_messages_business_plan_id ON messages (business_plan_id);


-- ---------------------------------------------------------------------
-- Audit and background work
--
-- audit_events has no foreign keys on purpose: the record must survive
-- the deletion of the row it describes, and it stores the actor's name
-- as text so it still reads correctly after that user is removed.
-- ---------------------------------------------------------------------
CREATE TABLE audit_events (
    id          SERIAL       PRIMARY KEY,
    entity_type VARCHAR(60)  NOT NULL,
    entity_id   VARCHAR(36)  NOT NULL,
    actor_id    VARCHAR(36),
    actor_name  VARCHAR(120) NOT NULL DEFAULT '',
    action      VARCHAR(60)  NOT NULL,
    detail      TEXT         NOT NULL DEFAULT '',
    occurred_at TIMESTAMP    NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc')
);

CREATE INDEX ix_audit_events_occurred_at ON audit_events (occurred_at DESC);

CREATE TABLE jobs (
    id            VARCHAR(36) PRIMARY KEY,
    job_type      VARCHAR(60) NOT NULL,
    entity_id     VARCHAR(36),
    status        VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    progress_pct  INTEGER     NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at    TIMESTAMP   NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    finished_at   TIMESTAMP
);


-- =====================================================================
-- SEED DATA
-- =====================================================================

-- Roles ---------------------------------------------------------------
INSERT INTO roles (id, name, description, is_admin) VALUES
  ('22222222-2222-4222-8222-000000000001', 'Platform Admin', 'Manages roles, projects and user access.', TRUE),
  ('22222222-2222-4222-8222-000000000002', 'Business Analyst', 'Writes the SRS and runs the gap check.', FALSE),
  ('22222222-2222-4222-8222-000000000003', 'QA Engineer', 'Owns test scenarios and failure paths.', FALSE),
  ('22222222-2222-4222-8222-000000000004', 'Developer', 'Reads the backlog and the systems behind it.', FALSE),
  ('22222222-2222-4222-8222-000000000005', 'Delivery Manager', 'Tracks traceability and sign-off.', FALSE);

-- Projects ------------------------------------------------------------
INSERT INTO projects
  (id, name, description, provider, repo_url, default_branch) VALUES
  ('33333333-3333-4333-8333-000000000001', 'HRMS — Leave Management',
   'Leave application, approval chain and balance accrual for the group HR system.',
   'GITLAB', 'https://gitlab.bracits.com/hrms/leave-management', 'develop'),
  ('33333333-3333-4333-8333-000000000002', 'Aarong Retail POS',
   'Point of sale, returns and stock movement across Aarong outlets.',
   'GITHUB', 'https://github.com/bracits/aarong-pos', 'main'),
  ('33333333-3333-4333-8333-000000000003', 'eRecruitment Portal',
   'Candidate applications, screening questionnaires and interview scheduling.',
   'GITHUB', 'https://github.com/bracits/erecruitment', 'main'),
  ('33333333-3333-4333-8333-000000000004', 'Microfinance Core',
   'Loan disbursement, repayment schedules and branch reconciliation.',
   'GITLAB', 'https://gitlab.bracits.com/mfi/core-ledger', 'master');

-- Which projects each role grants -------------------------------------
INSERT INTO role_projects (role_id, project_id) VALUES
  ('22222222-2222-4222-8222-000000000001', '33333333-3333-4333-8333-000000000001'),  -- Platform Admin -> HRMS — Leave Management
  ('22222222-2222-4222-8222-000000000001', '33333333-3333-4333-8333-000000000002'),  -- Platform Admin -> Aarong Retail POS
  ('22222222-2222-4222-8222-000000000001', '33333333-3333-4333-8333-000000000003'),  -- Platform Admin -> eRecruitment Portal
  ('22222222-2222-4222-8222-000000000001', '33333333-3333-4333-8333-000000000004'),  -- Platform Admin -> Microfinance Core
  ('22222222-2222-4222-8222-000000000004', '33333333-3333-4333-8333-000000000001'),  -- Developer -> HRMS — Leave Management
  ('22222222-2222-4222-8222-000000000004', '33333333-3333-4333-8333-000000000002'),  -- Developer -> Aarong Retail POS
  ('22222222-2222-4222-8222-000000000004', '33333333-3333-4333-8333-000000000003'),  -- Developer -> eRecruitment Portal
  ('22222222-2222-4222-8222-000000000002', '33333333-3333-4333-8333-000000000001'),  -- Business Analyst -> HRMS — Leave Management
  ('22222222-2222-4222-8222-000000000002', '33333333-3333-4333-8333-000000000003'),  -- Business Analyst -> eRecruitment Portal
  ('22222222-2222-4222-8222-000000000003', '33333333-3333-4333-8333-000000000001'),  -- QA Engineer -> HRMS — Leave Management
  ('22222222-2222-4222-8222-000000000005', '33333333-3333-4333-8333-000000000004');  -- Delivery Manager -> Microfinance Core

-- People --------------------------------------------------------------
INSERT INTO users (id, name, email, job_title) VALUES
  ('11111111-1111-4111-8111-000000000001', 'Anindo Dey', 'anindo.dey@bracits.com', 'Software Engineer'),
  ('11111111-1111-4111-8111-000000000002', 'Esmay Hassan Bhuiyan', 'esmay.hassan@bracits.com', 'Software Engineer'),
  ('11111111-1111-4111-8111-000000000003', 'Farhana Rahman', 'farhana.rahman@bracits.com', 'Business Analyst'),
  ('11111111-1111-4111-8111-000000000004', 'Tanvir Ahmed', 'tanvir.ahmed@bracits.com', 'QA Lead'),
  ('11111111-1111-4111-8111-000000000005', 'Nusrat Jahan', 'nusrat.jahan@bracits.com', 'Delivery Manager');

-- Role assignments. Anindo holds two roles, so he reaches the union
-- of what both grant.
INSERT INTO user_roles (user_id, role_id) VALUES
  ('11111111-1111-4111-8111-000000000001', '22222222-2222-4222-8222-000000000001'),  -- Anindo Dey -> Platform Admin
  ('11111111-1111-4111-8111-000000000001', '22222222-2222-4222-8222-000000000004'),  -- Anindo Dey -> Developer
  ('11111111-1111-4111-8111-000000000002', '22222222-2222-4222-8222-000000000004'),  -- Esmay Hassan Bhuiyan -> Developer
  ('11111111-1111-4111-8111-000000000002', '22222222-2222-4222-8222-000000000002'),  -- Esmay Hassan Bhuiyan -> Business Analyst
  ('11111111-1111-4111-8111-000000000003', '22222222-2222-4222-8222-000000000002'),  -- Farhana Rahman -> Business Analyst
  ('11111111-1111-4111-8111-000000000004', '22222222-2222-4222-8222-000000000003'),  -- Tanvir Ahmed -> QA Engineer
  ('11111111-1111-4111-8111-000000000005', '22222222-2222-4222-8222-000000000005');  -- Nusrat Jahan -> Delivery Manager

-- Sample indexed knowledge --------------------------------------------
-- Replaced by the real indexer later. Present now so placeholder
-- answers can cite rows that actually exist.
INSERT INTO artifacts (id, project_id, kind, source_ref, content, keywords) VALUES
  ('44444444-4444-4444-8444-000000000001', '33333333-3333-4333-8333-000000000001', 'SCHEMA',
   'public.leave_request',
   'Columns: id, employee_id, leave_type_id, start_date, end_date, status, approved_by, created_at. A CHECK constraint restricts status to DRAFT, SUBMITTED, APPROVED and REJECTED. There is no CANCELLED state, so cancelling an approved leave has no representation today.',
   'leave request status cancel approve reject state constraint'),
  ('44444444-4444-4444-8444-000000000002', '33333333-3333-4333-8333-000000000001', 'SCHEMA',
   'public.leave_balance',
   'Columns: employee_id, leave_type_id, entitled_days, taken_days, carried_forward, year. taken_days is decremented only by the nightly accrual job, never by the approval transaction itself.',
   'balance entitlement accrual carry forward days taken'),
  ('44444444-4444-4444-8444-000000000003', '33333333-3333-4333-8333-000000000001', 'CODE',
   'src/leave/leave_service.py#L118-176',
   'approve_leave() rejects the request when the employee is still inside their probation window, computed as joining_date plus config.probation_months. It raises PolicyViolation with code EMP_PROB_001.',
   'probation approve joining date policy violation eligibility'),
  ('44444444-4444-4444-8444-000000000004', '33333333-3333-4333-8333-000000000001', 'CODE',
   'src/leave/approval_chain.py#L44-92',
   'The approval chain walks upward through reporting_line until it finds a manager whose grade is at or above the leave type''s minimum approver grade. If nobody qualifies it escalates to the HR mailbox.',
   'approval chain manager escalation reporting line grade'),
  ('44444444-4444-4444-8444-000000000005', '33333333-3333-4333-8333-000000000001', 'RCA',
   'HRMS-4412',
   'Payroll paid out leave that had already been reversed in HR. Root cause: payroll_deduction rows referenced leave_request without any notification when the parent row changed, so the two systems drifted.',
   'payroll deduction reversal drift notification cross module'),
  ('44444444-4444-4444-8444-000000000006', '33333333-3333-4333-8333-000000000001', 'RCA',
   'HRMS-5027',
   'Concurrent approvals from two managers double-decremented the balance. Root cause: no optimistic locking on leave_balance during the approval transaction.',
   'concurrent approval race condition locking balance double'),
  ('44444444-4444-4444-8444-000000000007', '33333333-3333-4333-8333-000000000002', 'SCHEMA',
   'public.sales_return',
   'Columns: id, invoice_id, outlet_id, reason_code, refund_amount, approved_by, created_at. refund_amount is NOT NULL, so a zero-value exchange still has to write a row with an explicit zero.',
   'return refund exchange invoice outlet reason'),
  ('44444444-4444-4444-8444-000000000008', '33333333-3333-4333-8333-000000000002', 'CODE',
   'src/pos/return_policy.py#L60-104',
   'A return is refused when the invoice is older than the outlet''s return_window_days, unless the reason code is DEFECTIVE, which bypasses the window entirely.',
   'return window policy defective days invoice refuse'),
  ('44444444-4444-4444-8444-000000000009', '33333333-3333-4333-8333-000000000002', 'RCA',
   'POS-2210',
   'Stock counts drifted at three outlets after a network outage. Root cause: offline sales replayed on reconnect without checking whether the same transaction had already been posted.',
   'offline replay duplicate stock reconnect idempotency'),
  ('44444444-4444-4444-8444-000000000010', '33333333-3333-4333-8333-000000000003', 'SCHEMA',
   'public.application',
   'Columns: id, candidate_id, vacancy_id, stage, score, submitted_at. A unique index on (candidate_id, vacancy_id) prevents a candidate applying to the same vacancy twice.',
   'application candidate vacancy duplicate stage score'),
  ('44444444-4444-4444-8444-000000000011', '33333333-3333-4333-8333-000000000003', 'CODE',
   'src/screening/questionnaire_flow.py#L30-88',
   'The questionnaire renders one question per page and stores partial answers on every navigation, so an abandoned application keeps whatever the candidate had already entered.',
   'questionnaire partial answer navigation abandon page'),
  ('44444444-4444-4444-8444-000000000012', '33333333-3333-4333-8333-000000000003', 'RCA',
   'ERC-1180',
   'Candidates were emailed rejection notices twice. Root cause: the stage transition handler was not idempotent and the retry queue replayed it after a timeout.',
   'email duplicate rejection idempotent retry notification stage'),
  ('44444444-4444-4444-8444-000000000013', '33333333-3333-4333-8333-000000000004', 'SCHEMA',
   'public.repayment_schedule',
   'Columns: loan_id, installment_no, due_date, principal, interest, status. Regenerating a schedule deletes and reinserts rows, so any reference to a specific installment_no is not stable over time.',
   'repayment schedule installment regenerate loan due'),
  ('44444444-4444-4444-8444-000000000014', '33333333-3333-4333-8333-000000000004', 'CODE',
   'src/ledger/disbursement.py#L88-140',
   'Disbursement posts to the branch ledger before the schedule is generated, so a failure between the two steps leaves a disbursed loan with no repayment schedule.',
   'disbursement ledger branch schedule failure transaction'),
  ('44444444-4444-4444-8444-000000000015', '33333333-3333-4333-8333-000000000004', 'RCA',
   'MFI-3390',
   'Branch reconciliation mismatched for four days. Root cause: a timezone difference between the branch server and the core caused installments to be dated one day earlier.',
   'reconciliation timezone branch mismatch date installment');

COMMIT;


-- =====================================================================
-- Check it worked
-- =====================================================================
SELECT 'users' AS table_name, COUNT(*) FROM users
UNION ALL SELECT 'roles',         COUNT(*) FROM roles
UNION ALL SELECT 'user_roles',    COUNT(*) FROM user_roles
UNION ALL SELECT 'projects',      COUNT(*) FROM projects
UNION ALL SELECT 'role_projects', COUNT(*) FROM role_projects
UNION ALL SELECT 'artifacts',     COUNT(*) FROM artifacts
ORDER BY table_name;

-- Who can reach what. This is the same union the application computes
-- on every request, in security.py::projects_for_user.
SELECT u.name AS person,
       STRING_AGG(DISTINCT r.name, ', ' ORDER BY r.name) AS roles,
       COUNT(DISTINCT p.id) AS projects
FROM users u
LEFT JOIN user_roles    ur ON ur.user_id    = u.id
LEFT JOIN roles         r  ON r.id          = ur.role_id
LEFT JOIN role_projects rp ON rp.role_id    = r.id
LEFT JOIN projects      p  ON p.id          = rp.project_id
                          AND p.status      = 'ACTIVE'
GROUP BY u.name
ORDER BY u.name;
