import os
import psycopg

def _setting(name):
    value = os.environ.get(name)
    if value:
        return value

    try:
        import streamlit as st
        return st.secrets.get(name)
    except (ImportError, FileNotFoundError, KeyError):
        return None

def get_connection():
    return psycopg.connect(
        host=_setting("SUPABASE_DB_HOST"),
        port=int(_setting("SUPABASE_DB_PORT") or 5432),
        dbname="postgres",
        user=_setting("SUPABASE_DB_USER"),
        password=_setting("SUPABASE_DB_PASSWORD"),
        sslmode="require",
        connect_timeout=10,
    )