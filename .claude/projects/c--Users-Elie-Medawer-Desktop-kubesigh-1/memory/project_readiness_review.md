---
name: project-readiness-review
description: Production readiness and enterprise UX review findings for KubeSight, including implemented fixes and architectural roadmap
metadata:
  type: project
---

## KubeSight Production Readiness Review (2026-06-11)

**Why:** Comprehensive review requested to make the product enterprise-grade before real customer deployments.

### Implemented Fixes
- **CRITICAL** — `GET /api/upgrades/jobs/<job_id>` now validates cluster access via `can_access_cluster()` before returning job data
- **HIGH** — `kubectl` subprocess calls now have a 30s default timeout (`KUBECTL_TIMEOUT_SECONDS` env var); upgrade executor can use longer timeout
- **HIGH** — `AccessRule` now has compound DB indexes `(user_id, cluster_id)` and `(user_id, cluster_id, permission_key)`; `AuditLog` has `(actor_user_id, created_at)` and `(action, created_at)`
- **HIGH** — `namespace_resources_from_k8s` now uses `ThreadPoolExecutor` (up to 8 workers) to fetch 13 resource types in parallel — was 12 sequential kubectl calls, now ~5× faster
- **HIGH** — Global `@app.errorhandler(Exception)` added; unhandled exceptions now log full traceback server-side and return sanitized JSON 500 to client

### Open Architectural Items (require planning)
- In-memory upgrade jobs (upgrade_jobs.py) must be persisted to DB for crash recovery
- No rate limiting on `/api/auth/login` — needs Flask-Limiter
- JWT tokens not invalidated on logout — needs denylist table
- Dashboard/cluster caches are per-process — needs Redis for multi-replica deployments
- SQLite in production — must use PostgreSQL with `DATABASE_URL`
- Seed data creates predictable `admin` username — need `ADMIN_PASSWORD` env var enforcement

### Scaling Ceiling
- 100 users: works fine
- 1,000 users: SQLite breaks; access rule query latency dominates
- 10,000 users: in-memory caches and single-process threading breaks
- 100+ clusters: N+1 pattern in dashboard aggregation (list_alerts_from_k8s is O(n) serial)

**How to apply:** Flag any work touching auth, upgrade jobs, kubectl calls, or DB models against these known issues.

[[project-ux-redesign]]
