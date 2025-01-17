import multiprocessing
import json
import os
from unicodedata import name
from app.db import SQLiteJSONEncoder
from flask import current_app, session
from app.utils import get_named_arguments
from app.db import get_tmp_db
import shutil
import psutil


"""

All functions that depend on the peculiarities of the database

"""

# EXAMPLES

def add_example(db, prompt_id, completion, tags):
    c = db.cursor()
    c.execute("INSERT INTO examples (completion, prompt_id) VALUES (?, ?)", (completion, prompt_id))
    item_id = c.lastrowid
    
    # Add tags
    for tag in tags:
        c.execute("INSERT INTO tags (example_id, value) VALUES (?, ?)", (item_id, tag))
    db.commit()
    return item_id


def delete_example(db, example_id):
    db.execute("DELETE FROM examples WHERE id = ?", (example_id,))
    db.commit()


def update_example(db, example_id, completion, tags):
    c = db.cursor()

    # Remove all tags
    c.execute("DELETE FROM tags WHERE example_id = ?", (example_id,))
    # Update text
    c.execute("UPDATE examples SET completion = ? WHERE id = ?", (completion, example_id))
    for t in tags:
        c.execute("INSERT INTO tags (example_id, value) VALUES (?, ?)", (example_id, t))
    db.commit()
    return example_id


# PROMPTS

def delete_prompt(db, prompt_id):

    # Remove all tags
    # Remove all prompt_values
    # remove associated examples

    db.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
    db.execute("DELETE FROM tags WHERE prompt_id = ?", (prompt_id,))
    db.execute("DELETE FROM prompt_values WHERE prompt_id = ?", (prompt_id,))
    db.execute("DELETE FROM examples WHERE prompt_id = ?", (prompt_id,))
    db.commit()

def update_prompt(db, prompt_id, prompt_values, tags):
    c = db.cursor()

    # Update prompt values
    for key in prompt_values:
        c.execute("SELECT * FROM prompt_values WHERE prompt_id = ? AND key = ?", (prompt_id, key))
        res = c.fetchone()
        if res:
            sql = """
                UPDATE prompt_values SET value = ? WHERE prompt_id = ? AND key = ?
            """
            c.execute(sql, (prompt_values[key], prompt_id, key))
        else:
            sql = """
                INSERT INTO prompt_values (value, prompt_id, key) VALUES (?, ?, ?)
            """
            c.execute(sql, (prompt_values[key], prompt_id, key))
    # Update tags
    # Remove all prior tags
    sql = """
        DELETE FROM tags
        WHERE prompt_id = ?
    """
    c.execute(sql, (prompt_id,))
    for tag in tags:
        c.execute("INSERT INTO tags (value, prompt_id) VALUES (?, ?)", (tag, prompt_id))

    db.commit()
    return prompt_id

# Adds prompt to database and returns that prompt's id
def add_prompt(db, **kwargs):

    tags = kwargs['tags']
    keys = kwargs['keys']
    project_id = kwargs['project_id']
    style_id = kwargs['style_id']

    c = db.cursor()

    c.execute("INSERT INTO prompts (project_id, style) VALUES (?, ?)", (project_id, style_id))
    prompt_id = c.lastrowid

    for t in tags:
        c.execute("INSERT INTO tags (value, prompt_id) VALUES (?, ?)", (t, prompt_id))

    for k in keys:
        c.execute("INSERT INTO prompt_values (prompt_id, key, value) VALUES (?, ?, ?)", (prompt_id, k, keys[k]))

    db.commit()
    return prompt_id

# Takes data and inserts prompts and examples
# Data is a list of dictionaries (i.e. json data)
# tags is a list
def add_bulk(db, data, tags, project_id, style_id):
    c = db.cursor()
    sql = """
        INSERT INTO tasks (`type`, `status`)
        VALUES ("bulk_upload",
                "in_progress"
                );
    """
    c.execute(sql)
    task_id = c.lastrowid
    db.commit()

    p = multiprocessing.Process(target=add_bulk_background, args=(current_app.instance_path, current_app.config['DATABASE'], task_id, data, tags, project_id, style_id))
    p.start()

    sql = """
        UPDATE tasks
        SET pid = ?
        WHERE id = ?
    """
    db.execute(sql, (p.pid, task_id))
    db.commit()

    # Make session token to warn user about parallelism
    session['warn_parallelism'] = True

    status = {
        'pid': p.pid,
        'status': 'in_progress'
    }
    return status

