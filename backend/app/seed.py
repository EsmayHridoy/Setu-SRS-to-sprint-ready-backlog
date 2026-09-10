"""Sample data so the application is usable the moment it starts.

Runs only when the users table is empty, so restarting never overwrites work.
Delete setu.db to start again.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import Artifact, Project, Role, User

ROLES = [
    ("Platform Admin", "Manages roles, projects and user access.", True),
    ("Business Analyst", "Writes the SRS and runs the gap check.", False),
    ("QA Engineer", "Owns test scenarios and failure paths.", False),
    ("Developer", "Reads the backlog and the systems behind it.", False),
    ("Delivery Manager", "Tracks traceability and sign-off.", False),
]

PROJECTS = [
    {
        "name": "HRMS — Leave Management",
        "description": "Leave application, approval chain and balance accrual "
                       "for the group HR system.",
        "provider": "GITLAB",
        "repo_url": "https://gitlab.bracits.com/hrms/leave-management",
        "default_branch": "develop",
    },
    {
        "name": "Aarong Retail POS",
        "description": "Point of sale, returns and stock movement across "
                       "Aarong outlets.",
        "provider": "GITHUB",
        "repo_url": "https://github.com/bracits/aarong-pos",
        "default_branch": "main",
    },
    {
        "name": "eRecruitment Portal",
        "description": "Candidate applications, screening questionnaires and "
                       "interview scheduling.",
        "provider": "GITHUB",
        "repo_url": "https://github.com/bracits/erecruitment",
        "default_branch": "main",
    },
    {
        "name": "Microfinance Core",
        "description": "Loan disbursement, repayment schedules and branch "
                       "reconciliation.",
        "provider": "GITLAB",
        "repo_url": "https://gitlab.bracits.com/mfi/core-ledger",
        "default_branch": "master",
    },
]

USERS = [
    ("Anindo Dey", "anindo.dey@bracits.com", "Software Engineer",
     ["Platform Admin", "Developer"]),
    ("Esmay Hassan Bhuiyan", "esmay.hassan@bracits.com", "Software Engineer",
     ["Developer", "Business Analyst"]),
    ("Farhana Rahman", "farhana.rahman@bracits.com", "Business Analyst",
     ["Business Analyst"]),
    ("Tanvir Ahmed", "tanvir.ahmed@bracits.com", "QA Lead",
     ["QA Engineer"]),
    ("Nusrat Jahan", "nusrat.jahan@bracits.com", "Delivery Manager",
     ["Delivery Manager"]),
]

# role name -> project names it can reach
GRANTS = {
    "Platform Admin": [p["name"] for p in PROJECTS],
    "Developer": ["HRMS — Leave Management", "Aarong Retail POS",
                  "eRecruitment Portal"],
    "Business Analyst": ["HRMS — Leave Management", "eRecruitment Portal"],
    "QA Engineer": ["HRMS — Leave Management"],
    "Delivery Manager": ["Microfinance Core"],
}

# Sample artifacts so placeholder answers can cite something that exists.
ARTIFACTS: dict[str, list[tuple[str, str, str, str]]] = {
    "HRMS — Leave Management": [
        ("SCHEMA", "public.leave_request",
         "Columns: id, employee_id, leave_type_id, start_date, end_date, "
         "status, approved_by, created_at. A CHECK constraint restricts status "
         "to DRAFT, SUBMITTED, APPROVED and REJECTED. There is no CANCELLED "
         "state, so cancelling an approved leave has no representation today.",
         "leave request status cancel approve reject state constraint"),
        ("SCHEMA", "public.leave_balance",
         "Columns: employee_id, leave_type_id, entitled_days, taken_days, "
         "carried_forward, year. taken_days is decremented only by the nightly "
         "accrual job, never by the approval transaction itself.",
         "balance entitlement accrual carry forward days taken"),
        ("CODE", "src/leave/leave_service.py#L118-176",
         "approve_leave() rejects the request when the employee is still "
         "inside their probation window, computed as joining_date plus "
         "config.probation_months. It raises PolicyViolation with code "
         "EMP_PROB_001.",
         "probation approve joining date policy violation eligibility"),
        ("CODE", "src/leave/approval_chain.py#L44-92",
         "The approval chain walks upward through reporting_line until it "
         "finds a manager whose grade is at or above the leave type's minimum "
         "approver grade. If nobody qualifies it escalates to the HR mailbox.",
         "approval chain manager escalation reporting line grade"),
        ("RCA", "HRMS-4412",
         "Payroll paid out leave that had already been reversed in HR. Root "
         "cause: payroll_deduction rows referenced leave_request without any "
         "notification when the parent row changed, so the two systems drifted.",
         "payroll deduction reversal drift notification cross module"),
        ("RCA", "HRMS-5027",
         "Concurrent approvals from two managers double-decremented the "
         "balance. Root cause: no optimistic locking on leave_balance during "
         "the approval transaction.",
         "concurrent approval race condition locking balance double"),
    ],
    "Aarong Retail POS": [
        ("SCHEMA", "public.sales_return",
         "Columns: id, invoice_id, outlet_id, reason_code, refund_amount, "
         "approved_by, created_at. refund_amount is NOT NULL, so a zero-value "
         "exchange still has to write a row with an explicit zero.",
         "return refund exchange invoice outlet reason"),
        ("CODE", "src/pos/return_policy.py#L60-104",
         "A return is refused when the invoice is older than the outlet's "
         "return_window_days, unless the reason code is DEFECTIVE, which "
         "bypasses the window entirely.",
         "return window policy defective days invoice refuse"),
        ("RCA", "POS-2210",
         "Stock counts drifted at three outlets after a network outage. Root "
         "cause: offline sales replayed on reconnect without checking whether "
         "the same transaction had already been posted.",
         "offline replay duplicate stock reconnect idempotency"),
    ],
    "eRecruitment Portal": [
        ("SCHEMA", "public.application",
         "Columns: id, candidate_id, vacancy_id, stage, score, submitted_at. "
         "A unique index on (candidate_id, vacancy_id) prevents a candidate "
         "applying to the same vacancy twice.",
         "application candidate vacancy duplicate stage score"),
        ("CODE", "src/screening/questionnaire_flow.py#L30-88",
         "The questionnaire renders one question per page and stores partial "
         "answers on every navigation, so an abandoned application keeps "
         "whatever the candidate had already entered.",
         "questionnaire partial answer navigation abandon page"),
        ("RCA", "ERC-1180",
         "Candidates were emailed rejection notices twice. Root cause: the "
         "stage transition handler was not idempotent and the retry queue "
         "replayed it after a timeout.",
         "email duplicate rejection idempotent retry notification stage"),
    ],
    "Microfinance Core": [
        ("SCHEMA", "public.repayment_schedule",
         "Columns: loan_id, installment_no, due_date, principal, interest, "
         "status. Regenerating a schedule deletes and reinserts rows, so any "
         "reference to a specific installment_no is not stable over time.",
         "repayment schedule installment regenerate loan due"),
        ("CODE", "src/ledger/disbursement.py#L88-140",
         "Disbursement posts to the branch ledger before the schedule is "
         "generated, so a failure between the two steps leaves a disbursed "
         "loan with no repayment schedule.",
         "disbursement ledger branch schedule failure transaction"),
        ("RCA", "MFI-3390",
         "Branch reconciliation mismatched for four days. Root cause: a "
         "timezone difference between the branch server and the core caused "
         "installments to be dated one day earlier.",
         "reconciliation timezone branch mismatch date installment"),
    ],
}


def seed(db: Session) -> bool:
    """Insert sample data. Returns True when it actually wrote something."""
    if db.query(User).count() > 0:
        return False

    roles = {}
    for name, description, is_admin in ROLES:
        role = Role(name=name, description=description, is_admin=is_admin)
        db.add(role)
        roles[name] = role

    projects = {}
    for spec in PROJECTS:
        project = Project(**spec)
        db.add(project)
        projects[spec["name"]] = project

    db.flush()

    for role_name, project_names in GRANTS.items():
        roles[role_name].projects = [projects[n] for n in project_names]

    for name, email, title, role_names in USERS:
        db.add(User(name=name, email=email, job_title=title,
                    roles=[roles[n] for n in role_names]))

    for project_name, rows in ARTIFACTS.items():
        project = projects[project_name]
        for kind, source_ref, content, keywords in rows:
            db.add(Artifact(project_id=project.id, kind=kind,
                            source_ref=source_ref, content=content,
                            keywords=keywords))

    db.commit()
    return True
