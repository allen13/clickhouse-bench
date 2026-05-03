# ClickHouse Cloud Setup Guide

End-to-end walkthrough for creating a ClickHouse Cloud account, getting your connection credentials, and wiring them into this project's `.env` file.

---

## 1. Create a ClickHouse Cloud account

1. Go to <https://clickhouse.cloud/signUp>.
2. Sign up with email, Google, or GitHub. New accounts get a **30-day free trial with $300 in credits** — no credit card required.
3. Verify your email and finish the onboarding form (organization name, role).

## 2. Create a service

A "service" is a managed ClickHouse cluster. You'll create one per environment (dev, prod, etc.).

1. From the Cloud console, click **+ New service**.
2. Pick a tier:
   - **Development** — 1 replica, ~$1/day idle, fine for benchmarking and prototyping.
   - **Production** — 3 replicas, HA, autoscaling. Use this once you care about uptime.
3. Choose a **cloud provider** (AWS / GCP / Azure) and a **region** close to where you'll run the client. Network latency dominates small-query benchmarks, so co-locate when possible.
4. Name the service (e.g., `bench-dev`) and click **Create service**.
5. Provisioning takes ~2–5 minutes.

## 3. Capture the connection details

While the service spins up, ClickHouse Cloud shows a **Connect** modal. If you dismiss it, you can reopen it from the service page → **Connect** button.

Grab these four values:

| Field | Where to find it | Typical value |
|---|---|---|
| **Host** | Connect modal → "Hostname" | `xyz123abc.us-east-1.aws.clickhouse.cloud` |
| **Port** | Connect modal → "Port" (HTTPS / native-secure) | `8443` (HTTPS) or `9440` (native TLS) |
| **User** | Default admin user | `default` |
| **Password** | Shown **once** in the modal — copy immediately | (random string) |

> ⚠️ The default password is shown only at service creation. If you lose it, reset it from **Service → Settings → Reset password** (this invalidates the old one).

## 4. Allow your IP

ClickHouse Cloud blocks all inbound traffic by default.

1. Service page → **Settings → Security → IP access list**.
2. Add your current IP (the console shows it), or `0.0.0.0/0` for an open trial environment (**not** for anything with real data).

## 5. Wire credentials into the project

This repo reads credentials from a `.env` file at the project root. **`.env` is already in `.gitignore`** (see `.gitignore:13`) so secrets won't be committed.

```bash
cd ~/projects/clickhouse-bench
cp .env.example .env
```

Open `.env` and fill in the values from step 3:

```dotenv
CLICKHOUSE_HOST=xyz123abc.us-east-1.aws.clickhouse.cloud
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=<paste-from-connect-modal>
CLICKHOUSE_DATABASE=default
CLICKHOUSE_SECURE=true
```

Verify `.env` is ignored:

```bash
git check-ignore -v .env
# → .gitignore:13:.env	.env
```

If that command prints nothing, the file is **not** ignored — stop and fix `.gitignore` before continuing.

## 6. Test the connection

```bash
uv sync
uv run clickhouse-bench setup
```

A successful run creates the four benchmark tables (`users`, `orders`, `events`, `metrics`). If you see a TLS or auth error, re-check the host (no `https://` prefix), the port, and that your IP is on the allow list.

---

## Secret hygiene

- **Never** paste credentials into chat, issues, PRs, or commit messages.
- Don't `cat .env` in shared terminals or screen-shares.
- If a password leaks: reset it in the Cloud console immediately — old value is revoked instantly.
- For CI: store the same variables as repository secrets (GitHub Actions: **Settings → Secrets → Actions**) and inject them as env vars. Don't write a `.env` file in CI.
- Rotate the `default` user password every 90 days, or create a per-purpose SQL user (`CREATE USER bench IDENTIFIED BY '...'`) and grant it the minimum needed privileges.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Connection refused` / `timeout` | IP not on allow list | Add IP under Service → Settings → Security |
| `Authentication failed` | Wrong password or user | Reset password in console; update `.env` |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Corporate MITM proxy | Set `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` to your proxy CA |
| `Code: 81. Database ... doesn't exist` | `CLICKHOUSE_DATABASE` typo | Use `default` until you create your own |
| Service shows "Idle" | Auto-suspended after inactivity | First query wakes it (~10s cold start) |

## Costs to watch

- Development tier idle ≈ **$1/day**; query bursts add minutes of compute.
- Storage is billed separately (~$0.04/GB-month compressed).
- The trial credit covers light benchmarking for the full 30 days. Watch **Billing → Usage** to avoid surprises.
- Delete unused services from **Service → Settings → Delete service** when done — pausing isn't free.