# NOTE: doesn't support example tags yet or multiple examples per prompt
def add_bulk_background(instance_path, old_db_path, task_id, data, tags, project_id, style_id):
    db, new_db_path = get_tmp_db(instance_path, old_db_path)
    
    try:
        c = db.cursor()

        # Get the correct keys and note which is the completion key
        sql = """
            SELECT * FROM style_keys
            WHERE style_id = ?
        """
        res = c.execute(sql, (style_id,))
        style_keys = res.fetchall()

        # Get the style
        sql = """
            SELECT * FROM styles
            WHERE id = ?
        """
        res = c.execute(sql, (style_id))
        style_info = res.fetchone()

        completion_key = style_info['completion_key']

        prompt_values_keys = [x['name'] for x in style_keys if x['name'] != completion_key]
        db.commit()

        for item in data:

            # Add new prompt
            sql = """
                INSERT INTO prompts (style, project_id) VALUES (?, ?)
            """
            c.execute(sql, (style_id, project_id))
            prompt_id = c.lastrowid

            # Add examples and prompt values
            for key in item:
                if key == completion_key:
                    sql = """
                        INSERT INTO examples (prompt_id, completion) VALUES (?, ?)
                    """
                    c.execute(sql, (prompt_id, item[key]))
                elif key in prompt_values_keys:  # need to avoid tags, other irrelevant values
                    sql = """
                        INSERT INTO prompt_values (prompt_id, key, value) VALUES (?, ?, ?)
                    """
                    c.execute(sql, (prompt_id, key, item[key]))

            # Add tags to prompt
            for tag in tags:
                sql = """
                    INSERT INTO tags (prompt_id, value) VALUES (?, ?)
                """
                c.execute(sql, (prompt_id, tag))

            db.commit()

        sql = """
            UPDATE tasks
            SET status = 'completed'
            WHERE id = ?
        """
        db.execute(sql, (task_id,))
        db.commit()
    except:
        sql = """
        UPDATE tasks
        SET status = 'failed'
        WHERE id = ?;
        """
        db.execute(sql, (task_id,))
        db.commit()

    shutil.copyfile(new_db_path, old_db_path)

"""

Will export a json file, which is a list of dictionaries with each key matching
   a named argument in the template format string.
All named arguments are present in every dictionary.
Does not include tags at the moment.

If filename is None or "", the filename becomes "export.json"

"""
def export(db, filename, tags=[], content="", example="", project_id=None, style_id=None):

    if not filename:
        filename = "export.json"

    c = db.cursor()
    sql = """
        INSERT INTO tasks (`type`, `status`)
        VALUES ("export",
                "in_progress"
                );
    """
    c.execute(sql)
    db.commit()

    task_id = c.lastrowid

    p = multiprocessing.Process(target=export_background, args=(current_app.instance_path, current_app.config['DATABASE'], current_app.config['EXPORTS_PATH'], task_id, filename, content, tags, example, style_id, project_id))
    p.start()

    sql = """
        UPDATE tasks SET `pid` = ?
        WHERE id = ?
    """
    c.execute(sql, (p.pid, task_id))

    # Make session token to warn user about parallelism
    session['warn_parallelism'] = True

    db.commit()
    status = {
        'pid': p.pid,
        'status': 'in_progress'
    }
    return status

