-- ============================================================================
-- LinkedIn Automation Suite — initial database schema
-- ----------------------------------------------------------------------------
-- Creates every table, constraint, index, RPC function and seed row that the
-- Node service (automation-extensions) and the Python engine (automation-python)
-- use. Safe to run more than once (IF NOT EXISTS / ON CONFLICT everywhere).
--
-- Run it with either:
--   • Supabase dashboard → SQL Editor → paste this file → Run, or
--   • Supabase CLI:  npx supabase db push   (from automation-backend/)
-- ============================================================================

create extension if not exists pgcrypto;

-- ── updated_at helper ───────────────────────────────────────────────────────
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- ============================================================================
-- Core
-- ============================================================================

-- Bot registry. The suite uses a single bot: "cindy" (owner of the LinkedIn session).
create table if not exists public.bots (
  id          uuid primary key default gen_random_uuid(),
  name        text not null unique,
  created_at  timestamptz not null default now()
);

-- LinkedIn session (one row per bot + session_type).
create table if not exists public.bot_sessions (
  id            uuid primary key default gen_random_uuid(),
  bot_id        uuid not null references public.bots(id) on delete cascade,
  session_type  text not null default 'li_session',
  status        text not null default 'active',          -- active | expired
  raw_cookies   text,
  access_token  text,
  extra_data    jsonb not null default '{}'::jsonb,      -- { rawCookies, name, passcode }
  saved_at      timestamptz not null default now(),
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  constraint bot_sessions_bot_id_session_type_key unique (bot_id, session_type)
);
create index if not exists bot_sessions_status_idx on public.bot_sessions (bot_id, status);

