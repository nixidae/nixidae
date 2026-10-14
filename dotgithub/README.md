<div align="center">

<img src="assets/nixidae.svg" alt="Nixidae" width="120">

# `nixidae/.github`

Organisation profile and shared community health files for
[**Nixidae**](https://github.com/nixidae).

</div>

This repository contains no code. It exists because GitHub gives the `.github`
repository of an organisation two special jobs:

1. **`profile/README.md`** is rendered as the organisation's landing page at
   <https://github.com/nixidae>.
2. **Community health files** here are inherited by every repository in the
   organisation that does not define its own. A repo that ships its own
   `CONTRIBUTING.md` overrides the one here; a repo that ships nothing gets
   these by default.

## Layout

```
.
├── profile/
│   └── README.md              # the github.com/nixidae landing page
├── assets/
│   ├── nixidae.svg            # the organisation mark
│   └── README.md              # what the mark is derived from
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.yml
│   │   ├── feature_request.yml
│   │   ├── question.yml
│   │   └── config.yml         # issue-chooser configuration
│   └── PULL_REQUEST_TEMPLATE.md
├── CODE_OF_CONDUCT.md         # inherited org-wide
├── CONTRIBUTING.md            # inherited org-wide
├── SECURITY.md                # inherited org-wide
├── SUPPORT.md                 # inherited org-wide
└── LICENSE
```

Note the two different `README.md` files. The one you are reading documents
*this* repository; `profile/README.md` is the public face of the organisation.
Editing the wrong one is the usual mistake.

## Editing the profile

`profile/README.md` is plain GitHub-flavoured Markdown, with two constraints
worth knowing:

- **Images must use absolute URLs.** Relative paths do not resolve on the
  organisation profile page, so the logo is referenced through
  `raw.githubusercontent.com`.
- **Links to other repositories must be absolute too**, for the same reason.

Mermaid diagrams render fine, as do HTML `<div align="center">` blocks and
shields.io badges.

Changes go live on <https://github.com/nixidae> as soon as they land on `main`;
there is no build step.

## Adding a project

When a new project joins nixidae/nixidae (`nix/members.nix` there), add it to
the project table in `profile/README.md` and, if it changes the picture, to the
diagram below it.
Keep the description to what the thing actually does — the table is the first
and often only thing a visitor reads.

## Licence

Apache-2.0 — see [LICENSE](LICENSE).

The logo is **not** covered by that licence, and carries disclaimer obligations
that travel with it wherever it is used. Read
[`assets/README.md`](assets/README.md) before putting the mark anywhere new.

---

<sub>

"Python" and the Python logos are trademarks of the Python Software Foundation,
used with permission. Nixidae is not affiliated with the Python Software
Foundation.

The Nixidae mark also incorporates the NixOS logo, © NixOS contributors,
licensed [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
Nixidae is not affiliated with the NixOS Foundation.

</sub>