def export_background(instance_path, old_db_path, exports_path, task_id, filename, content, tags, example, style_id, project_id):

    db, new_db_path = get_tmp_db(instance_path, old_db_path)

    # The try catch is not compehensive
    # There should be an option for the user to check on the program itself (via its pid)
    try:
        if not example:
            example = "%"
        if not content:
            content = "%"

        args = [example]

        tag_query_str = ""
        if tags and len(tags) > 0:
            tag_query_str = f"JOIN tags ON prompts.id = tags.prompt_id AND ("
            for i, tag in enumerate(tags):
                tag_query_str += f"tags.value LIKE ?"
                args.append(tag)
                if i < len(tags) - 1:
                    tag_query_str += " OR "
            tag_query_str += ")"

        args.append(content)

        proj_id_query = ""
        if project_id:
            proj_id_query = "AND prompts.project_id = ?"
            args.append(project_id)

        style_id_query = ""
        if style_id:
            style_id_query = "AND prompts.style = ?"
            args.append(style_id)

        # notice that in string, there is no option to put "LEFT" join on examples
        # this is beacuse we really do only want prompts with examples in this case
        sql = f"""
            SELECT DISTINCT prompts.*, styles.template as template, styles.completion_key as completion_key
            FROM prompts
            JOIN prompt_values ON prompts.id = prompt_values.prompt_id
            JOIN styles ON prompts.style = styles.id AND prompt_values.key = styles.preview_key
            JOIN examples ON prompts.id = examples.prompt_id AND examples.completion LIKE ?
            {tag_query_str}
            WHERE prompt_values.value LIKE ?
            {proj_id_query}
            {style_id_query}
        """
        c = db.cursor()
        res = c.execute(sql, tuple(args))
        prompts = res.fetchall()

        path = os.path.join(exports_path, filename)
        fhand = open(path, 'w')
        fhand.write('[\n')

        encoder = SQLiteJSONEncoder(indent=4)

        for k, prompt in enumerate(prompts):
            sql = """
                SELECT * FROM examples
                WHERE prompt_id = ?
            """
            examples = c.execute(sql, (prompt['id'],)).fetchall()

            sql = """
                SELECT * FROM prompt_values
                WHERE prompt_id = ?
            """
            prompt_values = c.execute(sql, (prompt['id'],)).fetchall()
            prompt_value_kwargs = {x['key']: x['value'] for x in prompt_values}

            template = prompt['template']
            named_args = get_named_arguments(template)

            kwargs = {}
            for arg in named_args:
                if arg in prompt_value_kwargs:
                    kwargs[arg] = prompt_value_kwargs[arg]
                elif arg != prompt['completion_key']:
                    kwargs[arg] = ""

            for i, ex in enumerate(examples):
                kwargs[prompt['completion_key']] = ex['completion']
            
                json_data = encoder.encode(kwargs)

                fhand.write(json_data)

                if i < len(examples) - 1:
                    fhand.write(",\n")
            
            if k < len(prompts) - 1:
                fhand.write(",\n")

        fhand.write(']')
        fhand.close()

        sql = """
                INSERT INTO exports (`filename`)
                VALUES (?);
            """
        db.execute(sql, (filename,))

        sql = """
                    UPDATE tasks
                    SET status = 'completed'
                    WHERE id = ?;
                """
        db.execute(sql, (task_id,))
        db.commit()
    except Exception as e:
        sql = """
            UPDATE tasks
            SET status = 'failed'
            WHERE pid = ?;
        """
        db.execute(sql, (os.getpid(),))
        db.commit()
        print(f"Error occurred: {e}")

    shutil.copyfile(new_db_path, old_db_path)

def search_prompts(db, limit=None, offset=None, content_arg=None, example_arg=None, tags_arg=None, project_id=None, style_id=None):
    
    offset = 0 if not offset else offset
    limit = 100 if not limit else limit
    content_arg = "%" if not content_arg else "%" + content_arg + "%"
    
    x = "" if example_arg else "LEFT"
    example_arg = "%" if not example_arg else "%" + example_arg + "%"

    args = [example_arg]

    tag_query_str = ""
    if tags_arg and len(tags_arg) > 0:
        tag_query_str = f"JOIN tags ON prompts.id = tags.prompt_id AND ("
        for i, tag in enumerate(tags_arg):
            tag_query_str += f"tags.value LIKE ?"
            args.append(tag)
            if i < len(tags_arg) - 1:
                tag_query_str += " OR "
        tag_query_str += ")"

    args.extend([content_arg])

    proj_id_query = ""
    if project_id:
        proj_id_query = "AND prompts.project_id = ?"
        args.append(project_id)

    style_id_query = ""
    if style_id:
        style_id_query = "AND prompts.style = ?"
        args.append(style_id)

    args.extend([limit, offset])

    sql = f"""
        WITH main_search AS
        (
            SELECT DISTINCT prompts.*, prompt_values.*
            FROM prompts
            JOIN prompt_values ON prompts.id = prompt_values.prompt_id
            JOIN styles ON prompts.style = styles.id AND prompt_values.key = styles.preview_key
            {x} JOIN examples ON prompts.id = examples.prompt_id AND examples.completion LIKE ?
            {tag_query_str}
            WHERE prompt_values.value LIKE ?
            {proj_id_query}
            {style_id_query}
        )
        SELECT main_search.*, GROUP_CONCAT(t.value) AS tags, COUNT(*) OVER() AS total_results
        FROM main_search
        LEFT JOIN tags t ON main_search.prompt_id = t.prompt_id
        GROUP BY main_search.id
        LIMIT ?
        OFFSET ?
    """

    results = db.execute(sql, tuple(args))
    fetched = results.fetchall()
    total_results = 0 if len(fetched) == 0 else fetched[0]['total_results']
    return fetched, total_results


