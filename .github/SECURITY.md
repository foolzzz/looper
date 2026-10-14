# Security Policy

## Supported Versions

Security fixes are made on `main` and shipped in the next release of
[foolzzz/looper](https://github.com/foolzzz/looper/releases). Earlier releases
get no separate security branches; upgrade by running `install.sh` again
before you report a version-specific problem.

## Reporting A Vulnerability

Report suspected vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/foolzzz/looper/security/advisories/new)
(the repository's **Security** tab, then **Report a vulnerability**). Do not
open a public issue or pull request for an unpatched vulnerability. If private
reporting is unavailable, open an issue that asks the maintainer,
[@foolzzz](https://github.com/foolzzz), for a private channel, and leave the
details out of it.

A useful report includes:

- the affected Looper version or commit;
- the affected surface (the HTTP API, a channel, the agents' Claude runs,
  installation) and the boundary you expected to hold;
- minimal reproduction steps or a proof of concept;
- the potential impact and any known mitigation.

Do not include credentials, private data or unrelated internal material. If
a reproduction needs sensitive evidence, explain in the private report how
it can be shared.

Reports are handled on a best-effort basis. The maintainer will confirm
receipt, agree on the fix and the release with you, and publish an advisory.
Please wait for the fix before publishing details that would put users at
risk.

## Scope

This policy covers the Looper source in this repository, the release wheel,
`install.sh` and the repository's workflows. Every agent runs Claude Code with
`--dangerously-skip-permissions`, so whoever can drive the HTTP API or a
channel can run commands on the host. The API refuses requests from other web
origins. While it listens on a loopback address, it also refuses requests
that name another host. The channels pass on only the senders their
allowlists name. See the HTTP API and Channels sections of
[the architecture](../docs/architecture/agent-system.md), and report any way
around these boundaries. Serving on a non-loopback `--host` exposes the API to
that network by design. Vulnerabilities that exist only in a dependency belong
with that project; report them here as well when Looper's use of the
dependency makes them worse.
