# Locked native entrypoints

`src/axiom_mcp/entrypoints.py` is the launch surface for a host that must start the
gateway **without a shell**. A host does not activate a virtual environment, does not
source a script and does not quote a command line: it launches one absolute
interpreter with an argument vector. This document records that implementation; the
distribution, update and compatibility contracts stay in `axiom-specs`.
Companion documents: `docs/release-packaging.md` (the packager that owns the
install-root layout) and `docs/runtime-compatibility.md` (the Python and SDK pin).

## What the surface reads

The packager's install root is the only input:

```text
<install root>/
  current.json                       the active version pointer
  versions/<version>/lockfile.json   the locked document for that version
  versions/<version>/venv/           that version's own environment
  entrypoints.json                   written by this surface, never by the packager
```

`VERSIONS_DIRNAME`, `VENV_DIRNAME`, `LOCKFILE_FILENAME`, `CURRENT_FILENAME` and
`LOCK_LAYOUT_VERSION` are **mirrored** from `release/package.py` rather than imported,
because `release/` is not part of the installed wheel. A mirror that could drift
would be a second, silent definition of the layout, so
`tests/test_entrypoints.py::test_the_installed_layout_names_agree_with_the_packaging_tool`
reads both definitions and fails when they disagree.

## The launch document

`host_launch_document(plan)` renders exactly what a host configuration consumes:

```json
{
  "entrypoint": "stdio",
  "component": "axiom-mcp",
  "command": "<install root>/versions/<version>/venv/Scripts/python.exe",
  "args": ["-m", "axiom_mcp.stdio", "--name", "axiom-mcp"],
  "cwd": "<install root>",
  "env": {"...": "..."},
  "digest": "sha256:..."
}
```

`src/axiom_mcp/entrypoints.py` is reachable both as `python -m axiom_mcp.entrypoints`
and through the `axiom-mcp-entrypoints` console script that `pyproject.toml` declares;
both call the same `main`, and a test reads the declaration so a script whose target
does not exist cannot be installed.

`command` is one absolute interpreter path and `args` is a list, so a path holding a
space, an apostrophe, an ampersand or Thai characters arrives as one argument instead
of being split by a shell. **No string form of a plan is produced anywhere**: a joined
command line is the failure this document exists to prevent. `plan_digest` is the
SHA256 of a canonical JSON encoding of the mode, the executable, the argument vector,
the working directory, the whole environment and the parts of the verified runtime
that decide what runs (`interpreter`, `python_version`, `sdk_pin`, `sdk_version`,
`lockfile_sha256`), so an edited lock, a replaced environment and a changed argv all
report as drift. The document is emitted with `ensure_ascii` on, so a non-ASCII path
leaves the process as an escape sequence instead of as raw characters in the host
console's code page.

HTTP is opt-in and refused unless it is consistent: `http_arguments` reuses
`http_transport.HttpTransportSettings` for the address and `security.host_allowed` /
`security.origin_allowed` for the allowlist meanings, then adds the check neither of
them can make alone - the transport allowlist must admit the address the gateway is
about to bind, because a gateway whose own allowlist refuses it answers nothing.

## The launch environment

`scoped_environment` builds the environment from scratch. The caller's environment is
**not** copied, so `PYTHONPATH`, `PYTHONHOME` and the login shell's `PATH` cannot
change what a plan runs, and two launches of the same plan in different shells produce
the same environment - which is what lets the digest be written down and verified
later. What is set, and why:

| Variable | When | Why |
| --- | --- | --- |
| `VIRTUAL_ENV` | always | names the environment the process is believed to be in |
| `AXIOM_MCP_ENTRYPOINT` | always | records which transport was launched |
| `PATH` | always | the environment's own scripts directory plus, on Windows, the loader directory; never the login shell's |
| `PYTHONNOUSERSITE` | only when the environment does **not** share base packages | suppressing the base user site for a sharing environment would contradict the environment's own `pyvenv.cfg` |
| user-site anchors (`APPDATA`, `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH` on Windows; `HOME` elsewhere) | only when the environment does not suppress its user site | `site` derives the user site's location from them |
| `SYSTEMROOT`, `WINDIR` | Windows | the platform loader and the socket layer need them |

The anchors and the Windows system variables are *host facts*, not caller settings:
a caller that supplies one is believed, and otherwise the launcher's own environment
answers. A launch plan built from a partial environment must still be able to start
the interpreter it names.

## Findings recorded while implementing this surface

Four behaviours were measured on this host (Windows, Python 3.13.14, `mcp==1.28.1`
installed in the per-user site) rather than assumed.

**1. A launch environment without `SystemRoot` cannot import the SDK at all.** The
scoped environment carries `SYSTEMROOT`/`WINDIR` for this reason; the observed
failure without them is not a missing package but the platform's own socket layer:

```text
File "...\Python313\Lib\asyncio\windows_events.py", line 8, in <module>
    import _overlapped
OSError: [WinError 10106] The requested service provider could not be loaded or initialized
```

**2. A sharing environment's user site is located through the home anchors.** For a
`venv` created with `include-system-site-packages = true`, the pinned SDK is reachable
through the base interpreter's user site, and `site` finds that user site through
`APPDATA` (falling back to `USERPROFILE`, then `HOMEDRIVE` + `HOMEPATH`). Dropping
those variables makes `importlib.metadata.version("mcp")` answer nothing, so
`resolve_runtime` refuses with `sdk_not_installed` for an environment that is in fact
complete. Measured directly:

