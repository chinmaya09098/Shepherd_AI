"""PostgreSQL persistence layer for Shepherd AI.

Stores one row per email (inbound or outbound) in the `email_records` table.
Connection is driven by Config.DATABASE_URL (or POSTGRES_* parts). When no
database is configured the layer is a no-op so the rest of the app keeps working.
"""
