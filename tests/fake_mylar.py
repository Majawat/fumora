"""Builds a small Mylar-shaped SQLite database for tests."""

import sqlite3
from pathlib import Path

COMICS = """CREATE TABLE comics (ComicID TEXT, ComicName TEXT, ComicSortName TEXT, ComicYear TEXT,
    ComicPublisher TEXT, ComicLocation TEXT, Status TEXT, Type TEXT, AgeRating TEXT)"""
ISSUES = """CREATE TABLE issues (IssueID TEXT, ComicID TEXT, Issue_Number TEXT, IssueName TEXT,
    IssueDate TEXT, ReleaseDate TEXT, Status TEXT, Location TEXT)"""
ANNUALS = """CREATE TABLE annuals (IssueID TEXT, ComicID TEXT, Issue_Number TEXT, IssueName TEXT,
    IssueDate TEXT, ReleaseDate TEXT, Status TEXT, Location TEXT,
    ReleaseComicID TEXT, ReleaseComicName TEXT)"""


def build(path: Path, comics, issues, annuals=()) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(COMICS)
    conn.execute(ISSUES)
    conn.execute(ANNUALS)
    conn.executemany("INSERT INTO comics VALUES (?,?,?,?,?,?,?,?,?)", comics)
    conn.executemany("INSERT INTO issues VALUES (?,?,?,?,?,?,?,?)", issues)
    conn.executemany("INSERT INTO annuals VALUES (?,?,?,?,?,?,?,?,?,?)", annuals)
    conn.commit()
    conn.close()
    return path
