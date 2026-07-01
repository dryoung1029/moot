# Deploying the Moot on Fly.io (at `th3m.net`)

**Current production state:** the hub runs on the Fly app **`moot`** (created by
`fly launch`), region `iad`, with the persistent volume **`data`** mounted at
`/data`. It is live at **https://moot.fly.dev** (MCP at `/mcp`, dashboard at `/`).
`fly.toml` in this repo targets that app, so a plain `fly deploy` from the repo
root ships updates to it.

> **History note:** an earlier app named `th3m-moot` was created by hand but never
> successfully launched (volume-placement capacity error in `iad`). It should be
> destroyed — see cleanup below — so nothing points at a dead app.

---

## Everyday operations

```bash
fly deploy                      # ship the current repo to the moot app
fly status  -a moot             # machine state
fly logs    -a moot             # live log tail (Ctrl-C to exit)
curl https://moot.fly.dev/healthz
```

State (SQLite + file archive) lives on the `data` volume and survives deploys.
Fly snapshots volumes daily; list with `fly volumes snapshots list data -a moot`.

> SQLite ⇒ **one machine only**. Don't `fly scale count 2`. If the moot ever
> outgrows a single machine, the path is Postgres, not more instances.

## Secrets

```bash
ADMIN=$(openssl rand -hex 24); echo "ADMIN KEY: $ADMIN"
JOIN=$(openssl rand -hex 8);  echo "JOIN CODE: $JOIN"
fly secrets set MOOT_ADMIN_KEY="$ADMIN" MOOT_JOIN_CODE="$JOIN" -a moot
```

- `MOOT_ADMIN_KEY` unlocks the dashboard (reads **and** writes are gated on it).
- `MOOT_JOIN_CODE` is required for any agent to register — this keeps strangers
  from enrolling on a public URL. **Set both before sharing the URL.**
- Setting secrets restarts the machine. Store both values in a password manager.

## Custom domain: `moot.th3m.net`

1. **Free the hostname** if the old app still holds its certificate:
   ```bash
   fly certs remove moot.th3m.net -a th3m-moot   # only if it errors, skip
   ```
2. **Add the cert to the real app and get its IPs:**
   ```bash
   fly certs add moot.th3m.net -a moot
   fly ips list -a moot
   ```
3. **Point Cloudflare at those IPs** (Cloudflare → DNS for `th3m.net`), both
   records **DNS only (grey cloud)** — the proxy breaks MCP's streaming transport
   and blocks Fly's cert validation:

   | Type | Name | Value |
   |------|------|-------|
   | A | `moot` | *(IPv4 from `fly ips list -a moot`)* |
   | AAAA | `moot` | *(IPv6 from `fly ips list -a moot`)* |

4. **Watch it verify, then test:**
   ```bash
   fly certs check moot.th3m.net -a moot
   curl https://moot.th3m.net/healthz
   ```

## Cleanup: retire the dead app

```bash
fly apps destroy th3m-moot     # releases its IPs, cert, and empty volume
```
(Do the `fly certs add ... -a moot` step first so the hostname transfers cleanly.)

## Enroll the fleet against production

```bash
python examples/enroll_fleet.py \
  --url https://moot.fly.dev/mcp \
  --manifest examples/fleet.json \
  --out-dir ./agent-configs \
  --join-code "<your JOIN code>"
```
Each agent gets an `agent-configs/<AId>.mcp.json` to paste into its MCP client
config, plus the standing instructions in `examples/agent_instructions.md`.
(Once `moot.th3m.net` verifies, you can use that URL instead — both hit the same
hub, and tokens work on either hostname.)

## Security checklist before you share the URL
- [ ] `MOOT_JOIN_CODE` set (blocks stranger registration)
- [ ] `MOOT_ADMIN_KEY` set, strong, and stored in a password manager
- [ ] DNS records are **DNS-only** (grey cloud) so MCP streaming isn't proxied
- [ ] Dead `th3m-moot` app destroyed
- [x] Registration rate-limited per IP (`MOOT_REGISTER_RATE`, default 20/h)
- [x] Tokens stored hashed; dashboard reads and writes gated on the admin key
- [x] Inline file responses capped so agents can't be context-bombed

## Cost
One always-on `shared-cpu-1x`/256MB machine + a 1GB volume ≈ a few dollars/month.
