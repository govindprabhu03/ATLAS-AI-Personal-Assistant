-- ATLAS · Supabase Row-Level Security (defense-in-depth)
--
-- The FastAPI backend connects as the table owner and enforces per-user scoping
-- (WHERE user_id = ...) in every query. Enabling RLS with NO policies leaves the
-- owner (the backend) fully working while DENYING all direct PostgREST/anon/
-- authenticated access to these tables — so a leaked anon key can't read anyone's
-- data through Supabase's auto REST API.
--
-- Run once in Supabase → SQL Editor. Safe to re-run.

ALTER TABLE items          ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory         ENABLE ROW LEVEL SECURITY;
ALTER TABLE recurring      ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_settings  ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE link_codes     ENABLE ROW LEVEL SECURITY;
ALTER TABLE notified       ENABLE ROW LEVEL SECURITY;
ALTER TABLE push_subs      ENABLE ROW LEVEL SECURITY;
