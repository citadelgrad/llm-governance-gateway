# Fly.io example infrastructure

These files are deployment examples, not the default local development path.

Run commands from the repository root and pass the desired config explicitly:

```bash
fly deploy -c infra/example/fly.io/fly-proxy.toml
fly deploy -c infra/example/fly.io/fly-governance.toml
fly deploy -c infra/example/fly.io/fly-opa.toml
fly deploy -c infra/example/fly.io/fly-cron.toml
```

Set secrets with the template script after replacing/exporting real values:

```bash
bash infra/example/fly.io/fly-secrets.sh.example
```

The default supported developer workflow remains Docker Compose via `make up`, `make demo`, and `make test-integration`.
