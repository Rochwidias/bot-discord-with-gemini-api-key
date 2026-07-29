-- Jalankan SQL ini di Supabase SQL Editor (https://supabase.com/dashboard/project/_/sql/new)

CREATE TABLE IF NOT EXISTS guild_states (
    guild_id BIGINT PRIMARY KEY,
    queue JSONB DEFAULT '[]'::jsonb,
    history JSONB DEFAULT '[]'::jsonb,
    current_song JSONB DEFAULT NULL,
    repeat BOOLEAN DEFAULT FALSE,
    text_channel_id BIGINT DEFAULT NULL,
    voice_channel_id BIGINT DEFAULT NULL,
    player_message_id BIGINT DEFAULT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chat_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username TEXT NOT NULL,
    pesan TEXT NOT NULL,
    balasan TEXT NOT NULL,
    engine TEXT DEFAULT 'GEMINI',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index biar query chat_logs cepet
CREATE INDEX IF NOT EXISTS idx_chat_logs_created_at ON chat_logs(created_at DESC);
