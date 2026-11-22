# Database migrations (Supabase)

`migrations/20261121000000_linkedin_suite_init.sql` creates everything the suite needs:
15 tables, unique constraints, indexes, `updated_at` triggers, the RPCs
`mark_message_processed`, `acquire_conversation_lock`, `release_conversation_lock`,
Row Level Security, Realtime on `bot_sessions`, and the seed row `bots.name = 'cindy'`.
It is safe to run more than once.

## Option A — SQL Editor (easiest)
1. Supabase dashboard → your project → **SQL Editor** → **New query**.
2. Paste the whole migration file → **Run**.
3. Check: `select name from bots;` returns `cindy`.

## Option B — Supabase CLI
```bash
cd automation-backend
npx supabase login
npx supabase link --project-ref <your-project-ref>
npx supabase db push
```

New schema changes go in a new file: `migrations/<YYYYMMDDHHMMSS>_<name>.sql`.
