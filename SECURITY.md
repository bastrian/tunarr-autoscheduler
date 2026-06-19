# Security Policy

## Supported Versions

Security fixes are provided for the current `main` branch and the latest published release.

## Reporting a Vulnerability

Please do not open public issues for suspected vulnerabilities.

Report security concerns through GitHub's private vulnerability reporting for this repository when available. If private reporting is unavailable, contact the maintainer directly and include:

- A concise description of the issue
- Affected version or commit
- Reproduction steps
- Expected impact
- Any relevant logs, screenshots, or proof-of-concept details

The maintainer will acknowledge valid reports as soon as practical, coordinate a fix, and publish a release note when disclosure is appropriate.

## Scope

The scheduler stores configuration, API tokens, schedule history, and backup archives. Treat access to the admin UI, configuration files, database, and generated diagnostic bundles as sensitive.

## Operational Guidance

- Use HTTPS in front of the scheduler.
- Keep the admin UI behind authentication.
- Limit access to backup archives and diagnostic bundles.
- Rotate Jellyfin, Jellystat, metadata provider, SMTP, Telegram, and webhook credentials if they may have been exposed.
- Review generated exports before sharing them outside your trusted environment.
