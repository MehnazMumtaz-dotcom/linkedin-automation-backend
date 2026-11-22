// MongoDB removed — database is now Supabase (PostgreSQL).
// This file is kept as a no-op so existing imports don't break during migration.

export async function connectDatabase(): Promise<void> {
  // Supabase client is HTTP-based — no persistent connection needed.
  // The client initializes lazily on first query in src/lib/supabase.ts.
  const { isSupabaseConfigured } = await import("./lib/supabase");
  if (isSupabaseConfigured) console.log("✓ Supabase client ready (HTTP — no connection required)");
}
