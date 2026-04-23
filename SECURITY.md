# Security Policy

## Supported Versions

Only the latest release on the `main` branch receives security updates.

## Reporting a Vulnerability

If you discover a security vulnerability in this project, **please do not open a public GitHub issue.** Public disclosure before a fix is available puts all users at risk.

Instead, please report vulnerabilities privately through GitHub's built-in security advisory feature:

1. Navigate to the **Security** tab of this repository.
2. Click **Report a vulnerability** under "Private vulnerability reporting."
3. Provide a clear description of the issue, steps to reproduce, and the potential impact.

We will acknowledge receipt within 48 hours and aim to provide a fix or mitigation plan within 7 days, depending on severity.

## Scope

The following are in scope for security reports:

- Command injection, SQL injection, or code execution vulnerabilities
- Path traversal or unauthorized file access
- Credential or secret exposure (hardcoded keys, tokens, or passwords)
- Unsafe deserialization or insecure data handling
- Any issue that could compromise the integrity of the local SQLite database

The following are **out of scope**:

- Vulnerabilities in third-party dependencies (please report these upstream)
- Issues that require physical access to the machine running the software
- The dashboard UI, which binds exclusively to `127.0.0.1` by design

## Disclosure Policy

We follow coordinated disclosure. Once a fix is released, we will credit the reporter in the release notes unless they prefer to remain anonymous.

Thank you for helping keep this project and its users safe.
