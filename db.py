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
    return create_client(url, key)
