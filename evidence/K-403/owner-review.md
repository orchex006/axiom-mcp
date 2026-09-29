# K-403 owner review

The wheel is byte-identical to K-103 SHA-256
`ccb2166c414d36b16ca8316dc93e34d13a77f4db31ed9117895d8063161be20b`.
`git diff` from source revision `d92e71b25623132966b602b15ef5112c7eef7c96`
to the K-403 base finds no change in `src/axiom_mcp/`, `pyproject.toml` or
`README.md`. Metadata requires Python 3.13 and declares both console scripts.

The new Linux x64 dependency file pins 31 versions and SHA-256 values. An
ephemeral Debian 12 x86_64 container running CPython 3.13.15 installed all
dependencies with `pip --require-hashes --only-binary=:all:`, then installed
the wheel, imported Axiom MCP and its direct dependencies, found both scripts
and passed `pip check`. The local owner suite passed 762 tests with two skips;
36 package/runtime pin tests and both Ruff checks passed. The Python warning path in the pytest log is redacted as `<axiom-mcp>`; the warning itself is preserved. Corrupt wheel and
unsupported Python 3.9 installation each exited 1 as expected.

The owner change is limited to the Linux dependency lock, Changelog and
K-403 evidence. The wheel payload itself is reused. Docker installation
changed only a temporary container and the negative cases used temporary
virtual environments; no user installation or host state was changed. Revert
the K-403 owner commit to undo these inputs. Independent main-integration
review and released trust/signing remain pending.
