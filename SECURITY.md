# AI Knockout — Security Policy

Please refer to the full security documentation in [docs/SECURITY.md](docs/SECURITY.md).

### Quick Summary
- **No Hardcoded Secrets**: Secrets are read exclusively from environment variables.
- **Non-Root Containers**: Services execute under unprivileged user accounts.
- **Isolated Service Network**: Containers communicate across the internal `aik-internal` Docker bridge.
- **Strict Configuration Validation**: Application fails fast if weak or default secrets are supplied.
- **Security Headers**: Standard defense-in-depth headers applied to all API responses.
