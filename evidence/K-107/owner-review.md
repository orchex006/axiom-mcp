# K-107 scoped owner review

Native host: Darwin x86_64, macOS 26.7; versioned runtime: uv managed CPython
3.13.15. A archive SHA-256 `caeee2470868b67367c27bbfc368171ba744bcf92c18afafe85d61a9fca2c86f`;
B archive SHA-256 `5e8c7efb2c40acf8c1b29af5061962090d12e02b391de5de5ea441cb67f1314e`.
The harness checked these bytes before extraction and verified installed executable,
real-source generation manifest, and installed wheel import path. All seven
candidate process legs passed; the matrix's 30 hashed artifacts match SHA256SUMS.
The stdio tool-call launcher composes the installed handler and transport for a
positive graph query; the bare transport entry point is deliberately unregistered.

The previous V2-031 synthetic evidence was not rewritten. Candidate mode requires
both installed paths, removes checkout `PYTHONPATH` from process children, and
checks the installed HTTP/stdio/query import locations. HTTP test tokens are
fixed disposable probe values. No production credential, private key, or user
home path is present in the changed source or native evidence. The isolated
`user-data.txt` retained its exact content through rollback, and the LaunchAgent
was uninstalled and booted out. The exact owner diff stayed within K-107 paths;
`git diff --check` and the secret pattern scan returned no finding.

Limitations: this is an unsigned local candidate, with no released-fixture
certification, other-lane result, or independent shared-governance review. The
HTTP stalled-body case proves bounded client cancellation/timeout on a real
loopback socket; it does not claim a separate server-side cancellation trace.
