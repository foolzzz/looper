# Releases

This folder holds every release's GitHub release body (`vX.Y.Z.md`) and its
list of optional-capability surfaces (`vX.Y.Z.surfaces`). This page is the
procedure that publishes them.

## How a release is published

Looper releases are tagged in GitHub, and a tag publishes its release through
the [Release workflow](../../.github/workflows/release.yml). Nothing is built
or uploaded from a maintainer's machine, and the fork does not publish to
PyPI. `install.sh` installs the wheel attached to the latest GitHub release,
so every release carries one, built from its tag. Users upgrade by running
`install.sh` again.

1. **Release pull request.**
   - Bump `looper.__version__` (`looper/__init__.py`) and `version` in
     `pyproject.toml` together.
   - Move the CHANGELOG's `[Unreleased]` entries under the new version.
   - Commit the exact release body as `docs/releases/vX.Y.Z.md` and its
     surfaces as `docs/releases/vX.Y.Z.surfaces`, one per line. An empty
     file declares that the release adds none.
   - Check the body locally before the review:

     ```bash
     surfaces=()
     while IFS= read -r arg; do surfaces+=("$arg"); done < <(scripts/release-surface-args.sh docs/releases/vX.Y.Z.surfaces)
     uv run --extra test python examples/release/release-readiness-doc-smoke.py --release-notes docs/releases/vX.Y.Z.md "${surfaces[@]}"
     ```

2. **Tag.** After the pull request merges, tag the merge commit and push the
   tag:

   ```bash
   git tag -a vX.Y.Z -m "Looper vX.Y.Z" <merge commit> && git push origin vX.Y.Z
   ```

3. **The workflow** then:
   1. checks that the tag is `vX.Y.Z`, is on `main`, matches
      `pyproject.toml` and `looper.__version__`, and that both release files
      exist;
   2. runs `release-readiness-doc-smoke` on the committed body with its
      surfaces;
   3. builds the dashboard from `web/` into `looper/web/dashboard`
      (`scripts/stage-dashboard.sh`);
   4. runs `examples/release/agent-serve-wheel-smoke.py` against a PostgreSQL
      service: it builds the wheel from a clean copy of the tracked files,
      installs it into a fresh virtualenv and runs the installed
      `looper serve`;
   5. builds the wheel and creates the release as a draft with the body and
      the wheel, reads the body back, checks it again and only then publishes
      it as the latest release, so the latest release never lacks its wheel.

A failed run can be repeated for the same tag from the Actions tab
(`workflow_dispatch` with the tag).

## What the release body must contain

`examples/release/release-readiness-doc-smoke.py` refuses a body that lacks
any of the following. [v2.5.0](v2.5.0.md) is a complete example.

- **`## Release Decision`** with these fields:
  - `**Who should upgrade:**`;
  - `**What this release solves:**`;
  - `**Breaking changes:**`, starting with `No.` or `Yes.`;
  - `**How to verify:**`, with a `bash` code block;
  - `**Contributors:**`, naming a `@handle` or stating that there was no
    community contribution.
- **`## Optional Capability Activation & Use`**, with one `### <surface>` per
  line of the surfaces file. Each surface has these fields:
  - `**Activation:**`;
  - `**Validation:**`;
  - `**Disable / rollback:**`;
  - `**Authority boundary:**`;
  - `**Docs:**`.

  With an empty surfaces file, the section instead says "No new optional
  capability activation is introduced in this release."
- **`## 中文摘要`**, which repeats both parts in Chinese:
  - `### 升级决策`, with `谁需要升级`, `解决了什么`, `是否有破坏性变更` (starting with
    无, 有, 否 or 是), `如何验证` and `贡献者`;
  - `### 可选能力启用与使用`, with one `#### <surface>` per surface (`启用`, `验证`,
    `停用 / 回退`, `权限边界`, `文档`), or "本版本未新增可选能力启用入口。".
- **Balanced code fences**, and no local paths or credential variable names.

Credit community contributors by handle and pull request in both languages.
When a release has none, say so; the gate requires either.
