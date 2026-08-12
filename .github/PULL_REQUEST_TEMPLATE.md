## What changed

Describe the user-visible behavior and the reason for the change.

## Compatibility

- [ ] `/json/{sub_id}` and `/sub/{sub_id}` behavior remains compatible or the change is documented.
- [ ] SQLite, `.env`, and release-layout compatibility was considered.
- [ ] No secret, real subscription ID, credential, database, or log was added.

## Verification

- [ ] Relevant pytest regression tests were added or updated.
- [ ] Ruff and Pyright pass.
- [ ] Coverage, pip-audit, Bandit, and ShellCheck gates pass.
- [ ] Documentation and `CHANGELOG.md` are updated when behavior changes.
- [ ] `runtime-manifest.txt` is updated when a runtime file is added or removed.
- [ ] Linux/systemd/Nginx smoke results or an explicit untested-risk note are included.
