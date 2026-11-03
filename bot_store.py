import sqlite3


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS videos (
                bot INTEGER, source TEXT, format TEXT, file_id TEXT NOT NULL,
                PRIMARY KEY (bot, source, format)
            );
            CREATE TABLE IF NOT EXISTS preferences (
                bot INTEGER, owner INTEGER, setting TEXT, value INTEGER NOT NULL,
                PRIMARY KEY (bot, owner, setting)
            );
        """)

    def file_id(self, key):
        row = self.db.execute(
            "SELECT file_id FROM videos WHERE bot=? AND source=? AND format=?", key
        ).fetchone()
        return row[0] if row else None

    def save(self, key, file_id):
        self.db.execute("INSERT OR REPLACE INTO videos VALUES (?,?,?,?)", (*key, file_id))

    def forget(self, key):
        self.db.execute("DELETE FROM videos WHERE bot=? AND source=? AND format=?", key)

    def preference(self, bot, owner, setting, default):
        row = self.db.execute(
            "SELECT value FROM preferences WHERE bot=? AND owner=? AND setting=?",
            (bot, owner, setting),
        ).fetchone()
        return row[0] if row else default

    def set_preference(self, bot, owner, setting, value):
        self.db.execute("INSERT OR REPLACE INTO preferences VALUES (?,?,?,?)",
                        (bot, owner, setting, value))
