# Security policy

## Supported versions

Security fixes are provided for the latest published `3.x` release. Upgrade to
the newest release before reporting behavior that may already be fixed.

## Reporting a vulnerability

Do not publish subscription IDs, panel URLs, credentials, tokens, cookies,
database contents, or reproducible exploit details in a public issue. Use the
repository's private **Security → Report a vulnerability** channel when it is
available. If private reporting is unavailable, contact the maintainer through
the GitHub profile and share details only after a private channel is agreed.

Include the affected version, deployment topology, minimal reproduction,
expected impact, and whether the admin port was exposed publicly. Remove or
replace all real secrets before attaching logs or configuration.

## Deployment boundary

The supported production model binds AutoSub to `127.0.0.1`, exposes only
`/json/` and `/sub/` through Nginx, and reaches `/admin` through an SSH tunnel.
Deployments that publish the admin port directly have a materially different
risk profile and are not the default security boundary.