def check_running(db):
    tasks = get_tasks(db)
    has_oustanding = False
    for task in tasks:
        if task['status'] == 'in_progress':
            task_id = task['id']
            pid = task['pid']

            def update_records(task_id):
                sql = """
                    UPDATE tasks
                    SET status = 'failed'
                    WHERE id = ?;
                """
                db.execute(sql, (task_id,))
                db.commit()
            try:
                process = psutil.Process(pid)
                # Check if process with pid exists
                if process.is_running() and process.ppid() == os.getpid():
                    # is still in progress, nothing to do
                    has_oustanding = True
                else:
                    update_records(task_id)
            except psutil.NoSuchProcess:
                update_records(task_id)
    if not has_oustanding:
        session['warn_parallelism'] = False

    return has_oustanding


def get_tasks(db):
    sql = """
        SELECT * FROM tasks ORDER BY created_at DESC
    """
    tasks = db.execute(sql)
    return tasks.fetchall()

def get_exports(db):
    sql = """
        SELECT * FROM exports ORDER BY created_at DESC
    """
    exports = db.execute(sql)
    return exports.fetchall()

def get_export_by_id(db, id):
    sql = """
        SELECT * FROM exports WHERE id = ?
    """
    export = db.execute(sql, id)
    return export.fetchone()

def get_prompt_by_id(db, id):
    sql = """
        SELECT * FROM prompts WHERE id = ?
    """
    prompt = db.execute(sql, (id,))
    return prompt.fetchone()

def get_examples_by_prompt_id(db, prompt_id, with_tags=True):
    if with_tags:
        sql = """
        SELECT e.*, GROUP_CONCAT(t.value) AS tags
        FROM examples e
        LEFT JOIN tags t ON e.id = t.example_id
        WHERE e.prompt_id = ?
        GROUP BY e.id
        """
    else:
        sql = """
            SELECT * FROM examples WHERE prompt_id = ?
        """
    examples = db.execute(sql, (prompt_id,))
    return examples.fetchall()

def get_projects(db):
    sql = """
        SELECT * FROM projects ORDER BY created_at DESC
    """
    examples = db.execute(sql)
    return examples.fetchall()

def get_project_by_id(db, id):
    sql = """
        SELECT * FROM projects WHERE id = ?
    """
    examples = db.execute(sql, (id,))
    return examples.fetchone()

def get_styles_by_project_id(db, id):
    sql = """
        SELECT * FROM styles WHERE project_id = ?
    """
    examples = db.execute(sql, (id,))
    return examples.fetchall()

def get_styles(db):
    sql = """
        SELECT * FROM styles ORDER BY created_at DESC
    """
    styles = db.execute(sql)
    return styles.fetchall()

def get_style_by_id(db, id):
    sql = """
        SELECT * FROM styles WHERE id = ?
    """
    style = db.execute(sql, (id,))
    return style.fetchone()

def get_keys_by_style_id(db, id):
    sql = """
        SELECT * FROM style_keys WHERE style_id = ?
    """
    keys = db.execute(sql, (id,))
    return keys.fetchall()

def get_tags_by_prompt_id(db, prompt_id):
    sql = """
        SELECT * FROM tags WHERE prompt_id = ?
    """
    tags = db.execute(sql, (prompt_id,))
    return tags.fetchall()

