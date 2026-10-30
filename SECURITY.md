# Security Policy

## Scope

Dual-Lane Memory Core is a **local-first early alpha**. It is not hardened for
public internet exposure, multi-tenant hosting, or untrusted operators.

The HTTP service binds to `127.0.0.1` by default. Do not expose it directly to
the public internet.

## Reporting a vulnerability

Please do **not** open a public issue containing exploit details, tokens,
personal data, or a live proof of concept.

Use GitHub's **Security → Report a vulnerability** flow if private vulnerability
reporting is enabled for this repository. If it is not available, open a minimal
public issue saying that you have a security report and ask the maintainer for a
private channel; do not include the sensitive details in that issue.

## Sensitive data rules

- Never commit `memory_core/config.json`, `memory_core/memory.db`, `api-token`,
  `admin-token`, `state/`, chat logs, or real QQ IDs.
- `.gitignore` covers the default runtime paths, but check `git status` before
  every commit.
- Memory contents may contain personal data. Use the owner approval flow for
  sensitive facts and keep the service local.
- The owner-private read-all option (`owner_read_all`) is a read convenience, not
  an authorization system for hostile users.

## Supported versions

| Version | Support |
| --- | --- |
| `0.1.0-alpha` | Best-effort fixes during alpha |

## Known limitations

- Compression is a mock (first N characters).
- The default extractor is regex-based.
- Only the QQ/SnowLuma adapter is included.
- No dashboard or graph relations yet.
- Single-owner local deployment.

These are documented limitations, not vulnerabilities by themselves.
