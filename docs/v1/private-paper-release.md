# Paper candidate build

A frozen build is tied to its clean source checkout. The archive manifest
and Settings → About show its full source commit. If the checkout contains
tracked or untracked changes, or Git is unavailable, no commit is claimed.
Build the candidate after the paper sprint changes have landed, and compare
the displayed commit with the build's manifest before testing.

The existing release workflow can build workflow-dispatch artifacts on Linux
x86-64, macOS arm64, and Windows x86-64. Its archive step requires
`KRELLBOT_RELEASE_BASE_URL`. Repository signing settings may request platform
signatures even for a workflow dispatch. Check those settings before running
it. A tag can publish a release, so use a non-tag build for a candidate.

The merge queue also builds unsigned candidates for those three platforms.
After the queued commit passes and lands, open its `release-frozen` Actions
run and download the `paper-candidate-<platform>-<architecture>` artifact.
Each contains the archive and a local-delivery manifest with SHA-256, version,
platform, architecture, and source commit. The job checks that the source
commit in the frozen archive matches the queued commit before upload. These
Actions artifacts expire after seven days and are accessible under the
repository's Actions permissions. A queue build does not sign artifacts or
publish a GitHub release.

For an offline Linux x86-64 candidate on a machine with the development
requirements installed, build from a clean checkout:

```sh
npm --prefix frontend ci --include=dev
npm --prefix frontend run build
uv sync --frozen --group dev --python 3.13
uv run python scripts/freeze.py
```

The resulting `dist/krellbot/` contains the executable, workstation, runtime,
and local installer. Package it using `scripts.release_archive.build_archive`
with the real delivery URL if it is being distributed. The helper records the
archive SHA-256, size, OS, architecture, version, and source commit. Compare
the downloaded archive against that digest before extracting. A local-only
archive can be used directly from the build machine; do not present a
placeholder URL as a download location. The candidate is an unsigned
prerelease and retains the release-wide paper-only policy.

Run the existing `scripts/ci_smoke_frozen_paper.py` against the actual frozen
binary and a fresh home outside the checkout. The smoke validates a strategy,
arms and disarms it in paper, opens the built workstation, and checks that the
release refuses a live arm. Further candidate acceptance covers a controlled
paper tick, pause and resume, restart, export, upgrade, and rollback for each
supported platform. Record real results for the exact build; a successful Linux
smoke does not establish native macOS or Windows behavior.
