# Security Policy

## Supported versions

Security fixes are provided for the latest released minor version.

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |
| Older | No |

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's **Report a vulnerability**
button on the repository Security page to create a private security advisory.

Include the affected version, operating system, transport, reproduction steps, and expected impact.
Remove real questions, database contents, local paths, tokens, and other personal information before
submitting evidence.

You should receive an acknowledgement within seven days. Confirmed reports will be investigated,
fixed on a private branch, and disclosed with a coordinated release when appropriate.

## Security boundaries

- The default deployment is a local single-user stdio server.
- Streamable HTTP does not provide authentication or tenant isolation by itself.
- Generated PDF content is treated as untrusted and rendered without JavaScript or a TeX process.
- Export resources are constrained to the configured data directory and verified by SHA-256.
- OCR is performed by the host agent; this server does not upload images to an external provider.
