-- Optional Supabase / PostgreSQL schema for future high-volume analytics.
-- Current production stack uses MongoDB (mini_app_analytics, story_events) via FastAPI.

create table if not exists analytics_users (
  id                bigserial primary key,
  telegram_id       bigint not null unique,
  first_seen_at     timestamptz not null default now(),
  last_seen_at      timestamptz not null default now(),
  visits            integer not null default 0,
  is_premium        boolean not null default false,
  language          text,
  timezone          text
);

create table if not exists analytics_sessions (
  id                uuid primary key default gen_random_uuid(),
  telegram_id       bigint not null references analytics_users(telegram_id) on delete cascade,
  started_at        timestamptz not null default now(),
  ended_at          timestamptz,
  duration_seconds  double precision,
  device            text,
  browser           text,
  os                text,
  country           text,
  region            text,
  city              text,
  referrer          text
);

create index if not exists idx_sessions_started on analytics_sessions (started_at desc);
create index if not exists idx_sessions_user on analytics_sessions (telegram_id);

create table if not exists analytics_events (
  id                bigserial primary key,
  session_id        uuid references analytics_sessions(id) on delete set null,
  telegram_id       bigint,
  event_type        text not null,
  payload           jsonb not null default '{}',
  country           text,
  city              text,
  created_at        timestamptz not null default now()
);

create index if not exists idx_events_type_time on analytics_events (event_type, created_at desc);
create index if not exists idx_events_user_time on analytics_events (telegram_id, created_at desc);

create table if not exists analytics_story_metrics (
  story_id          text primary key,
  views             bigint not null default 0,
  completions       bigint not null default 0,
  avg_listen_sec    double precision,
  updated_at        timestamptz not null default now()
);

create table if not exists analytics_searches (
  id                bigserial primary key,
  query             text not null,
  success           boolean not null default true,
  telegram_id       bigint,
  created_at        timestamptz not null default now()
);

create index if not exists idx_searches_query on analytics_searches (lower(query), created_at desc);
