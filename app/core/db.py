import os
import psycopg2
from psycopg2 import pool, extras
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "dbname": os.getenv("DB_NAME", "news_db"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "your_password"),
}

import threading

# Connection pool tuned for concurrent API + worker load.
_pool = None
_pool_lock = threading.Lock()


def get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                # Use ThreadedConnectionPool instead of SimpleConnectionPool for thread safety
                _pool = pool.ThreadedConnectionPool(5, 100, **DB_CONFIG)
    return _pool


def get_connection():
    """Get a connection from the pool with health checking."""
    conn = get_pool().getconn()
    try:
        # Check if connection is still alive (catches stale/broken connections)
        if conn.closed:
            get_pool().putconn(conn, close=True)
            conn = get_pool().getconn()
        else:
            # Quick health ping
            conn.cursor().execute("SELECT 1")
    except Exception:
        try:
            get_pool().putconn(conn, close=True)
        except Exception:
            pass
        conn = get_pool().getconn()
    return conn


def release_connection(conn):
    try:
        get_pool().putconn(conn)
    except Exception:
        try:
            get_pool().putconn(conn, close=True)
        except Exception:
            pass


def execute_query(query, params=None):
    """Execute a query (INSERT, UPDATE, DELETE) and return affected row count."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            conn.commit()
            return cur.rowcount
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise e
    finally:
        release_connection(conn)


def execute_many(query, params_list):
    """Execute a query with multiple param sets using execute_batch for speed."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            extras.execute_batch(cur, query, params_list)
            conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise e
    finally:
        release_connection(conn)


def fetch_all(query, params=None):
    """Execute a SELECT query and return all rows as list of dicts."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchall()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise e
    finally:
        release_connection(conn)


def fetch_one(query, params=None):
    """Execute a SELECT query and return the first row as a dict."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchone()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise e
    finally:
        release_connection(conn)
