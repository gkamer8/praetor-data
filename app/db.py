import sqlite3
import json
import click
from flask import current_app, g
import os
import shutil


def dict_factory(cursor, row):
    """
    Converts each row to a dictionary
    """
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

def get_db():
    if 'db' not in g:

        need_to_init = not os.path.exists(current_app.config['DATABASE'])

        g.db = sqlite3.connect(
            current_app.config['DATABASE'],
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = dict_factory

        if need_to_init:
            with current_app.open_resource('schema.sql') as f:
                g.db.executescript(f.read().decode('utf8'))

    return g.db


def get_tmp_db(instance_path, old_db_path):

    # TODO: make unique for each process?
    new_db_path = os.path.join(instance_path, "tmp.sqlite")
    shutil.copyfile(old_db_path, new_db_path)

    db = sqlite3.connect(
        new_db_path,
        detect_types=sqlite3.PARSE_DECLTYPES
    )
    db.row_factory = dict_factory

    return db, new_db_path


def close_db(e=None):
    db = g.pop('db', None)

    if db is not None:
        db.close()

def init_db():
    db = get_db()

    with current_app.open_resource('schema.sql') as f:
        db.executescript(f.read().decode('utf8'))


@click.command('init-db')
def init_db_command():
    """Clear the existing data and create new tables."""
    init_db()
    click.echo('Initialized the database.')

def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)


class SQLiteJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, sqlite3.Row):
            return dict(obj)
        return json.JSONEncoder.default(self, obj)