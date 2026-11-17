# Security Policy

## Supported versions

Only the latest commit on `main` is supported. Please reproduce issues there
before reporting.

## Reporting a vulnerability

Please do not open a public issue for security problems.

Report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/sponsorscan/sponsorscan/security/advisories/new).
Include:

- A description of the issue and its impact
- Steps to reproduce, or a proof of concept
- The affected file, command, or workflow

You can expect an initial response within 7 days. Once a fix is ready, it will
be released on `main` and you will be credited in the advisory unless you
prefer otherwise.

## Scope

Of particular interest:

- Leaks of personal data, such as profiles, visa status, or state files, into
  logs, reports, commits, or workflow artifacts
- Exposure of credentials used for Google Sheets, email, or GitHub Actions
- Unsafe handling of data fetched from DOL downloads or ATS job board APIs

Keeping your own secrets safe (for example, not committing `.env` or a service
account key) is your responsibility, but gaps in the tooling or docs that make
this easy to get wrong are in scope.
