# Security Policy

## Supported code

Security fixes target the current main branch.

## Reporting a vulnerability

Please do not publish real API keys, access tokens, account identifiers, private market data, personal data, or exploit details containing live credentials in a public issue.

For a sensitive vulnerability, use GitHub's private vulnerability reporting / Security Advisory flow for this repository when available. If that UI is unavailable, contact the maintainer through the GitHub profile without including secrets in a public thread.

For non-sensitive hardening suggestions, a normal GitHub issue is appropriate.

## Secrets

Aldebaran is designed so that credentials are supplied through environment variables or local configuration and are not committed to the repository. If a real credential is accidentally committed, revoke or rotate it first; removing it from Git history is not a substitute for rotation.

## Scope

Reports about code execution, credential handling, unsafe file operations, dependency risks, data leakage, or unintended network behavior are especially useful.
