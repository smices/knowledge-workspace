# Project collaboration memory

## User requirements (2026-09-10)

- Use Astra for planning, analysis, substantive review, and final acceptance.
- Use Sol with low reasoning effort for routine work and coordination.
- Use Luna with xhigh reasoning effort for bounded implementation and focused tests. After repeated unsuccessful Luna attempts, the user permits escalation to Terra high; follow higher-priority runtime constraints and disclose any fallback.
- Prefix every user-facing update and final response with the actual producing model and effort when known (for example, `【Sol Low】`); never invent a model identity. Identify delegated work separately from the author of the update.
- Commit each completed bug fix or feature after focused validation. A commit does not authorize pushing or deploying.
- After implementation, review the diff for bugs, missing boundaries, requirements mismatches, and quality issues. Fix confirmed problems, rerun relevant checks, then deliver.
- Current objective: close the architecture, authorization, reliability, machine-caller, capacity-protection, and test gaps identified in the project audit. Do not claim support for 1500 employees and AI workers without representative workload evidence.
- Preserve unrelated work and keep secrets and machine-specific paths out of tracked artifacts.

## Product requirements (2026-09-10 update)

- Track unfinished work and acceptance evidence in `docs/implementation-backlog.md`; implement and review it in bounded phases rather than marking planned capabilities complete.
- Knowledge content requires authentication. Support local username/password login (including the installation administrator) alongside OpenIdentity OIDC login.
- Public self-registration is closed. Administrators can create application users; do not add an anonymous registration endpoint.
- Local credentials and IdP identities are separate. Never collect IdP passwords, use a password grant, or merge identities by email/name. IdP account identity uses the verified stable subject.
- Read OpenIdentity's application and API service integration contracts before identity changes. External IdP registration, credentials, and deployment require separate authorization.