-- Per-bot settings JSON (cron interval, cindy_cron running state…).
create table if not exists public.bot_settings (
  id          uuid primary key default gen_random_uuid(),
  bot_id      uuid not null unique references public.bots(id) on delete cascade,
  data        jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- Scraped / analysed LinkedIn profiles.
create table if not exists public.profiles (
  id                 uuid primary key default gen_random_uuid(),
  profile_url        text not null unique,
  vanity_name        text,
  name               text,
  headline           text,
  location           text,
  executive_summary  text,
  scraped_at         timestamptz,
  last_updated       timestamptz not null default now(),
  created_at         timestamptz not null default now()
);

-- ============================================================================
-- Cindy — inbox, history, knowledge base, escalation
-- ============================================================================

create table if not exists public.conversations (
  id                        uuid primary key default gen_random_uuid(),
  bot_id                    uuid not null references public.bots(id) on delete cascade,
  external_conversation_id  text not null,               -- LinkedIn thread URN
  sender_id                 text,
  sender_name               text,
  sender_username           text,
  last_activity             timestamptz not null default now(),
  locked_until              timestamptz,                 -- used by acquire_conversation_lock()
  created_at                timestamptz not null default now(),
  constraint conversations_bot_external_key unique (bot_id, external_conversation_id)
);
create index if not exists conversations_last_activity_idx on public.conversations (bot_id, last_activity desc);

create table if not exists public.messages (
  id               uuid primary key default gen_random_uuid(),
  conversation_id  uuid not null references public.conversations(id) on delete cascade,
  role             text not null,                        -- prospect | cindy | user | assistant
  text             text not null default '',
  source           text,                                 -- linkedin_inbox | cindy_auto
  sent_at          timestamptz not null default now(),
  created_at       timestamptz not null default now()
);
create index if not exists messages_conversation_idx on public.messages (conversation_id, sent_at);

-- bot_id NULL = global entry shared by all bots.
create table if not exists public.knowledge_base (
  id          uuid primary key default gen_random_uuid(),
  bot_id      uuid references public.bots(id) on delete cascade,
  type        text not null,
  title       text not null,
  content     text not null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index if not exists knowledge_base_bot_idx on public.knowledge_base (bot_id);

create table if not exists public.escalations (
  id                uuid primary key default gen_random_uuid(),
  bot_id            uuid not null references public.bots(id) on delete cascade,
  conversation_id   uuid references public.conversations(id) on delete set null,
  platform          text,
  sender_name       text,
  sender_username   text,
  last_message      text,
  reason            text,
  status            text not null default 'pending',     -- pending | reminded | resolved
  reminder_sent_at  timestamptz,
  resolved_at       timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create index if not exists escalations_status_idx on public.escalations (bot_id, status, created_at desc);

create table if not exists public.session_alerts (
  id           uuid primary key default gen_random_uuid(),
  bot_id       uuid not null references public.bots(id) on delete cascade,
  platform     text,
  status       text not null default 'pending',          -- pending | resolved
  resolved_at  timestamptz,
  created_at   timestamptz not null default now()
);
create index if not exists session_alerts_status_idx on public.session_alerts (bot_id, status);

-- Duplicate-message protection.
create table if not exists public.processed_messages (
  id                        uuid primary key default gen_random_uuid(),
  bot_id                    uuid not null references public.bots(id) on delete cascade,
  external_msg_id           text not null,
  external_conversation_id  text,
  created_at                timestamptz not null default now(),
  constraint processed_messages_bot_msg_key unique (bot_id, external_msg_id)
);

-- ============================================================================
-- Cindy — follow-ups
-- ============================================================================

create table if not exists public.follow_up_records (
  id                          uuid primary key default gen_random_uuid(),
  bot_id                      uuid not null references public.bots(id) on delete cascade,
  user_id                     text not null,             -- LinkedIn thread URN
  user_name                   text,
  contact_info                jsonb,
  original_message_id         text,
  original_message_text       text,
  original_message_sent_at    timestamptz,
  reply_received              boolean not null default false,
  reply_received_at           timestamptz,
  follow_ups_sent             integer not null default 0,
  last_followup_sent_at       timestamptz,
  next_followup_scheduled_at  timestamptz,
  status                      text not null default 'active',  -- active | paused | stopped | completed | replied
  manually_stopped_at         timestamptz,
  notes                       text,
  created_at                  timestamptz not null default now(),
  updated_at                  timestamptz not null default now()
);
create index if not exists follow_up_records_due_idx on public.follow_up_records (bot_id, status, next_followup_scheduled_at);
create index if not exists follow_up_records_user_idx on public.follow_up_records (bot_id, user_id);

create table if not exists public.follow_up_rules (
  id          uuid primary key default gen_random_uuid(),
  bot_id      uuid not null references public.bots(id) on delete cascade,
  type        text not null default 'keyword',
  value       text not null,
  enabled     boolean not null default true,
  created_at  timestamptz not null default now()
);

create table if not exists public.follow_up_templates (
  id                uuid primary key default gen_random_uuid(),
  bot_id            uuid not null references public.bots(id) on delete cascade,
  follow_up_number  integer not null check (follow_up_number between 1 and 5),
  message_text      text not null,
  delay_in_hours    numeric,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  constraint follow_up_templates_bot_number_key unique (bot_id, follow_up_number)
);

-- ============================================================================
-- Cara — personas & simulations
-- ============================================================================

create table if not exists public.cara_personas (
  id              uuid primary key default gen_random_uuid(),
  profile_url     text not null unique,
  name            text,
  headline        text,
  location        text,
  analysis        jsonb not null default '{}'::jsonb,
  posts_analyzed  integer not null default 0,
  scraped_at      timestamptz,
  last_updated    timestamptz not null default now(),
  created_at      timestamptz not null default now()
);

create table if not exists public.cara_simulation_sessions (
  id                   uuid primary key default gen_random_uuid(),
  persona_id           uuid not null references public.cara_personas(id) on delete cascade,
  conversation_topic   text,                             -- client session id
  simulated_responses  jsonb not null default '[]'::jsonb,
  last_activity        timestamptz not null default now(),
  created_at           timestamptz not null default now()
);
create index if not exists cara_sessions_persona_idx on public.cara_simulation_sessions (persona_id, last_activity desc);

-- ============================================================================
-- updated_at triggers
-- ============================================================================
do $$
declare t text;
begin
  foreach t in array array[
    'bot_sessions', 'bot_settings', 'knowledge_base', 'escalations',
    'follow_up_records', 'follow_up_templates'
  ] loop
    execute format('drop trigger if exists %I on public.%I', t || '_set_updated_at', t);
    execute format(
      'create trigger %I before update on public.%I for each row execute function public.set_updated_at()',
      t || '_set_updated_at', t
    );
  end loop;
end $$;

-- ============================================================================
-- RPC functions
-- ============================================================================

-- Atomic dedup: true when the message is newly marked, false if already processed.
create or replace function public.mark_message_processed(p_bot_id uuid, p_msg_id text, p_conversation_id text)
returns boolean
language plpgsql
as $$
begin
  insert into public.processed_messages (bot_id, external_msg_id, external_conversation_id)
  values (p_bot_id, p_msg_id, p_conversation_id)
  on conflict (bot_id, external_msg_id) do nothing;
  return found;
end;
$$;

-- Conversation lock (5 minute lease) so overlapping jobs never process the same thread.
create or replace function public.acquire_conversation_lock(p_id uuid)
returns boolean
language plpgsql
as $$
begin
  update public.conversations
     set locked_until = now() + interval '5 minutes'
   where id = p_id
     and (locked_until is null or locked_until < now());
  return found;
end;
$$;

create or replace function public.release_conversation_lock(p_id uuid)
returns void
language sql
as $$
  update public.conversations set locked_until = null where id = p_id;
$$;

-- ============================================================================
-- Security: Row Level Security on, no public policies.
-- The backends use the service_role key, which bypasses RLS; the anon key
-- (never used by this suite) gets no access.
-- ============================================================================
do $$
declare t text;
begin
  foreach t in array array[
    'bots', 'bot_sessions', 'bot_settings', 'profiles', 'conversations', 'messages',
    'knowledge_base', 'escalations', 'session_alerts', 'processed_messages',
    'follow_up_records', 'follow_up_rules', 'follow_up_templates',
    'cara_personas', 'cara_simulation_sessions'
  ] loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
end $$;

-- ============================================================================
-- Realtime: the Node session watcher listens for bot_sessions changes.
-- ============================================================================
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime')
     and not exists (
       select 1 from pg_publication_tables
        where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'bot_sessions'
     ) then
    alter publication supabase_realtime add table public.bot_sessions;
  end if;
end $$;

-- ============================================================================
-- Seed
-- ============================================================================
insert into public.bots (name) values ('cindy') on conflict (name) do nothing;
