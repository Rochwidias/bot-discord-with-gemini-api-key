import os
from functools import lru_cache

@lru_cache(maxsize=1)
def get_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        print("⚠️ SUPABASE_URL / SUPABASE_KEY belum diisi. State hanya disimpan di memory.")
        return None
    from supabase import create_client
    sb = create_client(url, key)
    _check_tables(sb)
    return sb

def _check_tables(sb):
    try:
        sb.table("guild_states").select("guild_id").limit(1).execute()
    except Exception as e:
        if "PGRST205" in str(e):
            print("❌ Tabel guild_states & chat_logs belum ada di Supabase.")
            print("   Buka https://supabase.com → SQL Editor → paste isi schema.sql → Run")
