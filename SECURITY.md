# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Email **hello@quanticdata.io** with the subject "Security: proxy-pool" and include a description, steps to reproduce and the version you use. We acknowledge reports within 3 business days and keep you informed until a fix is released.

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | yes |

## Credentials

Never commit proxy credentials or an API key. proxy-pool masks passwords in `str()`, `repr()`, logs and parse errors. The QuanticData source reads `QUANTICDATA_API_KEY` from the environment and sends it only in the `Authorization` header to the API base URL (`QUANTICDATA_API_BASE`, default `https://api.quanticdata.io/v1`). Report any path where a credential is printed, logged or sent elsewhere.
