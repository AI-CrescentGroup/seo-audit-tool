-- SEO Audit Tool — Supabase Schema
-- Run this in the Supabase SQL editor

create extension if not exists "uuid-ossp";

create table if not exists audits (
    id          uuid primary key default uuid_generate_v4(),
    domain      text not null,
    metrics     jsonb not null default '{}',
    pagespeed   jsonb,
    pdf_url     text,
    ai_insights jsonb,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create index if not exists audits_domain_idx     on audits (domain);
create index if not exists audits_created_at_idx on audits (created_at desc);
create index if not exists audits_metrics_idx    on audits using gin (metrics);

create or replace function update_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists audits_updated_at on audits;
create trigger audits_updated_at
    before update on audits
    for each row execute function update_updated_at();

-- Summary view
create or replace view audit_summaries as
select
    id,
    domain,
    (metrics->>'pages_crawled')::int                         as pages_crawled,
    (ai_insights->>'overall_score')::int                     as seo_score,
    (pagespeed->>'performance')::int                         as pagespeed_performance,
    (metrics->'http_errors'->>'count')::int                  as http_errors,
    (metrics->'missing_h1'->>'count')::int                   as missing_h1,
    (metrics->'broken_internal_links'->>'count')::int        as broken_links,
    created_at
from audits;