def get_prompt_values_by_prompt_id(db, prompt_id):
    c = db.cursor()
    sql = """
        SELECT * FROM prompt_values WHERE prompt_id = ?
    """
    vals = c.execute(sql, (prompt_id,))
    return vals.fetchall()

def add_project(db, name, description):
    sql = """
        INSERT INTO projects (name, desc)
        VALUES (?, ?)
    """
    c = db.cursor()
    c.execute(sql, (name, description))
    proj_id = c.lastrowid
    db.commit()
    return proj_id

def add_style(db, idtext, format_string, completion_key, preview_key, project_id, style_keys):
    sql = """
        INSERT INTO styles (id_text, template, completion_key, preview_key, project_id)
        VALUES (?, ?, ?, ?, ?)
    """
    c = db.cursor()
    c.execute(sql, (idtext, format_string, completion_key, preview_key, project_id))
    style_id = c.lastrowid
    db.commit()

    # Now add style keys
    for key in style_keys:
        sql = """
            INSERT INTO style_keys (name, style_id)
            VALUES (?, ?)
        """
        c.execute(sql, (key, style_id))
    db.commit()

    return style_id

def delete_project(db, project_id):
    c = db.cursor()
    # Remove all associated completions
    sql = """
        DELETE FROM examples
        WHERE prompt_id IN (
            SELECT id FROM prompts
            WHERE project_id = ?
        );
    """
    c.execute(sql, (project_id,))

    # Remove all associated prompts
    c.execute("DELETE FROM prompts WHERE project_id = ?", (project_id,))

    # Remove all associated styles
    c.execute("DELETE FROM styles WHERE project_id = ?", (project_id,))

    # Remove project
    c.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    db.commit()

def update_project(db, project_id, description, name):
    c = db.cursor()

    if description:
        c.execute("UPDATE projects SET desc = ? WHERE id = ?", (description, project_id))
    if name:
        c.execute("UPDATE projects SET name = ? WHERE id = ?", (name, project_id))

    db.commit()

def update_style(db, style_id, id_text, template, completion_key, preview_key):
    c = db.cursor()

    new_keys = get_named_arguments(template)
    c.execute("SELECT * FROM style_keys WHERE style_id = ?", (style_id,))
    old_keys = c.fetchall()

    missing_keys = [x['name'] for x in old_keys if x['name'] not in new_keys]
    added_keys = [x for x in new_keys if x not in [y['name'] for y in old_keys]]

    for key in missing_keys:
        c.execute("DELETE FROM style_keys WHERE name LIKE ? AND style_id = ?", (key, style_id))
        # Also we delete all the prompt values associated with the removed key
        c.execute("""
            DELETE FROM prompt_values
            WHERE prompt_values.key LIKE ?
            AND prompt_values.prompt_id IN (
                SELECT prompts.id FROM prompts
                JOIN styles ON prompts.style = styles.id
                WHERE styles.id = ?
            )
            """, (key, style_id))

    for key in added_keys:
        sql = """
            INSERT INTO style_keys (name, style_id)
            VALUES (?, ?)
        """
        c.execute(sql, (key, style_id))

    if id_text:
        c.execute("UPDATE styles SET id_text = ? WHERE id = ?", (id_text, style_id))
    if template:
        c.execute("UPDATE styles SET template = ? WHERE id = ?", (template, style_id))
    if completion_key:
        c.execute("UPDATE styles SET completion_key = ? WHERE id = ?", (completion_key, style_id))
    if preview_key:
        c.execute("UPDATE styles SET preview_key = ? WHERE id = ?", (preview_key, style_id))

    db.commit()


def delete_style(db, style_id):
    c = db.cursor()
    # Remove all associated completions
    sql = """
        DELETE FROM examples
        WHERE prompt_id IN (
            SELECT id FROM prompts
            WHERE style = ?
        );
    """
    c.execute(sql, (style_id,))

    # Remove all associated prompts
    c.execute("DELETE FROM prompts WHERE style = ?", (style_id,))

    # Remove the style
    c.execute("DELETE FROM styles WHERE id = ?", (style_id,))

    db.commit()

