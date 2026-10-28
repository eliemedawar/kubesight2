# KubeSight BuildKit builders

When a CI pipeline has a container image stage, the build pod in
`kubesight-ci` does not build the image itself. It sends the work to a BuildKit
daemon in `kubesight-build`, which builds the image and pushes it to the
registry. Two builders run, preferably on different nodes, so one can fail
without stopping builds. They run rootless, inside the platform's admission
fence for this namespace.

| | |
|---|---|
| Owner | DevOps team |
| Purpose | Build and push container images for KubeSight CI, without root or privileged mode |
| Version | BuildKit v0.23.2 (`docker.io/moby/buildkit:v0.23.2-rootless`); released with KubeSight 1.0.0 |
| Status | Planned: installed by CHG-KS-002 |
| Namespace | `kubesight-build` (pod security `privileged`, fenced by the platform's admission policy) |
| Runs as | uid 1000 / gid 1000, non-root, no added capabilities, seccomp and AppArmor unconfined, no service account token |
| Depends on | `kubesight-backend` (tells build pods where the builders are), the registry `registry.areeba.com` |
| Used by | `kubesight-ci` build pods (image stages), on port 1234 |
| Secrets | none of its own; each build passes its registry login with the request |
| Change records | [CHG-KS-001: secrets in OpenBao](../../../changes/CHG-KS-001-openbao-secrets.md), [CHG-KS-002: install](../../../changes/CHG-KS-002-install.md) |
| Signed | ____ (fill in your name) |

## Documents

- [Tutorial](tutorial.md): look at the builders and watch one work.
- [How-to guides](how-to.md): one task each.
- [Explanation](explanation.md): why it is built this way.
- [Reference](reference.md): objects, settings, rules and limits.
