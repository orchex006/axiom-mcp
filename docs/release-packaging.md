# Release packaging into versioned environments

`release/package.py` is the release-side packager for `axiom-mcp`. It is the other
half of the update contract in `docs/update-plan-delegation.md`: the update surface
checks versions and delegates an approved plan, and the packager turns an
already-built artifact into an installed, isolated, reproducible environment that can
be activated or rolled back. Neither surface pip-upgrades a running process in place.

## Install-root layout

```text
<install root>/
  current.json          the active version pointer
  history.jsonl         one JSON record per install or rollback
  versions/
    <version>/
      lockfile.json     the locked document for this version
      artifact/         the staged artifact, copied from its build
      venv/             this version own isolated virtual environment
```

`LAYOUT_VERSION` (currently `1`) is written into every lockfile so a future layout
change can be detected rather than guessed.

## Isolation and the lockfile

Each version is installed into its own virtual environment through
`create_environment`: the artifact is copied next to the version first
(`stage_artifact`), the venv is created, and the artifact is installed with
`pip install --no-index --no-deps`. No network is consulted and no dependency is
resolved; the artifact is the whole input.

`pip freeze --all` is then recorded into `lockfile.json`, which carries the layout
version, the version name, the artifact name, size and SHA256, the interpreter, and the
frozen set. The staged copy is what rollback verifies later, so replacing the artifact
at its original build location cannot change what a rollback installs.

Frozen sets are compared with `normalize_frozen`, which strips the absolute install
directory from pip direct-URL lines (`name @ file:///abs/path`) and keeps the
distribution name and any `#sha256=` fragment. An install root that was moved is
therefore still recognized as the same environment.

## Install

`install(root, version, artifact=...)` resolves and validates the root, refuses a
version name that could escape the versions directory, builds the environment, writes
the lockfile, moves the active pointer, and appends an `install` record to
`history.jsonl`. A failure cleans up a version directory it created in this call; it
never removes a version that already existed.

## Rollback

`rollback(root, to_version=None)` is deliberately narrow. It chooses the target - an
explicit version, or the previous install recorded in history - and then:

1. refuses if the chosen version directory is absent;
2. proves the staged artifact still hashes to the digest its lockfile recorded;
3. recreates the venv if it is missing, or reinstalls the artifact if the frozen set
   has drifted, recording either as a repair;
4. refuses with `environment_not_restored` if the frozen set still does not match;
5. only then moves the active pointer and appends a `rollback` record.

A rollback never downloads, never guesses a version, and never installs bytes whose
hash no longer matches the lockfile.

## Refusals

Every refusal is a `PackageError` (or its `PackageRefused` subclass) with a stable
`code` and a safe `detail`; the CLI renders it as JSON.

| Code | Meaning |
| --- | --- |
| `version_invalid` | the version name is empty or could escape the versions directory |
| `install_root_invalid` | the root is the running interpreter or the source tree |
| `artifact_missing` | the artifact, or a staged artifact, does not exist |
| `artifact_not_a_file` | the artifact path is a directory or other non-file |
| `lockfile_missing` / `lockfile_corrupt` | the version has no readable lockfile |
| `current_corrupt` | the active pointer cannot be read |
| `rollback_target_missing` | the chosen version has no environment |
| `artifact_digest_mismatch` | the staged bytes are not the locked bytes |
| `no_previous_version` | history records no earlier install to fall back to |
| `venv_creation_failed` / `install_failed` / `freeze_failed` | a build step failed |
| `build_failed` | `python -m build --wheel --no-isolation` failed |
| `environment_not_restored` | the environment still drifts from the lockfile |

## Command line

```text
python release/package.py install  --install-root ROOT --version VERSION --artifact WHEEL
python release/package.py rollback --install-root ROOT [--to VERSION]
python release/package.py status   --install-root ROOT
```

`main` prints JSON only: a payload on success (`exit 0`) or
`{"error": code, "detail": ...}` on refusal (`exit 1`).

## Verification

`tests/test_release_package.py` proves AC1 on this host with the real thing: two wheels
are built with the real build backend, installed by the real pip into two isolated
virtual environments, and each environment own interpreter is run to observe that it
imports its own build rather than the one installed afterwards. The same leg then rolls
back and proves the earlier version is active again and still imports its own build.

The remaining legs drive the same production code through a scripted runner: the
directories are real and every decision under test - the lockfile document, the digest
check, the drift repair, the pointer move, the history record and the no-partial-state
cleanup - is the real code, while pip itself is answered by the runner. Each such test
says so in its own docstring.

## Limitations

A virtual environment costs about twenty seconds to create and populate on the
reference host, so the drift, tamper and failure legs are scripted rather than repeated
real installs; they are a code-path proof, not a second real pip run.
