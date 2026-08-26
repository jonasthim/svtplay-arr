# Security

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report it privately through GitHub's private vulnerability reporting:

<https://github.com/jonasthim/svtplay-arr/security/advisories/new>

If that is not available to you, open a public issue containing only a request
for a private contact channel — no details — and you will be contacted to
continue there.

Please include:

- What the problem is, and what an attacker can do with it.
- The version or commit you found it on.
- Steps to reproduce, ideally minimal.
- Whether you have disclosed it anywhere else.

This is a small, unfunded, single-maintainer project with no security team and
no bounty programme. Expect a first response within a couple of weeks rather
than a couple of hours. Fixes land as ordinary commits on the default branch;
if a report warrants it, a GitHub Security Advisory will be published with it.

Please give a reasonable window to fix before disclosing publicly.

## The security posture, stated honestly

Anyone deploying this should know exactly what they are exposing. None of the
below is a discovered flaw — it is all deliberate design, documented so that the
decision to expose it is an informed one.

### The service authenticates nothing

**No route in this service checks any credential.** Not the indexer surface,
not the download-client surface, not the configuration page, not `/health`.

Sonarr's indexer and download-client forms require an API key field to be
filled in, and this service accepts any value in it, because it never looks.
Do not go looking for a real key; there isn't one.

The service is designed to sit on a private network where only Sonarr talks to
it. That assumption is the entire access-control model.

### The configuration page can rewrite the service's configuration

`/config` is unauthenticated and can:

- rewrite `config.yaml`, including the Sonarr URL and the download directories
- delete every series mapping
- read back the Sonarr API key

It cannot restart the service, alter the worker, or touch the download
pipeline — that seam is deliberate and tested. But an unauthenticated party who
can reach the page can break the service, redirect it at a Sonarr instance of
their choosing, and read the API key for your real one.

**It is intended for a trusted network, or behind a reverse proxy that
authenticates in front of it.** If you publish it, publish the **whole
origin** — `/`, `/config`, `/api` and `/sabnzbd` all behind the same gate, with
no bypass rules, no public path exceptions, and no `/health` carve-out. A
`/health` exception in particular is tempting and should be resisted; if you
want external health monitoring, solve it deliberately rather than by punching
a hole in the only gate the page has.

See
[`deploy/README.md` § The configuration page](deploy/README.md#the-configuration-page)
for the operational detail, including why Sonarr itself must keep using the
internal address and never the published hostname.

### The Sonarr API key is rendered into the configuration page

This is a deliberate choice, not an oversight, and it was made after the
alternative had shipped.

The key is editable on the page, and its value is written into the page's HTML.
The field is masked with a Show/Hide button — but the value is in the page
source **whichever way that button is set**. Masking is a shoulder-surfing
measure and nothing else. Concretely, the key is therefore:

- in the HTML delivered to any browser that loads the page
- in that browser's cache
- in that browser's history
- in any screenshot of the page

The reasoning for accepting that: the key was previously the one setting that
required an SSH session, which is exactly the asymmetry the page exists to
remove. Excluding it did not make the threat model better, only less honest
about it — anyone who can reach the page can already rewrite the config and
delete every mapping, so they can already break the service. What protects the
value is the network boundary (or the authenticating proxy), plus the `0640`
mode on `config.yaml`. Not the page's silence.

The full argument, including what was reversed and why, is in
[`docs/design/2026-08-25-config-ui-design.md`](docs/design/2026-08-25-config-ui-design.md)
under "Reversed after implementation".

Note that the *effective* key — an override supplied through the
`SONARR_API_KEY` environment variable — is never rendered and is never written
to disk by a save. Only the file's own value reaches the page.

### The service invokes a media downloader

`svtplay-dl` is called with an SVT video id taken from an uploaded `.nzb`. Both
the release title (which becomes a filename) and the quality label are
sanitised against path traversal and against `svtplay-dl`'s own filename
rewriting before they are used, and there is a test for it. Uploaded `.nzb`
files are size-capped at 64 KB and parsed with `defusedxml` rather than the
standard library, so entity-expansion and DTD attacks are refused.

Nonetheless: this is a service that drives a media downloader on the strength
of input arriving over HTTP. Exposing it to the public internet
unauthenticated has no upside.

### What is not a vulnerability report

- "The API has no authentication." Documented above; it is the design.
- "The config page has no login." Documented above; it is the design.
- "The API key is visible in the page source." Documented above; it is the
  design.
- "It downloads copyrighted material." It automates something a person can do
  by hand in a browser with content that is free to watch in Sweden. See the
  usage note in the [README](README.md#legal-and-usage).

A report that any of the above can be reached **from outside the boundary they
assume** — a path that bypasses a proxy, an unintended listener, a route that
leaks the key somewhere it was believed not to go — is very much wanted.

### Where the key must not appear

Tests pin these, and a change that breaks one is a real bug:

- `sonarr_api_key` never appears in `/health`.
- It never appears in any Newznab or SABnzbd response.
- It never appears in the settings-saved notice or the pending-restart banner,
  which render field labels only.
- An environment-supplied key is never written into `config.yaml`.

## Supported versions

This project is pre-1.0 and has had no releases yet. Only the default branch is
supported; fixes go there and nowhere else.
