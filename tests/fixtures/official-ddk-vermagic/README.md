These eight `vermagic` values were read from the actual `.modinfo` ELF sections
of the ARM64 modules built by SukiSU's official workflow for commit
`b50518a2946b7ca49e933356343526bb7dd81ff8`, using DDK release `20260828`.

Each downloaded Actions archive was checked against its original GitHub API
SHA-256 and size before extracting its module. `provenance.json` records the
official artifact URL and digest, the extracted module's digest and size, its
`.modinfo` digest, and the immutable DDK image used by this repository.

Seven official modules have a generic kernel release without an `androidNN`
substring. Inferring Android KMI from that substring incorrectly rejected real
official-compatible output. Production checks bind each KMI to its fixed image
and exact release instead. This fixture records public metadata only; it does
not contain loadable modules and is never embedded in an APK or kernel.
