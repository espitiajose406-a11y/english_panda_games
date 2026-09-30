"""Crea las tablas en una base MySQL remota (por ejemplo Aiven).

Usa las mismas variables de entorno que app.py:
DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME y DB_SSL_CA.
Lee database/panda.sql y omite las lineas CREATE DATABASE y USE,
porque la base de datos ya existe en el servicio remoto.
"""
import os
import re

import pymysql

from app import DB_CONFIG


def statements():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database", "panda.sql")
    with open(path, encoding="utf-8") as f:
        lines = [l for l in f.read().splitlines() if not l.strip().startswith("--")]
    out = []
    for stmt in "\n".join(lines).split(";"):
        stmt = stmt.strip()
        if stmt and not re.match(r"(CREATE\s+DATABASE|USE)\b", stmt, re.I):
            out.append(stmt)
    return out


if __name__ == "__main__":
    stmts = statements()
    print(f"Conectando a {DB_CONFIG['host']}:{DB_CONFIG['port']} / {DB_CONFIG['database']} ...")
    con = pymysql.connect(**DB_CONFIG)
    try:
        with con.cursor() as cur:
            for s in stmts:
                cur.execute(s)
        con.commit()
    finally:
        con.close()
    print(f"Listo: {len(stmts)} tablas creadas o ya existentes.")