```text
scoped_environment(..., shared=True, parent={})                                  -> sdk None
scoped_environment(..., shared=True, parent={}) + APPDATA                        -> sdk 1.28.1
```

**3. The emitted document must not cross a code page.** Reading the plan back as a
process on this host showed the Thai part of the interpreter path arriving as
mojibake: with `ensure_ascii=False`, `json.dumps` hands raw characters to
`sys.stdout`, whose encoding for a pipe is the host's ANSI code page, so the reader
decodes bytes that never were UTF-8. `ensure_ascii` is therefore left on - the
document is ASCII on the wire and carries the exact path.

**4. `pyvenv.cfg` is not always UTF-8.** A virtual environment created at a path with
non-ASCII characters records its creation command in the host code page, so the file
can hold cp874 or cp1252 bytes. `read_venv_metadata` therefore decodes lossily on
purpose: it reads one boolean, and a replacement character inside a recorded command
line must not make a launchable environment look unreadable. The negative leg
(`venv_metadata_corrupt`) is still raised for a genuinely empty document.

## The refusal table

Every failure below is a refusal with a stable code, never a warning, because each one
means the running code is not the code the lock names. `plan` prints the refusal on
stderr as JSON and exits `2`; `verify` exits `2` when a recorded lock has drifted.

| Code | Refused because |
| --- | --- |
| `install_root_missing` | the install root is not an existing directory |
| `current_missing`, `current_corrupt` | no readable active-version pointer |
| `version_invalid` | the active value is not a bare version directory name |
| `lockfile_missing`, `lockfile_corrupt` | the version document is absent or unreadable |
| `lockfile_version_mismatch` | the document names another version than the pointer |
| `interpreter_missing`, `interpreter_unresolvable` | the recorded interpreter is not a usable file |
| `interpreter_outside_environment` | the interpreter does not resolve inside that version's `venv` |
| `venv_metadata_missing`, `venv_metadata_unreadable`, `venv_metadata_corrupt` | `pyvenv.cfg` cannot be read for one boolean |
| `interpreter_probe_failed`, `interpreter_probe_corrupt` | the interpreter did not answer, or answered without a prefix |
| `interpreter_version_unsupported` | the answer is outside `>=3.13,<3.14` |
| `interpreter_not_isolated` | `prefix` equals `base_prefix`: this is the base interpreter |
| `interpreter_prefix_mismatch` | the answer is not the environment that was bound |
| `sdk_not_installed`, `sdk_pin_mismatch` | the SDK is absent, or is not the pin |
| `mode_unknown` | the transport is neither `stdio` nor `http` |
| `activation_script_refused`, `path_lookup_refused` | a shell script, or a `PATH` lookup, was requested |
| `http_parameters_on_stdio` | HTTP options were given for the stdio entrypoint |
| `transport_allowlist_required` | HTTP without both a host and an origin allowlist |
| `wildcard_allowlist_refused` | an allowlist entry contains `*` |
| `bind_host_not_allowed`, `bind_origin_not_allowed` | the allowlist does not admit the address about to be bound |
| `cwd_missing` | the requested working directory does not exist |
| `no_plans`, `plan_mode_duplicated`, `runtime_mismatch` | the lock to record is empty, repeats a mode, or mixes two runtimes |
| `lock_corrupt` | a recorded `entrypoints.json` is unreadable |

`verify` answers about a recorded lock, so an install root that holds no lock is
reported as `lock_absent` before any runtime is resolved: a missing record is the
answer, not a launch failure.

## Evidence

`tests/test_entrypoints.py` creates **real** virtual environments with
`venv.EnvBuilder` at a path holding a space, an ampersand, parentheses, an apostrophe
and Thai text, and then starts the planned argv for real - a JSON-RPC `initialize`
over the process pipes - which is the only way to prove that such a path needs no
quoting, no activation script and no shell. The refusal legs are driven by a scripted
interpreter probe, because creating a broken virtual environment per case would cost
minutes and prove the same branches; the one real environment that must fail to
resolve is real. Scripted legs say so in their own docstring.

This surface also exposed a live defect in the entrypoint it distributes:
`axiom_mcp.stdio.main` passed `banner=` to `anyio.run`, which forwards only positional
arguments to the callable it is given, so `python -m axiom_mcp.stdio` - the documented
way to run the transport on its own, and the exact argv this plan names - died with
`TypeError: run() got an unexpected keyword argument 'banner'` before serving anything.
`serve_stdio` itself was correct and directly tested, which is why the defect stayed
latent until a plan was actually launched. `main` now binds the keyword with
`functools.partial`.

### Reproduction

```text
python -m pytest tests/test_entrypoints.py -q
python -m pytest tests -q
python -m ruff check .
python -m ruff format --check .
python -m axiom_mcp.entrypoints plan --mode stdio --install-root <install root>
python -m axiom_mcp.entrypoints verify --mode stdio --install-root <install root>
```

## Not run here

The following cannot be produced on this host and are recorded as `not_run` rather
than claimed:

- Windows reparse-point (junction/symlink) resolution of an interpreter path, and
  Windows ACL behaviour on the install root;
- macOS: the POSIX `bin/python` layout, `HOME`-derived user site and POSIX permissions;
- a real non-loopback bind: only the refusal path
  (`--allow-non-loopback-bind` without an admitting allowlist) is exercised;
- a real `pip install` into the version environment: the packager's own regression
  test owns that leg (`tests/test_release_package.py`), and this surface only reads the
  layout the packager leaves behind.