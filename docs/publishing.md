# Publishing And Releases

[Repository](https://github.com/marinfrankovic/Unifi-NOC) | [Actions](https://github.com/marinfrankovic/Unifi-NOC/actions) | [Pages](https://marinfrankovic.github.io/Unifi-NOC/) | [Releases](https://github.com/marinfrankovic/Unifi-NOC/releases)

## Local Changes To GitHub

Work in this isolated checkout, not its parent workspace repository. Change code, documentation or public defaults, then run:

```powershell
./tools/publish.ps1 -Message "Describe the change"
```

The VS Code **Unifi-NOC: Publish Validated Changes** task invokes the same helper when this checkout is opened as the workspace folder. It runs the privacy gate, validates the frontend, stages changes, repeats the privacy gate, commits and pushes `main`. GitHub Actions runs the fuller regression/build checks. On other platforms, run the privacy/build checks and standard Git add/commit/push commands.

File saves are not automatically committed. This intentional publication boundary prevents partial edits and runtime credentials from being uploaded. Ignore rules and the privacy scanner are safeguards, not a substitute for reviewing diffs. Do not force-add ignored artifacts. UI settings, passwords, controller keys, private overrides and `.env` remain private and are not GitHub content.

## Automatic Outputs

Every `main` push validates source, refreshes Pages and publishes both images with `edge`, `sha-<commit>` and `<VERSION>-main.<run>` tags. Pull requests validate without publishing or receiving Docker credentials. Pages includes the current source version, workflow build and commit.

Stable `vX.Y.Z` tags matching `VERSION` publish `X.Y.Z`, `X.Y`, `X` and `latest` image tags. Development builds never overwrite stable `latest`. Compose defaults to a stable pinned version; explicitly select `edge` only if accepting development-build risk. A new registry image is not an unattended rollout to an existing host.

## Repository Configuration

Enable Pages with **GitHub Actions** as its build source. Set repository variable `DOCKERHUB_USERNAME=mfrankovic` and secret `DOCKERHUB_TOKEN` to a dedicated Docker Hub write token. Never place that token in source or pass it to a conversational prompt. Store it using GitHub's encrypted secret settings or the CLI through a protected process.

The publishing workflow refreshes Docker Hub short/full descriptions using the README. Image labels identify source, documentation, license, version and revision. Docker Hub public visibility and metadata are set during onboarding. Categories depend on account/platform support; do not claim verified-publisher status.

## Stable Release

1. Update `VERSION`, public Compose defaults and `.env.example` to the new stable version, with release notes.
2. Publish the tested commit to `main` and wait for checks.
3. Create and push the matching `vX.Y.Z` tag, or create a GitHub release with that tag.
4. Confirm the tagged workflow succeeds and both image manifests are available before announcing deployment readiness.

```bash
git tag v1.0.0
git push origin v1.0.0
```

Do not reuse stable tags for different source. Rollback references should use immutable SHA/version tags or digests, not `edge` or `latest`.

## Production Policy

Publication workflows hold registry credentials only, not production SSH access or database secrets. Preserve users, site selections, database volumes and encryption keys on upgrades. There is no automatic private-host rollout in this project; use the administration guide's backup/upgrade procedure.