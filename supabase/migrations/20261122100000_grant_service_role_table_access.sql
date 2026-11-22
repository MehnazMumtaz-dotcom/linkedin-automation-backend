-- The service_role JWT bypasses RLS, but it still requires SQL table privileges.
-- Both backend services use this role for the suite's server-side data access.
grant usage on schema public to service_role;

grant select, insert, update, delete on table
  public.bots,
  public.bot_sessions,
  public.bot_settings,
  public.profiles,
  public.conversations,
  public.messages,
  public.knowledge_base,
  public.escalations,
  public.session_alerts,
  public.processed_messages,
  public.follow_up_records,
  public.follow_up_rules,
  public.follow_up_templates,
  public.cara_personas,
  public.cara_simulation_sessions
to service_role;
